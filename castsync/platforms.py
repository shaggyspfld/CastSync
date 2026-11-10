"""Direct platform scanning with yt-dlp (no API keys needed for public channels).

Each configured URL is a channel's list of past streams, e.g.
  https://www.youtube.com/@YourShow/streams
  https://www.twitch.tv/yourshow/videos?filter=archives
  https://www.facebook.com/YourShow/live_videos   (Facebook support in yt-dlp is less reliable)
"""
from __future__ import annotations

import glob
import logging
import os
import time
from datetime import datetime, timezone

from .models import Candidate

log = logging.getLogger(__name__)


def _source_name(url: str) -> str:
    u = url.lower()
    for name in ("youtube", "twitch", "facebook"):
        if name in u or (name == "youtube" and "youtu.be" in u):
            return name
    return "ytdlp"


def _ts(info: dict):
    for k in ("release_timestamp", "timestamp"):
        if info.get(k):
            return int(info[k])
    d = info.get("upload_date")
    if d:
        return int(datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())
    return None


def _ydl_opts(cfg, extra=None):
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    cookies = cfg.get("platforms.cookies_file")
    if cookies:
        opts["cookiefile"] = os.path.expanduser(cookies)
    opts.update(extra or {})
    return opts


def scan_platforms(cfg, since_ts: int):
    import yt_dlp

    out, errors = [], []
    per_channel = int(cfg.get("platforms.max_items_per_channel", 10))
    for url in cfg.get("platforms.channels", []) or []:
        src = _source_name(url)
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(cfg, {"extract_flat": "in_playlist",
                                                  "playlistend": per_channel})) as ydl:
                listing = ydl.extract_info(url, download=False)
        except Exception as e:  # keep going with other channels
            log.warning("Could not list %s: %s", url, e)
            errors.append(f"{url}: {e}")
            continue
        for entry in (listing or {}).get("entries") or []:
            if not entry:
                continue
            vurl = entry.get("url") or entry.get("webpage_url")
            if not vurl:
                continue
            ts = _ts(entry)
            if ts is None:  # flat listings often lack dates: resolve this one item fully
                try:
                    with yt_dlp.YoutubeDL(_ydl_opts(cfg)) as ydl:
                        entry = ydl.extract_info(vurl, download=False) or entry
                    ts = _ts(entry)
                except Exception as e:
                    log.warning("Could not read %s: %s", vurl, e)
                    continue
            if ts is not None and ts < since_ts:
                continue
            live_status = entry.get("live_status")
            if live_status in ("is_live", "is_upcoming", "post_live"):
                continue  # still live / VOD not processed yet; try next hour
            if src == "youtube" and live_status not in (None, "was_live"):
                continue  # the /streams tab can contain premieres/uploads
            dur = entry.get("duration")
            out.append(Candidate(
                key=f"{src}:{entry.get('id')}", source=src,
                title=entry.get("title") or "Untitled stream",
                description=entry.get("description") or "",
                started_at=ts, finished_at=(ts + int(dur)) if ts and dur else None,
                duration_s=int(dur) if dur else None, urls=[vurl],
            ))
    return out, errors


def download_audio_with_ytdlp(cfg, urls: list[str], work_dir: str, stem: str) -> str:
    """Try each URL in order; return the path of the downloaded audio file."""
    import yt_dlp

    from .models import download_order, order_urls
    errors = []
    for url in order_urls(urls, download_order(cfg)):
        tmpl = os.path.join(work_dir, f"{stem}.%(ext)s")
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(cfg, {
                "format": "bestaudio/best", "outtmpl": tmpl, "retries": 5,
                "fragment_retries": 10, "overwrites": True,
            })) as ydl:
                ydl.download([url])
            files = [f for f in glob.glob(os.path.join(work_dir, f"{stem}.*"))
                     if not f.endswith((".part", ".ytdl", ".mp3"))]
            if files:
                return max(files, key=os.path.getmtime)
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("yt-dlp could not fetch audio from any URL: " + " | ".join(errors or urls))
