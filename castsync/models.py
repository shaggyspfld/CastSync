from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional


def normalize_title(title: str) -> str:
    """Loose comparison key so 'Ep 42: Big News!' matches 'ep 42 - big news'."""
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    t = t.lower()
    t = re.sub(r"\[?\(?\b(live|livestream|live stream|stream)\b\)?\]?", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slugify(title: str, maxlen: int = 80) -> str:
    s = normalize_title(title).replace(" ", "-")
    return s[:maxlen].strip("-") or "episode"


@dataclass
class Candidate:
    key: str                         # unique id, e.g. "restream:<uuid>"
    source: str                      # restream | youtube | twitch | facebook | ytdlp
    title: str
    description: str = ""
    started_at: Optional[int] = None  # unix seconds
    finished_at: Optional[int] = None
    duration_s: Optional[int] = None
    urls: list[str] = field(default_factory=list)  # platform URLs usable by yt-dlp
    # Callable that downloads the best audio to the given dir and returns the path,
    # or None if this source has no direct audio (yt-dlp on `urls` is then used).
    fetch_audio: Optional[Callable[[str], str]] = None
    # Callable returning a short description of where the audio would come from,
    # without downloading anything (used by --dry-run).
    preview_audio: Optional[Callable[[], str]] = None

    @property
    def norm_title(self) -> str:
        return normalize_title(self.title)


PLATFORM_HOSTS = {
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "fb.watch"),
    "twitch": ("twitch.tv",),
}
DEFAULT_DOWNLOAD_ORDER = ("youtube", "facebook", "twitch")
_ALIASES = {"yt": "youtube", "fb": "facebook", "meta": "facebook"}


def platform_key(url: str) -> str | None:
    low = (url or "").lower()
    for key, hosts in PLATFORM_HOSTS.items():
        if any(h in low for h in hosts):
            return key
    return None


def download_order(cfg=None) -> tuple[str, ...]:
    """platforms.download_order from config, validated. Default: youtube, facebook, twitch."""
    raw = cfg.get("platforms.download_order") if cfg is not None else None
    if not raw:
        return DEFAULT_DOWNLOAD_ORDER
    if isinstance(raw, str):
        raw = [p for p in raw.replace(">", ",").split(",")]
    out = []
    for item in raw:
        key = _ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
        if key not in PLATFORM_HOSTS:
            raise ValueError(f"platforms.download_order: '{item}' isn't a platform castsync knows. "
                             f"Use any of: {', '.join(PLATFORM_HOSTS)}")
        if key not in out:
            out.append(key)
    return tuple(out)


def order_urls(urls: list[str], order=DEFAULT_DOWNLOAD_ORDER) -> list[str]:
    """Replay links sorted by the preferred platform order, duplicates removed.
    Platforms left out of `order` are not tried; links to unknown sites go last."""
    rank = {k: i for i, k in enumerate(order)}
    seen, keyed = set(), []
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        k = platform_key(u)
        if k is None:
            keyed.append((len(order), u))
        elif k in rank:
            keyed.append((rank[k], u))
    return [u for _, u in sorted(keyed, key=lambda t: t[0])]


def platform_name(url: str) -> str:
    low = url.lower()
    if "youtu" in low:
        return "YouTube"
    if "twitch" in low:
        return "Twitch"
    if "facebook" in low or "fb.watch" in low:
        return "Facebook"
    return "the platform"
