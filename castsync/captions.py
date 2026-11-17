"""Caption export: save each finished YouTube stream's captions to a folder.

Used by the P.T.R. Radio movie site (movies.ptrradio.com), which reads the folder to find
where each movie review starts in an episode. Separate from the podcast upload: a stream
gets its captions exported whether or not it's posted to Libsyn/Transistor.

For each video the folder gets two files, written atomically (temp file, then rename):
  <id>.json   what the video is: id, title, url, started_at (UTC ISO), duration_s, ...
  <id>.vtt    the captions (WebVTT): the hosts' own captions if any, else YouTube's
              automatic ones. Missing until YouTube has made them (often a few hours
              after a stream ends); castsync keeps checking for captions.retry_hours.

No YouTube API key or sign-in is needed: captions come through yt-dlp, like the audio.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from .models import Candidate

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS captions (
    video_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    started_at  INTEGER,
    status      TEXT NOT NULL,      -- exported | waiting | none | failed
    kind        TEXT,               -- manual | auto
    lang        TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    detail      TEXT
);
"""

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def enabled(cfg) -> bool:
    return bool(cfg.get("captions.enabled", False))


def settings(cfg) -> dict:
    """Validated captions settings (raises ValueError, like the other check_* helpers)."""
    folder = cfg.get("captions.folder")
    if enabled(cfg) and not folder:
        raise ValueError("captions.enabled is true but captions.folder is not set")
    langs = cfg.get("captions.languages", ["en", "en-US", "en-GB"]) or []
    if isinstance(langs, str):
        langs = [langs]
    s = {
        "folder": os.path.expanduser(str(folder)) if folder else "",
        "languages": [str(x) for x in langs],
        "min_minutes": float(cfg.get("captions.min_duration_minutes", 30)),
        "retry_hours": float(cfg.get("captions.retry_hours", 48)),
        "per_run": int(cfg.get("captions.max_per_run", 10)),
        "channels": list(cfg.get("captions.channels") or []),
        "marker": str(cfg.get("captions.marker_file") or ""),
    }
    if s["per_run"] < 1:
        raise ValueError("captions.max_per_run must be at least 1")
    return s


def youtube_id(url: str) -> str | None:
    """The 11-character video id from any YouTube watch/live/youtu.be/shorts link."""
    m = re.search(r"(?:youtu\.be/|[?&]v=|/(?:live|shorts|embed|v)/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url or "")
    return m.group(1) if m else None


def channels(cfg) -> list[str]:
    """Where to list videos for a backfill: captions.channels, else the YouTube entries
    of platforms.channels."""
    own = settings(cfg)["channels"]
    if own:
        return own
    return [u for u in (cfg.get("platforms.channels") or []) if "youtu" in u.lower()]


class CaptionState:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def get(self, vid):
        return self.db.execute("SELECT * FROM captions WHERE video_id=?", (vid,)).fetchone()

    def put(self, vid, title, started_at, status, kind=None, lang=None, detail=None, bump=False):
        prev = self.get(vid)
        now = int(time.time())
        self.db.execute(
            """INSERT INTO captions(video_id,title,started_at,status,kind,lang,attempts,first_seen,updated_at,detail)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
                   started_at=COALESCE(excluded.started_at, captions.started_at), status=excluded.status,
                   kind=excluded.kind, lang=excluded.lang, attempts=excluded.attempts,
                   updated_at=excluded.updated_at, detail=excluded.detail""",
            (vid, title, started_at, status, kind, lang,
             (prev["attempts"] if prev else 0) + (1 if bump else 0),
             prev["first_seen"] if prev else now, now, detail))
        self.db.commit()

    def counts(self) -> dict:
        return {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM captions GROUP BY status")}


# ----------------------------------------------------------------- choosing --

def pick_track(info: dict, languages: list[str]):
    """(kind, lang, url) of the best WebVTT track: the hosts' own captions first, in the
    order of `languages`, then YouTube's automatic captions (its original-language track,
    "<lang>-orig", before machine translations). None when there are none."""
    def vtt(formats):
        for f in formats or []:
            if f.get("ext") == "vtt" and f.get("url"):
                return f["url"]
        return None

    def match(pool, wanted):
        for lang in wanted:
            for key in pool:
                if key == lang or (lang.lower() == key.lower()):
                    url = vtt(pool[key])
                    if url:
                        return key, url
        return None

    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    hit = match(manual, languages)
    if hit:
        return "manual", hit[0], hit[1]
    original = info.get("language") or ""
    wanted_auto = [f"{l}-orig" for l in languages] + languages
    if original:
        wanted_auto = [f"{original}-orig", original] + wanted_auto
    hit = match(auto, wanted_auto)
    if hit:
        return "auto", hit[0], hit[1]
    return None


# ------------------------------------------------------------------ writing --

def _write_atomic(path: str, data: bytes) -> None:
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def manifest(vid: str, title: str, started_at, duration_s, url: str, kind=None, lang=None) -> dict:
    return {
        "id": vid,
        "title": title,
        "url": url or f"https://www.youtube.com/watch?v={vid}",
        "started_at": (datetime.fromtimestamp(started_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                       if started_at else None),
        "duration_s": int(duration_s) if duration_s else None,
        "captions": kind,          # manual | auto | null (not available yet)
        "language": lang,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exported_by": "castsync",
    }


def write_video(folder: str, meta: dict, vtt: bytes | None) -> None:
    """Captions first, then the manifest, so a reader that sees a manifest saying
    captions are there also finds the .vtt."""
    if vtt is not None:
        _write_atomic(os.path.join(folder, meta["id"] + ".vtt"), vtt)
    _write_atomic(os.path.join(folder, meta["id"] + ".json"),
                  (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# ------------------------------------------------------------------ fetching --

def fetch_info(cfg, url: str) -> dict:
    import yt_dlp
    from .platforms import _ydl_opts
    with yt_dlp.YoutubeDL(_ydl_opts(cfg, {"skip_download": True})) as ydl:
        return ydl.extract_info(url, download=False) or {}


def fetch_text(cfg, url: str) -> bytes:
    import yt_dlp
    from .platforms import _ydl_opts
    with yt_dlp.YoutubeDL(_ydl_opts(cfg)) as ydl:
        return ydl.urlopen(url).read()


def export_one(cfg, st: CaptionState, s: dict, vid: str, title: str, started_at, duration_s,
               url: str, dry_run=False, info_fn=None, text_fn=None) -> tuple[str, str]:
    """Returns (status, message). Never raises for a single video's problems."""
    info_fn = info_fn or (lambda u: fetch_info(cfg, u))
    text_fn = text_fn or (lambda u: fetch_text(cfg, u))
    watch = f"https://www.youtube.com/watch?v={vid}"
    if dry_run:
        return "would_check", "would fetch captions"
    try:
        info = info_fn(watch)
    except Exception as e:
        st.put(vid, title, started_at, "failed", detail=str(e)[:500], bump=True)
        return "failed", f"couldn't read the video: {e}"
    title = info.get("title") or title
    started_at = (info.get("release_timestamp") or info.get("timestamp") or started_at)
    duration_s = info.get("duration") or duration_s
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        st.put(vid, title, started_at, "waiting", detail="still live or processing")
        return "waiting", "still live or being processed"
    track = pick_track(info, s["languages"])
    if not track:
        age_h = (time.time() - (started_at or time.time()) - (duration_s or 0)) / 3600
        meta = manifest(vid, title, started_at, duration_s, watch)
        write_video(s["folder"], meta, None)
        if age_h < s["retry_hours"]:
            st.put(vid, title, started_at, "waiting", detail="no captions yet", bump=True)
            return "waiting", "no captions yet; will check again next run"
        st.put(vid, title, started_at, "none", detail=f"no captions after {s['retry_hours']:.0f} h", bump=True)
        return "none", "no captions (gave up waiting)"
    kind, lang, turl = track
    try:
        data = text_fn(turl)
    except Exception as e:
        st.put(vid, title, started_at, "failed", detail=str(e)[:500], bump=True)
        return "failed", f"couldn't download the captions: {e}"
    if not data.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"WEBVTT"):
        st.put(vid, title, started_at, "failed", detail="download was not WebVTT", bump=True)
        return "failed", "YouTube returned something that isn't WebVTT"
    write_video(s["folder"], manifest(vid, title, started_at, duration_s, watch, kind, lang), data)
    st.put(vid, title, started_at, "exported", kind=kind, lang=lang, detail=f"{len(data)} bytes")
    what = "hosts' captions" if kind == "manual" else "automatic captions"
    return "exported", f"{what} ({lang})"


MAX_ATTEMPTS = 5


def due(st: CaptionState, s: dict, vid: str, now: float, max_attempts: int = MAX_ATTEMPTS) -> bool:
    row = st.get(vid)
    if os.path.exists(os.path.join(s["folder"], vid + ".vtt")):
        return False
    if row is None:
        return True
    if row["status"] in ("exported", "none"):
        return False
    if row["status"] == "failed" and row["attempts"] >= max_attempts:
        return False
    # waiting / failed: at most once an hour
    return now - row["updated_at"] >= 3300


def videos_from_candidates(cands: list[Candidate]) -> list[dict]:
    """One entry per YouTube video among this run's streams (Restream events carry
    their YouTube link; the platform scan finds them directly)."""
    seen, out = set(), []
    for c in sorted(cands, key=lambda c: -(c.started_at or 0)):
        for u in c.urls:
            vid = youtube_id(u)
            if vid and vid not in seen:
                seen.add(vid)
                out.append({"id": vid, "title": c.title, "started_at": c.started_at,
                            "duration_s": c.duration_s, "url": u})
    return out


def list_channel(cfg, url: str, limit: int | None) -> list[dict]:
    """Every video on a channel tab (e.g. /streams), newest first, without downloading."""
    import yt_dlp
    from .platforms import _ydl_opts, _ts
    opts = {"extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(_ydl_opts(cfg, opts)) as ydl:
        listing = ydl.extract_info(url, download=False) or {}
    out = []
    for e in listing.get("entries") or []:
        if not e or not e.get("id") or not _ID.match(str(e["id"])):
            continue
        out.append({"id": e["id"], "title": e.get("title") or "", "started_at": _ts(e),
                    "duration_s": e.get("duration"), "url": e.get("url") or ""})
    return out


def check_folder(s: dict) -> None:
    """The folder must be there already (castsync never creates it, so an unmounted
    share isn't silently replaced by an empty local folder), and must hold the marker
    file when one is configured."""
    if not os.path.isdir(s["folder"]):
        raise RuntimeError(f"captions.folder {s['folder']} doesn't exist (is the share mounted?)")
    if s["marker"] and not os.path.exists(os.path.join(s["folder"], s["marker"])):
        raise RuntimeError(f"{s['marker']} not found in {s['folder']}: the share isn't mounted there")


def export(cfg, db, videos: list[dict], now: float | None = None, dry_run=False,
           limit: int | None = None, info_fn=None, text_fn=None) -> list[dict]:
    """Export captions for the videos that need it. Returns one line per video touched."""
    s = settings(cfg)
    if not dry_run:
        check_folder(s)
    st = CaptionState(db)
    now = now or time.time()
    done = []
    cap = limit if limit is not None else s["per_run"]
    for v in videos:
        if len(done) >= cap:
            break
        if v.get("duration_s") and v["duration_s"] < s["min_minutes"] * 60:
            continue
        if not due(st, s, v["id"], now):
            continue
        status, msg = export_one(cfg, st, s, v["id"], v.get("title") or "", v.get("started_at"),
                                 v.get("duration_s"), v.get("url") or "", dry_run, info_fn, text_fn)
        row = st.get(v["id"])
        if status == "failed" and row and row["attempts"] >= MAX_ATTEMPTS:
            status, msg = "gave_up", msg + f" [gave up after {row['attempts']} tries]"
        log.info("Captions %s %s: %s", v["id"], status, msg)
        done.append({"id": v["id"], "title": v.get("title") or "", "status": status, "message": msg})
    return done
