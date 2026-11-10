"""Transistor.fm destination (API key auth, every plan).

Flow per episode (https://developers.transistor.fm):
  1. GET  /v1/episodes/authorize_upload?filename=...  -> upload_url, content_type, audio_url
  2. PUT  the MP3 to upload_url with that Content-Type (URL expires after ~10 min)
  3. POST /v1/episodes  episode[show_id|title|summary|description|audio_url] -> draft
  4. PATCH /v1/episodes/{id}/publish  episode[status]=published   (unless left as draft)

Existing episodes (published, scheduled AND drafts) come from GET /v1/episodes, so
duplicate protection also sees drafts that aren't in the public feed yet.
API limit: 10 requests per 10 seconds; castsync spaces requests and backs off on 429.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)
API = "https://api.transistor.fm/v1"


def _plain(text: str, limit: int) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0] + "…"


def batch_settings(cfg) -> tuple[int, float]:
    """transistor.batch_size (default 10) requests, then wait transistor.batch_pause_seconds
    (default 30). batch_size 0 turns the pause off."""
    try:
        size = int(cfg.get("transistor.batch_size", 10))
        pause = float(cfg.get("transistor.batch_pause_seconds", 30))
    except (TypeError, ValueError):
        raise ValueError("transistor.batch_size and transistor.batch_pause_seconds must be numbers") from None
    if size < 0 or pause < 0:
        raise ValueError("transistor.batch_size and transistor.batch_pause_seconds can't be negative")
    return size, pause


def estimate_seconds(cfg, requests_count: int) -> float:
    size, pause = batch_settings(cfg)
    pauses = (requests_count - 1) // size if size and requests_count > 0 else 0
    return requests_count * TransistorClient.MIN_GAP + pauses * pause


def episode_numbering(cfg, started_at: int | None) -> dict:
    """Extra episode fields from transistor.episode_number / transistor.season.

    episode_number: date (default; YYYYMMDD, e.g. 20261102) | none (don't send) |
                    a template such as "{date:%Y%m%d}" that renders to digits
    season:         leave (default; don't send) | year (e.g. 2026) | a fixed number
    """
    from .titles import EpisodeDate
    dt = datetime.fromtimestamp(started_at) if started_at else datetime.now()
    d = EpisodeDate(dt.year, dt.month, dt.day)
    out = {}
    num = str(cfg.get("transistor.episode_number", "date") or "none").strip()
    if num.lower() not in ("none", "off", "no", "false", ""):
        tmpl = "{date:%Y%m%d}" if num.lower() in ("date", "yyyymmdd") else num
        try:
            value = tmpl.format(date=d)
        except (KeyError, ValueError, IndexError) as e:
            raise ValueError(f"transistor.episode_number '{num}' can't be used ({e}). "
                             "Use none, date, or a template like {date:%Y%m%d}.") from None
        if not value.isdigit():
            raise ValueError(f"transistor.episode_number must give a whole number; '{num}' gives "
                             f"'{value}'. Use none, date, or digits-only formats like {{date:%Y%m%d}}.")
        if int(value) > 2_147_483_647:
            raise ValueError(f"transistor.episode_number '{value}' is too large for Transistor.")
        out["number"] = str(int(value))
    season = str(cfg.get("transistor.season", "leave") or "leave").strip().lower()
    if season in ("year", "yyyy"):
        out["season"] = str(d.year)
    elif season.isdigit():
        out["season"] = str(int(season))
    elif season not in ("leave", "none", "default", ""):
        raise ValueError(f"transistor.season '{season}' must be leave, year, or a number.")
    return out


class TransistorClient:
    MIN_GAP = 1.05  # seconds between calls -> stays under 10 requests / 10 s

    def __init__(self, cfg, session: requests.Session | None = None, sleep=None):
        self.cfg = cfg
        self.key = cfg.get("transistor.api_key")
        self.show_id = str(cfg.get("transistor.show_id") or "")
        self.http = session or requests.Session()
        self.sleep = sleep or (lambda secs: time.sleep(secs))
        self._last = 0.0
        self.batch_size, self.batch_pause = batch_settings(cfg)
        self._in_batch = 0

    def _call(self, method, path, **kw):
        if not self.key:
            raise RuntimeError("transistor.api_key is not set (Transistor dashboard > Account > API Access)")
        if self.batch_size and self._in_batch >= self.batch_size:
            # Pause between batches so bulk commands (renumber, retitle, catching up on
            # many episodes) never hammer Transistor.
            log.info("Pausing %s s after %s Transistor requests", self.batch_pause, self._in_batch)
            if self.batch_pause:
                self.sleep(self.batch_pause)
            self._in_batch = 0
        self._in_batch += 1
        for attempt in range(4):
            wait = self.MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            r = self.http.request(method, API + path, timeout=60,
                                  headers={"x-api-key": self.key, "Accept": "application/json"}, **kw)
            if r.status_code == 429:
                log.info("Transistor rate limit hit; waiting 11 s")
                self.sleep(11)
                continue
            if r.status_code == 401:
                raise RuntimeError("Transistor rejected the API key (401). Check transistor.api_key.")
            if r.status_code >= 400:
                raise RuntimeError(f"Transistor {method} {path} failed ({r.status_code}): {r.text[:400]}")
            return r.json() if r.content else {}
        raise RuntimeError("Transistor kept rate-limiting requests; will retry next run")

    # ---- discovery -----------------------------------------------------
    def shows(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._call("GET", "/shows", params={"pagination[page]": page,
                                                       "pagination[per]": 50})
            for s in data.get("data", []):
                a = s.get("attributes", {})
                out.append({"id": s.get("id"), "title": a.get("title"), "feed": a.get("feed_url")})
            meta = data.get("meta", {})
            if page >= int(meta.get("totalPages") or meta.get("total_pages") or page):
                return out
            page += 1

    def episodes(self, max_pages: int = 40) -> list[dict]:
        """All episodes of the show in the same shape as libsyn.feed_items()."""
        if not self.show_id:
            raise RuntimeError("transistor.show_id is not set (run: castsync shows)")
        items, page = [], 1
        while page <= max_pages:
            data = self._call("GET", "/episodes", params={
                "show_id": self.show_id, "pagination[page]": page, "pagination[per]": 50})
            for e in data.get("data", []):
                a = e.get("attributes", {})
                when = a.get("published_at") or a.get("created_at")
                ts = year = None
                if when:
                    try:
                        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                        ts, year = int(dt.timestamp()), dt.year
                    except ValueError:
                        pass
                media = a.get("media_url") or a.get("audio_url") or ""
                items.append({"title": a.get("title") or "", "year": year, "pub_ts": ts,
                              "file": os.path.basename(media.split("?")[0]), "bytes": 0,
                              "status": a.get("status"), "id": str(e.get("id") or ""),
                              "number": a.get("number"), "season": a.get("season")})
            meta = data.get("meta", {})
            total = int(meta.get("totalPages") or meta.get("total_pages") or page)
            if not data.get("data") or page >= total:
                break
            page += 1
        return items

    def episode(self, ep_id: str) -> dict:
        data = self._call("GET", f"/episodes/{ep_id}")
        return (data.get("data") or {}).get("attributes") or {}

    def update_title(self, ep_id: str, title: str) -> None:
        self.update(ep_id, {"title": title})

    def update(self, ep_id: str, fields: dict) -> None:
        self._call("PATCH", f"/episodes/{ep_id}",
                   data={f"episode[{k}]": str(v) for k, v in fields.items()})

    # ---- publishing ----------------------------------------------------
    def upload(self, path: str) -> str:
        auth = self._call("GET", "/episodes/authorize_upload",
                          params={"filename": os.path.basename(path)})
        attrs = (auth.get("data") or {}).get("attributes") or {}
        upload_url, audio_url = attrs.get("upload_url"), attrs.get("audio_url")
        if not upload_url or not audio_url:
            raise RuntimeError(f"Unexpected authorize_upload response: {str(auth)[:300]}")
        ctype = attrs.get("content_type") or "audio/mpeg"
        with open(path, "rb") as fh:  # pre-signed URL: no API key header
            r = self.http.put(upload_url, data=fh, timeout=3600,
                              headers={"Content-Type": ctype,
                                       "Content-Length": str(os.path.getsize(path))})
        if r.status_code >= 400:
            raise RuntimeError(f"Audio upload to Transistor failed ({r.status_code}): {r.text[:300]}")
        return audio_url

    def publish(self, mp3_path: str, title: str, description: str,
                started_at: int | None = None) -> tuple[str, str]:
        audio_url = self.upload(mp3_path)
        desc_html = (description or title).replace("\n", "<br>\n")
        form = {
            "episode[show_id]": self.show_id,
            "episode[title]": title,
            "episode[summary]": _plain(description or title, 250),
            "episode[description]": desc_html,
            "episode[audio_url]": audio_url,
        }
        for k, v in episode_numbering(self.cfg, started_at).items():
            form[f"episode[{k}]"] = v
        for k, v in (self.cfg.get("transistor.extra_fields") or {}).items():
            form[f"episode[{k}]"] = str(v)  # e.g. season, number, type, explicit, keywords
        created = self._call("POST", "/episodes", data=form)
        ep_id = str((created.get("data") or {}).get("id") or "")
        if not ep_id:
            raise RuntimeError(f"Transistor didn't return an episode id: {str(created)[:300]}")
        status = str(self.cfg.get("transistor.publish", "published")).lower()
        if status in ("published", "publish", "true", "yes"):
            self._call("PATCH", f"/episodes/{ep_id}/publish",
                       data={"episode[status]": "published"})
            return f"transistor:{ep_id}", f"Published on Transistor (episode {ep_id})"
        return f"transistor:{ep_id}", f"Created on Transistor as a draft (episode {ep_id})"
