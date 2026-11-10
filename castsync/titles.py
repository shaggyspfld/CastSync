"""Episode titles: you control the whole name with episode.title_template.

Placeholders:
  {title}         stream title, after episode.strip_from_title is applied
  {stream_title}  stream title exactly as it was on Restream/YouTube
  {topic}         just the episode's own part of the stream title: the show name,
                  any dates and the separators around them are removed.
                  "PTR Radio (11/2/2026) - we're sooo tired"  ->  "we're sooo tired"
  {show}          general.show_name
  {date}          2026-11-02
  {short_date}    11/2/2026
  {pretty_date}   November 2, 2026
  {yyyymmdd}      20261102  (same format as the date-based episode number)
  {date:FORMAT}   any strftime format, e.g. {date:%m/%d/%y} -> 11/02/26,
                  {date:%b %-d} -> Nov 2, {date:%A} -> Monday
All dates are the day the stream aired (local time).
"""
from __future__ import annotations

import os
import re
import string
from datetime import date, datetime

from .dates import PATTERNS

PLACEHOLDERS = ("title", "stream_title", "topic", "show", "date", "short_date", "pretty_date",
                "yyyymmdd")
# The MP3 file name can also use these:
FILENAME_EXTRA = ("slug", "episode_title")


class EpisodeDate(date):
    """A date that prints as 2026-11-02 but also accepts strftime formats: {date:%m/%d}."""

    def __str__(self):
        return self.isoformat()

    def __format__(self, spec):
        return self.strftime(spec) if spec else self.isoformat()


def _strip_dates(text: str) -> str:
    for _kind, rx in PATTERNS:
        text = rx.sub(" ", text)
    return text


def topic_of(stream_title: str, show_name: str = "") -> str:
    """The episode-specific part of a stream title.

    Removes the show name, any dates (any format castsync recognises), empty
    brackets left behind and the separators (- | : , ~ •) at the ends. Returns ""
    when the title has nothing else in it (e.g. "PTR Radio (11/2/2026)")."""
    t = stream_title or ""
    if show_name:
        t = re.sub(re.escape(show_name), " ", t, flags=re.I)
    t = _strip_dates(t)
    t = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", t)             # () [] {} left empty
    t = re.sub(r"\b(ep(isode)?\.?)\s*#?\s*$", " ", t, flags=re.I)
    seps = " \t-–—|:,~•·/"
    t = re.sub(r"\s+", " ", t).strip(seps)
    # collapse separators left stacked in the middle, e.g. "Live -  - topic"
    t = re.sub(r"\s*([-–—|:•·])\s*(?:[-–—|:•·]\s*)+", r" \1 ", t).strip(seps)
    return t


def check_template(tmpl: str, setting: str = "episode.title_template",
                   extra: tuple = ()) -> None:
    """Raise ValueError with a plain explanation if the template can't be used."""
    allowed = PLACEHOLDERS + tuple(extra)
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(tmpl) if f is not None]
    except ValueError as e:
        raise ValueError(f"{setting} has unbalanced {{ }}: {e}. "
                         "Use {{ and }} for literal braces.") from None
    for f in fields:
        name = f.split(".")[0].split("[")[0]
        if name not in allowed:
            raise ValueError(f"{setting} uses {{{f}}}, which castsync doesn't know. "
                             f"Available: {', '.join('{' + p + '}' for p in allowed)} "
                             "and {date:FORMAT}.")


def check_filename_template(cfg) -> None:
    check_template(cfg.get("episode.filename_template", "{date}-{slug}.mp3"),
                   "episode.filename_template", FILENAME_EXTRA)


def _values(cfg, stream_title: str, started_at: int | None) -> dict:
    dt = datetime.fromtimestamp(started_at) if started_at else datetime.now()
    d = EpisodeDate(dt.year, dt.month, dt.day)
    title = stream_title or ""
    for pat in cfg.get("episode.strip_from_title", []) or []:
        title = re.sub(pat, "", title, flags=re.I).strip(" -|:")
    show = cfg.get("general.show_name", "") or ""
    return dict(title=title, stream_title=stream_title or "", topic=topic_of(title, show),
                show=show, date=d, short_date=f"{d.month}/{d.day}/{d.year}",
                pretty_date=f"{d:%B} {d.day}, {d.year}", yyyymmdd=f"{d:%Y%m%d}")


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def render_filename(cfg, stream_title: str, started_at: int | None, episode_title: str) -> str:
    """MP3 file name from episode.filename_template. Accepts every title placeholder plus
    {slug} (episode title as lowercase-with-dashes) and {episode_title}. Characters that
    aren't allowed in file names (e.g. the / in 11/2/2026) and spaces become '-', and
    repeated hyphens are collapsed."""
    from .models import normalize_title
    tmpl = cfg.get("episode.filename_template", "{date}-{slug}.mp3")
    check_filename_template(cfg)
    v = _values(cfg, stream_title, started_at)
    v["slug"] = re.sub(r"[^a-z0-9]+", "-", normalize_title(episode_title))[:60].strip("-")
    v["episode_title"] = episode_title
    name = tmpl.format(**v)
    name = _UNSAFE.sub("-", name)
    name = re.sub(r"\s+", "-", name.strip())          # no spaces: they become hyphens
    name = re.sub(r"-{2,}", "-", name)                  # "PTR-Radio---topic" -> "PTR-Radio-topic"
    name = re.sub(r"-*\.-*", ".", name).strip(".-") or "episode"
    stem, ext = os.path.splitext(name)
    if ext.lower() != ".mp3":
        stem, ext = name, ".mp3"
    return stem[:150].rstrip(".-") + ext


def render_title(cfg, stream_title: str, started_at: int | None) -> str:
    tmpl = cfg.get("episode.title_template", "{title}")
    check_template(tmpl)
    out = tmpl.format(**_values(cfg, stream_title, started_at))
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\(\s*\)|\[\s*\]", "", out)                      # empty brackets
    out = re.sub(r"\s*([-–—|:•])\s*(?:[-–—|:•]\s*)+", r" \1 ", out)  # stacked separators
    out = out.strip(" -–—|:•,")
    return out or (stream_title or "").strip() or "Untitled episode"
