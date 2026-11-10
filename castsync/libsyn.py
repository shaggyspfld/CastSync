"""Libsyn side: what episodes already exist, and how to publish a new one.

Existence check uses the show's public RSS feed (no auth) plus castsync's own
history, so drafts castsync created are never duplicated.

Publish modes (libsyn.mode):
  ftp  - upload the MP3 to your Libsyn FTP dropbox. The email tells you it is
         waiting there. Needs only your Libsyn login. Safest starting point.
  api  - FTP upload, then create the episode with the Libsyn API
         (POST https://api.libsyn.com/post) pointing at the uploaded file.
         Needs a Libsyn API client_id/secret (request from Libsyn support).
"""
from __future__ import annotations

import ftplib
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from .models import normalize_title, slugify
from .state import TokenStore

log = logging.getLogger(__name__)
API = "https://api.libsyn.com"


def feed_items(feed_url: str, session=None) -> list[dict]:
    """Every episode in the feed: title, audio file name, size and publish time."""
    from email.utils import parsedate_to_datetime

    http = session or requests
    r = http.get(feed_url, timeout=60, headers={"User-Agent": "castsync/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for item in root.iter("item"):
        t = item.find("title")
        enc = item.find("enclosure")
        pub = item.find("pubDate")
        year = pub_ts = None
        if pub is not None and pub.text:
            try:
                dt = parsedate_to_datetime(pub.text)
                year, pub_ts = dt.year, int(dt.timestamp())
            except (TypeError, ValueError):
                pass
        fname, size = "", 0
        if enc is not None and enc.get("url"):
            fname = os.path.basename(enc.get("url").split("?")[0])
            try:
                size = int(enc.get("length") or 0)
            except ValueError:
                size = 0
        items.append({"title": (t.text or "") if t is not None else "", "file": fname,
                      "year": year, "pub_ts": pub_ts, "bytes": size})
    return items


def feed_titles(feed_url: str, session=None) -> set[str]:
    return {normalize_title(i["title"]) for i in feed_items(feed_url, session) if i["title"]}


class LibsynPublisher:
    def __init__(self, cfg, state_dir, session: requests.Session | None = None):
        self.cfg = cfg
        self.mode = cfg.get("libsyn.mode", "ftp")
        self.http = session or requests.Session()
        self.tokens = TokenStore(state_dir, "libsyn")

    # ---- FTP -----------------------------------------------------------
    def ftp_upload(self, path: str) -> str:
        host = self.cfg.get("libsyn.ftp.host", "ftp.libsyn.com")
        user = self.cfg.get("libsyn.ftp.username")
        pw = self.cfg.get("libsyn.ftp.password")
        if not user or not pw:
            raise RuntimeError("libsyn.ftp.username / password not configured")
        use_tls = bool(self.cfg.get("libsyn.ftp.tls", False))
        ftp = ftplib.FTP_TLS(host, timeout=120) if use_tls else ftplib.FTP(host, timeout=120)
        try:
            ftp.login(user, pw)
            if use_tls:
                ftp.prot_p()
            ftp.set_pasv(True)
            remote_dir = self.cfg.get("libsyn.ftp.directory")
            if remote_dir:
                ftp.cwd(remote_dir)
            name = os.path.basename(path)
            with open(path, "rb") as fh:
                ftp.storbinary(f"STOR {name}", fh, blocksize=1 << 20)
            return name
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    # ---- API auth ------------------------------------------------------
    def _token_request(self, body: dict) -> dict:
        body = {"client_id": self.cfg.get("libsyn.api.client_id"),
                "client_secret": self.cfg.get("libsyn.api.client_secret"), **body}
        r = self.http.post(f"{API}/oauth", json=body, timeout=30,
                           headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"Libsyn auth failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def authorize_password(self, username: str, password: str):
        self.tokens.save(self._token_request(
            {"grant_type": "password", "username": username, "password": password}))
        print("Libsyn authorized; tokens saved to", self.tokens.path)

    def _access_token(self) -> str:
        tok = self.tokens.load()
        if not tok:
            user = self.cfg.get("libsyn.api.username")
            pw = self.cfg.get("libsyn.api.password")
            if user and pw:
                tok = self._token_request({"grant_type": "password", "username": user,
                                           "password": pw})
                self.tokens.save(tok)
                tok = self.tokens.load()
            else:
                raise RuntimeError("Libsyn API not authorized. Run: castsync auth libsyn")
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        new = self._token_request({"grant_type": "refresh_token",
                                   "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])
        self.tokens.save(new)
        return new["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}

    # ---- API publish ---------------------------------------------------
    def find_ftp_content_id(self, filename: str, attempts: int = 10, wait: int = 30) -> str:
        show_id = self.cfg.get("libsyn.api.show_id")
        for i in range(attempts):
            r = self.http.get(f"{API}/ftp-unreleased/{show_id}", headers=self._headers(), timeout=30)
            r.raise_for_status()
            found = _find_file_id(r.json(), filename)
            if found:
                return str(found)
            time.sleep(wait)  # Libsyn ingests dropbox files asynchronously
        raise RuntimeError(f"Uploaded file {filename} did not appear in Libsyn ftp-unreleased")

    def create_post(self, title: str, body_html: str, ftp_name: str) -> str:
        show_id = self.cfg.get("libsyn.api.show_id")
        content_id = self.find_ftp_content_id(ftp_name)
        tmpl = self.cfg.get("libsyn.api.primary_content_template", "libsyn-ftp-{content_id}")
        field = self.cfg.get("libsyn.api.primary_content_field", "primary_content_url")
        fmt = self.cfg.get("libsyn.api.release_date_format", "%Y-%m-%d %H:%M:%S")
        form = {
            "show_id": str(show_id),
            "item_title": title,
            "item_slug": slugify(title),
            "item_body": body_html,
            "is_draft": "true" if self.cfg.get("libsyn.publish_as_draft", True) else "false",
            "release_date": datetime.now(timezone.utc).strftime(fmt),
            field: tmpl.format(content_id=content_id, filename=ftp_name),
        }
        for k, v in (self.cfg.get("libsyn.api.extra_fields") or {}).items():
            form[k] = str(v)
        # multipart/form-data, as in Libsyn's "How to Publish Episodes" guide
        r = self.http.post(f"{API}/post", headers=self._headers(),
                           files={k: (None, v) for k, v in form.items()}, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"Libsyn create post failed ({r.status_code}): {r.text[:500]}")
        data = r.json() if r.content else {}
        return str(_find_key(data, ("item_id", "id", "post_id")) or "created")

    # ---- entry point ---------------------------------------------------
    def publish(self, mp3_path: str, title: str, description: str,
                started_at: int | None = None) -> tuple[str, str]:
        """Returns (libsyn_ref, human summary)."""
        ftp_name = self.ftp_upload(mp3_path)
        if self.mode == "ftp":
            return (f"ftp:{ftp_name}",
                    f"Uploaded {ftp_name} to Libsyn FTP dropbox (create the episode in Libsyn)")
        body = description.replace("\n", "<br>\n") if description else title
        ref = self.create_post(title, body, ftp_name)
        state = "draft" if self.cfg.get("libsyn.publish_as_draft", True) else "published"
        return f"post:{ref}", f"Created Libsyn episode {ref} ({state})"


def _find_key(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            hit = _find_key(v, keys)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, keys)
            if hit:
                return hit
    return None


def _find_file_id(obj, filename):
    """Walk an arbitrary JSON response for the record whose fields mention filename."""
    if isinstance(obj, dict):
        if any(isinstance(v, str) and filename in v for v in obj.values()):
            return _find_key(obj, ("content_id", "id", "ftp_id", "file_id"))
        for v in obj.values():
            hit = _find_file_id(v, filename)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_file_id(v, filename)
            if hit:
                return hit
    return None
