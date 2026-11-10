"""Monthly upload limit of the destination host (Libsyn has one; Transistor doesn't).
Settings live under the destination's section: libsyn.monthly_limit_mb, or
transistor.monthly_limit_mb if you ever want a self-imposed cap there.

Libsyn plans allow a fixed amount of NEW uploads per calendar month; the quota
resets on the 1st. castsync counts, for the current period:
  * every file castsync itself uploaded (exact sizes, kept in its database), plus
  * episodes in your RSS feed published this period that castsync didn't upload
    (manual uploads), using the file size the feed reports.
An episode that would push the total over the limit (minus a safety margin) is
held and uploaded automatically after the next reset.
"""
from __future__ import annotations

import calendar
import os
from datetime import datetime, timedelta

MB = 1_000_000  # counted in decimal megabytes - the stricter reading of "MB"


def _period(now: datetime, reset_day: int, delay_h: float = 0) -> tuple[datetime, datetime]:
    """Current quota period [start, next_reset). The start is pushed `delay_h` hours
    past local midnight so we never count on the reset before Libsyn has done it."""
    def at(year, month):
        day = min(reset_day, calendar.monthrange(year, month)[1])
        return datetime(year, month, day) + timedelta(hours=delay_h)

    start = at(now.year, now.month)
    if now < start:
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = at(y, m)
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, at(y, m)


class Quota:
    def __init__(self, cfg, state, existing_items: list[dict], now: float):
        from .destinations import dest_key, dest_name
        p = dest_key(cfg)
        self.dest, self.name = p, dest_name(cfg)
        limit = cfg.get(f"{p}.monthly_limit_mb")
        self.limit = int(float(limit) * MB) if limit not in (None, "", 0) else None
        self.margin = int(float(cfg.get(f"{p}.quota_safety_mb", 5)) * MB)
        delay = float(cfg.get(f"{p}.quota_reset_delay_hours", 6))
        self.start, self.next_reset = _period(
            datetime.fromtimestamp(now), int(cfg.get(f"{p}.quota_reset_day", 1)), delay)
        ours = state.uploads_since((self.start - timedelta(hours=delay)).timestamp())
        our_files = {os.path.basename(r["filename"]) for r in ours}
        self.castsync_bytes = sum(r["bytes"] for r in ours)
        self.manual_bytes = 0
        start_ts = (self.start - timedelta(hours=delay)).timestamp()
        if cfg.get(f"{p}.count_feed_uploads", True):
            self.manual_bytes = sum(i.get("bytes") or 0 for i in existing_items
                                    if (i.get("pub_ts") or 0) >= start_ts
                                    and i.get("file") not in our_files)
        self.extra_bytes = int(float(cfg.get(f"{p}.quota_already_used_mb", 0) or 0) * MB)
        self.added = 0

    @property
    def used(self) -> int:
        return self.castsync_bytes + self.manual_bytes + self.extra_bytes + self.added

    @property
    def remaining(self) -> float:
        if self.limit is None:
            return float("inf")
        return max(0, self.limit - self.margin - self.used)

    def fits(self, nbytes: int) -> bool:
        return nbytes <= self.remaining

    def add(self, nbytes: int):
        self.added += nbytes

    def reset_text(self) -> str:
        return self.next_reset.strftime("%b %-d, %-I %p").replace(" 12 AM", " midnight")

    def summary(self) -> str:
        used = self.used / MB
        if self.limit is None:
            if self.dest == "transistor":
                return f"Transistor: no upload limit ({used:.1f} MB uploaded by castsync this month)"
            return (f"{self.name} uploads this month: {used:.1f} MB "
                    f"(no monthly limit set - add {self.dest}.monthly_limit_mb)")
        pct = 100 * self.used / self.limit
        return (f"{self.name} monthly upload: {used:.1f} of {self.limit / MB:.0f} MB used "
                f"({pct:.0f}%), {self.remaining / MB:.1f} MB available; resets {self.reset_text()}")


def estimate_bytes(duration_s, audio: dict) -> int | None:
    """Size of the MP3 we'd make (CBR only): duration x bitrate, +1% for tags/framing."""
    if not duration_s or audio.get("mode") != "cbr":
        return None
    return int(duration_s * audio["bitrate"] * 1000 / 8 * 1.01)
