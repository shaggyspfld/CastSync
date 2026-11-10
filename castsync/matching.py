"""Decides whether a stream is already at the destination (Libsyn or Transistor).

A stream counts as already uploaded when either
  * its title matches an existing episode title (ignoring case/punctuation), or
  * any date found in its names matches any date found in an existing episode's
    names. "Names" are the stream title, the rendered episode title and (by
    default, match_air_date) the stream's air date on the stream side; the
    episode title and its audio file name on the destination side.
"""
from __future__ import annotations

from datetime import date, datetime

from .dates import extract_dates
from .models import normalize_title


class ExistingIndex:
    def __init__(self, date_order: str = "mdy", dest_name: str = "Libsyn"):
        self.date_order = date_order
        self.dest_name = dest_name
        self.titles: dict[str, str] = {}      # normalized title -> display label
        self.dates: dict[date, str] = {}      # date -> display label

    def add(self, label: str, texts: list[str], ref_year: int | None = None,
            extra_dates: set[date] | None = None):
        for t in texts:
            if not t:
                continue
            self.titles.setdefault(normalize_title(t), label)
            for d in extract_dates(t, ref_year, self.date_order):
                self.dates.setdefault(d, label)
        for d in extra_dates or ():
            self.dates.setdefault(d, label)

    def match(self, texts: list[str], ref_year: int | None = None,
              extra_dates: set[date] | None = None) -> str | None:
        """Return a human reason if this looks already uploaded, else None."""
        for t in texts:
            n = normalize_title(t)
            if n and n in self.titles:
                return f"already on {self.dest_name} as '{self.titles[n]}' (same title)"
        wanted = set(extra_dates or ())
        for t in texts:
            wanted |= extract_dates(t, ref_year, self.date_order)
        for d in sorted(wanted):
            if d in self.dates:
                return f"already on {self.dest_name} as '{self.dates[d]}' (same date {d.isoformat()})"
        return None


def air_date(ts: int | None) -> set[date]:
    return {datetime.fromtimestamp(ts).date()} if ts else set()
