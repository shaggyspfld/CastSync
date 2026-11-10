"""SQLite-backed record of every stream castsync has seen and what it did with it,
plus a small JSON token store for OAuth tokens (Restream rotates refresh tokens)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    key            TEXT PRIMARY KEY,   -- e.g. restream:<uuid> or yt:<videoid>
    source         TEXT NOT NULL,
    title          TEXT NOT NULL,
    norm_title     TEXT NOT NULL,
    started_at     INTEGER,
    status         TEXT NOT NULL,      -- published | exists_in_libsyn | failed | skipped
    attempts       INTEGER NOT NULL DEFAULT 0,
    libsyn_ref     TEXT,
    mp3_path       TEXT,
    detail         TEXT,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_norm ON episodes(norm_title);
CREATE TABLE IF NOT EXISTS uploads (   -- every file castsync sent to Libsyn
    key         TEXT NOT NULL,
    filename    TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    uploaded_at INTEGER NOT NULL
);
"""


class State:
    def __init__(self, state_dir: Path):
        self.db = sqlite3.connect(str(state_dir / "castsync.db"))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, key: str):
        return self.db.execute("SELECT * FROM episodes WHERE key=?", (key,)).fetchone()

    def title_published(self, norm_title: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM episodes WHERE norm_title=? AND status='published'", (norm_title,)
        ).fetchone()
        return row is not None

    def record(self, key, source, title, norm_title, started_at, status,
               libsyn_ref=None, mp3_path=None, detail=None, bump_attempt=False):
        prev = self.get(key)
        attempts = (prev["attempts"] if prev else 0) + (1 if bump_attempt else 0)
        self.db.execute(
            """INSERT INTO episodes(key,source,title,norm_title,started_at,status,attempts,
                                    libsyn_ref,mp3_path,detail,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET status=excluded.status, attempts=excluded.attempts,
                   libsyn_ref=COALESCE(excluded.libsyn_ref, episodes.libsyn_ref),
                   mp3_path=COALESCE(excluded.mp3_path, episodes.mp3_path),
                   detail=excluded.detail, updated_at=excluded.updated_at""",
            (key, source, title, norm_title, started_at, status, attempts,
             libsyn_ref, mp3_path, detail, int(time.time())),
        )
        self.db.commit()

    def record_upload(self, key, filename, nbytes, when=None):
        self.db.execute("INSERT INTO uploads VALUES(?,?,?,?)",
                        (key, filename, int(nbytes), int(when or time.time())))
        self.db.commit()

    def uploads_since(self, ts):
        return self.db.execute("SELECT * FROM uploads WHERE uploaded_at>=?", (int(ts),)).fetchall()

    def rows_with_status(self, status):
        return self.db.execute("SELECT * FROM episodes WHERE status=?", (status,)).fetchall()

    def published_rows(self):
        return self.db.execute("SELECT * FROM episodes WHERE status='published'").fetchall()

    def recent(self, limit=25):
        return self.db.execute(
            "SELECT * FROM episodes ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()


class TokenStore:
    """Stores OAuth tokens as JSON, written atomically with 0600 permissions."""

    def __init__(self, state_dir: Path, name: str):
        self.path = state_dir / f"{name}_token.json"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def save(self, data: dict) -> None:
        data = dict(data)
        if "expires_in" in data and "expires_at" not in data:
            data["expires_at"] = int(time.time()) + int(data["expires_in"]) - 60
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
