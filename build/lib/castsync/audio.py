"""ffmpeg conversion to a podcast-ready MP3 with ID3 tags."""
from __future__ import annotations

import json
import os
import re
import subprocess


def probe_duration(path: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, check=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


# MP3 only allows certain bitrates, and which ones depends on the sample rate.
_MPEG1 = (32000, 44100, 48000)
_MPEG2 = (16000, 22050, 24000)
_MPEG25 = (8000, 11025, 12000)
BITRATES = {
    _MPEG1: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    _MPEG2: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    _MPEG25: (8, 16, 24, 32, 40, 48, 56, 64),
}
SAMPLE_RATES = _MPEG1 + _MPEG2 + _MPEG25
DEFAULTS = {"bitrate": "64k", "sample_rate": 44100, "channels": 1}


def _num(value) -> float:
    return float(re.sub(r"[^0-9.]", "", str(value)) or "nan")


def parse_bitrate(value) -> int:
    """64, "64", "64k", "64kbps", "64 kb/s" -> 64 (kbps)."""
    n = _num(value)
    if n != n:
        raise ValueError(f"audio.bitrate '{value}' is not a number")
    if n >= 1000:  # given in bits per second, e.g. 64000
        n /= 1000
    return int(round(n))


def parse_sample_rate(value) -> int:
    """44100, "44.1k", "44khz", "44.1 kHz", "48k", 22050 -> Hz.
    "44"/"44k"/"44khz" means 44.1 kHz (the CD/podcast standard)."""
    n = _num(value)
    if n != n:
        raise ValueError(f"audio.sample_rate '{value}' is not a number")
    if n < 1000:
        n *= 1000
    n = int(round(n))
    if n in (44000, 44100):
        return 44100
    if n in (22000, 22050):
        return 22050
    if n in (11000, 11025):
        return 11025
    return n


def parse_channels(value) -> int:
    v = str(value).strip().lower()
    if v in ("1", "mono", "m"):
        return 1
    if v in ("2", "stereo", "s", "joint", "joint_stereo"):
        return 2
    raise ValueError(f"audio.channels '{value}' must be mono (1) or stereo (2)")


def audio_settings(cfg) -> dict:
    """Validated MP3 settings from config, with podcast-friendly defaults
    (64 kbps, 44.1 kHz, mono)."""
    rate = parse_sample_rate(cfg.get("audio.sample_rate", DEFAULTS["sample_rate"]))
    if rate not in SAMPLE_RATES:
        raise ValueError(f"audio.sample_rate {rate} Hz is not allowed in MP3. Use one of: "
                         + ", ".join(str(r) for r in sorted(SAMPLE_RATES)))
    channels = parse_channels(cfg.get("audio.channels", DEFAULTS["channels"]))
    mode = str(cfg.get("audio.bitrate_mode", "cbr")).lower()
    if mode not in ("cbr", "vbr"):
        raise ValueError("audio.bitrate_mode must be cbr or vbr")
    out = {"sample_rate": rate, "channels": channels, "mode": mode}
    if mode == "cbr":
        kbps = parse_bitrate(cfg.get("audio.bitrate", DEFAULTS["bitrate"]))
        allowed = next(v for k, v in BITRATES.items() if rate in k)
        if kbps not in allowed:
            raise ValueError(f"audio.bitrate {kbps} kbps isn't possible at {rate} Hz. Use one of: "
                             + ", ".join(f"{b}k" for b in allowed))
        out["bitrate"] = kbps
    else:
        q = int(cfg.get("audio.vbr_quality", 6))
        if not 0 <= q <= 9:
            raise ValueError("audio.vbr_quality must be 0 (best, biggest) to 9 (smallest)")
        out["vbr_quality"] = q
    return out


def describe(settings: dict) -> str:
    rate = f"{settings['sample_rate'] / 1000:g} kHz"
    ch = "mono" if settings["channels"] == 1 else "stereo"
    rate_txt = (f"{settings['bitrate']} kbps" if settings["mode"] == "cbr"
                else f"VBR q{settings['vbr_quality']}")
    return f"{rate_txt}, {rate}, {ch}"


def to_mp3(src: str, dest: str, cfg, title: str, artist: str = "", album: str = "",
           date: str = "") -> str:
    a = audio_settings(cfg)
    filters = []
    if cfg.get("audio.loudnorm", True):
        lufs = cfg.get("audio.target_lufs", -16)
        filters.append(f"loudnorm=I={lufs}:TP=-1.5:LRA=11")
    trim_start = float(cfg.get("audio.trim_start_seconds", 0) or 0)
    trim_end = float(cfg.get("audio.trim_end_seconds", 0) or 0)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if trim_start:
        cmd += ["-ss", str(trim_start)]
    cmd += ["-i", src, "-vn", "-map", "0:a:0"]
    if trim_end:
        dur = probe_duration(src)
        if dur:
            cmd += ["-t", str(max(1.0, dur - trim_start - trim_end))]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-ac", str(a["channels"]), "-ar", str(a["sample_rate"]), "-codec:a", "libmp3lame"]
    if a["mode"] == "cbr":
        cmd += ["-b:a", f"{a['bitrate']}k"]
    else:
        cmd += ["-q:a", str(a["vbr_quality"])]
    cmd += ["-id3v2_version", "3",
            "-metadata", f"title={title}", "-metadata", f"artist={artist}",
            "-metadata", f"album={album}", "-metadata", "genre=Podcast"]
    if date:
        cmd += ["-metadata", f"date={date}"]
    tmp = dest + ".tmp.mp3"
    cmd.append(tmp)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
    os.replace(tmp, dest)
    return dest
