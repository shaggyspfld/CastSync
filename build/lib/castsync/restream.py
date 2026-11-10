"""Restream API source: finished events + their stored audio recording.

Docs: https://developers.restream.io  (events history, recordings, download-url).
Restream refresh tokens rotate on every refresh, so the new pair is saved immediately.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse

import requests

from .models import Candidate, download_order, order_urls, platform_name
from .oauth import capture_code
from .state import TokenStore

log = logging.getLogger(__name__)
API = "https://api.restream.io/v2"
TOKEN_URL = "https://api.restream.io/oauth/token"
LOGIN_URL = "https://api.restream.io/login"


class RestreamClient:
    def __init__(self, cfg, state_dir, session: requests.Session | None = None):
        self.client_id = cfg.get("restream.client_id")
        self.client_secret = cfg.get("restream.client_secret")
        self.redirect_uri = cfg.get("restream.redirect_uri", "http://localhost:8765/callback")
        self.tokens = TokenStore(state_dir, "restream")
        self.http = session or requests.Session()

    # ---- auth ----------------------------------------------------------
    def authorize_interactive(self):
        q = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
        })
        code = capture_code(f"{LOGIN_URL}?{q}", self.redirect_uri)
        r = self.http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), data={
            "grant_type": "authorization_code", "redirect_uri": self.redirect_uri, "code": code,
        }, timeout=30)
        r.raise_for_status()
        self.tokens.save(r.json())
        print("Restream authorized; tokens saved to", self.tokens.path)

    def _access_token(self) -> str:
        tok = self.tokens.load()
        if not tok:
            raise RuntimeError("Restream not authorized. Run: castsync auth restream")
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        r = self.http.post(TOKEN_URL, auth=(self.client_id, self.client_secret), data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        }, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Restream token refresh failed ({r.status_code}): {r.text[:300]}. "
                               "Re-run: castsync auth restream")
        new = r.json()
        self.tokens.save(new)  # old pair is now invalid; persist new one right away
        return new["access_token"]

    def _get(self, path, **params):
        r = self.http.get(API + path, params=params, timeout=30,
                          headers={"Authorization": f"Bearer {self._access_token()}"})
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self.http.post(API + path, json=body, timeout=30,
                           headers={"Authorization": f"Bearer {self._access_token()}"})
        r.raise_for_status()
        return r.json()

    # ---- data ----------------------------------------------------------
    def finished_events(self, since_ts: int, max_pages: int = 10):
        for page in range(1, max_pages + 1):
            data = self._get("/user/events/history", page=page, limit=25)
            items = data.get("items", [])
            for ev in items:
                ts = ev.get("startedAt") or ev.get("scheduledFor") or 0
                if ts and ts < since_ts:
                    return
                yield ev
            pag = data.get("pagination") or {}
            if not items or page >= int(pag.get("pages_total") or page):
                return

    def recordings(self, event_id: str) -> dict:
        return self._get(f"/user/events/{event_id}/recordings")

    def download_url(self, event_id: str, file_name: str) -> str:
        return self._post(f"/user/events/{event_id}/recordings/download-url",
                          {"fileName": file_name})["downloadUrl"]

    def download(self, url: str, dest: str):
        with self.http.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            os.replace(tmp, dest)


def restream_candidates(cfg, state_dir, since_ts: int, client: RestreamClient | None = None):
    client = client or RestreamClient(cfg, state_dir)
    include_record_only = bool(cfg.get("restream.include_record_only", False))
    out = []
    for ev in client.finished_events(since_ts):
        if ev.get("status") != "finished":
            continue
        if ev.get("isRecordOnly") and not include_record_only:
            continue
        eid = ev["id"]
        urls = order_urls([d.get("externalUrl") for d in ev.get("destinations", [])
                           if d.get("externalUrl")], download_order(cfg))

        def fetch(work_dir, _eid=eid):
            rec = client.recordings(_eid)
            files = rec.get("audio") or rec.get("primaryVideos") or []
            if not files:
                return None  # recording disabled/expired -> caller falls back to yt-dlp
            name = files[0]["fileName"]
            dest = os.path.join(work_dir, f"restream-{_eid}-{name}")
            client.download(client.download_url(_eid, name), dest)
            return dest

        def preview(_eid=eid, _c=None):
            try:
                rec = client.recordings(_eid)
            except Exception as e:
                rec, why = {}, f"couldn't check Restream ({e})"
            else:
                why = "Restream no longer has a recording"
            if rec.get("audio"):
                return "Restream recording (audio track)"
            if rec.get("primaryVideos"):
                return "Restream recording (video; audio taken from it)"
            return why  # caller appends the fallback

        started, finished = ev.get("startedAt"), ev.get("finishedAt")
        out.append(Candidate(
            key=f"restream:{eid}", source="restream", title=ev.get("title") or "Untitled stream",
            description=ev.get("description") or "", started_at=started, finished_at=finished,
            duration_s=(finished - started) if started and finished else None,
            urls=urls, fetch_audio=fetch, preview_audio=preview,
        ))
    return out
