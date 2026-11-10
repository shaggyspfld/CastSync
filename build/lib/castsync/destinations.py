"""Which podcast host episodes go to (config: destination: libsyn | transistor)."""
from __future__ import annotations

NAMES = {"libsyn": "Libsyn", "transistor": "Transistor"}


def dest_key(cfg) -> str:
    key = str(cfg.get("destination", "libsyn")).strip().lower().replace(".fm", "")
    if key not in NAMES:
        raise ValueError(f"destination '{key}' must be one of: {', '.join(NAMES)}")
    return key


def dest_name(cfg) -> str:
    return NAMES[dest_key(cfg)]


def check_settings(cfg) -> None:
    """Fail fast (before any download) when the chosen destination isn't configured."""
    key = dest_key(cfg)
    if key == "transistor":
        missing = [k for k in ("api_key", "show_id") if not cfg.get(f"transistor.{k}")]
        if missing:
            raise ValueError("transistor." + " and transistor.".join(missing) + " not set. "
                             "Get the API key from the Transistor dashboard (Account > API Access), "
                             "then run `castsync shows` to find the show id.")


def existing_items(cfg) -> list[dict]:
    """Episodes already at the destination (title, file, year, pub_ts, bytes)."""
    key = dest_key(cfg)
    if key == "transistor":
        from .transistor import TransistorClient
        return TransistorClient(cfg).episodes()
    from .libsyn import feed_items
    feed = cfg.get("libsyn.feed_url")
    return feed_items(feed) if feed else []


def make_publisher(cfg, state_dir):
    """Object with .publish(mp3_path, title, description) -> (ref, summary)."""
    if dest_key(cfg) == "transistor":
        from .transistor import TransistorClient
        return TransistorClient(cfg)
    from .libsyn import LibsynPublisher
    return LibsynPublisher(cfg, state_dir)


def quota_prefix(cfg) -> str:
    """Config section holding monthly_limit_mb & friends for the chosen destination."""
    return dest_key(cfg)
