"""Find calendar dates written inside episode/stream names, in any common format.

Recognised (case-insensitive, anywhere in the text):
  2026-11-09  2026/11/09  2026.11.09  20261109
  11/09/2026  11-9-26  11.09.2026      (month/day/year by default; date_order: dmy flips it)
  Nov 9 2026  November 9th, 2026  Nov. 9  Sept 9th
  9 Nov 2026  9th of November  9 November
Numeric dates must include a year (so "Part 1/2" is not read as Jan 2).
Month-name dates without a year take the year from `ref_year` (the stream's air
date, or the Libsyn episode's publish date)."""
from __future__ import annotations

import re
from datetime import date

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12,
}
_MON = r"(?P<mon>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YR = r"(?P<year>\d{4}|'?\d{2})"

PATTERNS = [
    ("ymd", re.compile(r"(?<!\d)(?P<year>\d{4})[-/._](?P<m>\d{1,2})[-/._](?P<d>\d{1,2})(?!\d)")),
    ("compact", re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)")),
    ("numeric", re.compile(r"(?<!\d)(?P<a>\d{1,2})[-/._](?P<b>\d{1,2})[-/._](?P<year>\d{4}|\d{2})(?!\d)")),
    ("mdy", re.compile(r"(?<![a-z])" + _MON + r"\s*" + _DAY + r"(?![\d])(?:\s*,?\s*" + _YR + r"(?!\d))?", re.I)),
    ("dmy", re.compile(r"(?<!\d)" + _DAY + r"\s*(?:of\s+)?" + _MON + r"(?![a-z])(?:\s*,?\s*" + _YR + r"(?!\d))?", re.I)),
]


def _year(raw: str | None, ref_year: int | None) -> int | None:
    if not raw:
        return ref_year
    raw = raw.lstrip("'")
    y = int(raw)
    return 2000 + y if y < 100 else y


def _mk(y, m, d):
    try:
        if y and 1990 <= y <= 2100:
            return date(y, m, d)
    except ValueError:
        pass
    return None


def extract_dates(text: str, ref_year: int | None = None, date_order: str = "mdy") -> set[date]:
    found: set[date] = set()
    text = text or ""
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            g = m.groupdict()
            if kind in ("ymd", "compact"):
                dt = _mk(int(g["year"]), int(g["m"]), int(g["d"]))
            elif kind == "numeric":
                a, b = int(g["a"]), int(g["b"])
                mon, day = (b, a) if date_order == "dmy" else (a, b)
                dt = _mk(_year(g["year"], None), mon, day)
            else:
                dt = _mk(_year(g.get("year"), ref_year), MONTHS[g["mon"].lower()], int(g["day"]))
            if dt:
                found.add(dt)
    return found
