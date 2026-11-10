"""What happens to downloaded and converted audio once an episode is handled.

general.keep_files:
  nothing                  delete everything (cleanest; the default)
  originals                keep only the downloaded source audio
  originals_and_converted  keep both
  converted                keep only the MP3 that was sent to Libsyn

Each episode is worked on in its own folder:
  <work_dir>/<episode key>/original/    downloaded audio
  <work_dir>/<episode key>/converted/   the MP3
Kept files are moved to general.keep_dir (if set) under originals/ and converted/,
otherwise they stay in the work folder. Partial/temporary files are always removed.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import time

CHOICES = {
    "nothing": (False, False),
    "originals": (True, False),
    "originals_and_converted": (True, True),
    "converted": (False, True),
}
ALIASES = {
    "none": "nothing", "delete": "nothing", "clean": "nothing", "discard": "nothing",
    "original": "originals", "originals_only": "originals", "only_originals": "originals",
    "source": "originals",
    "both": "originals_and_converted", "all": "originals_and_converted",
    "originals+converted": "originals_and_converted",
    "originals_converted": "originals_and_converted",
    "converted_only": "converted", "only_converted": "converted", "mp3": "converted",
    "mp3s": "converted",
}
TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".tmp.mp3", ".temp")


def keep_policy(cfg) -> tuple[str, bool, bool]:
    """Returns (name, keep_originals, keep_converted)."""
    raw = str(cfg.get("general.keep_files", "nothing")).strip().lower()
    # "Originals + Converted", "originals & converted", "originals and converted" ...
    key = re.sub(r"\s*(\+|&|\band\b)\s*", "+", raw)
    key = re.sub(r"[\s-]+", "_", key)
    key = ALIASES.get(key, key)
    if key not in CHOICES:
        raise ValueError(f"general.keep_files '{raw}' must be one of: "
                         + ", ".join(CHOICES))
    return (key, *CHOICES[key])


def episode_dirs(work_root: str, safe_key: str) -> tuple[str, str, str]:
    base = os.path.join(work_root, safe_key)
    orig, conv = os.path.join(base, "original"), os.path.join(base, "converted")
    os.makedirs(orig, exist_ok=True)
    os.makedirs(conv, exist_ok=True)
    return base, orig, conv


def _remove_temp(folder: str):
    for p in glob.glob(os.path.join(folder, "**", "*"), recursive=True):
        if os.path.isfile(p) and p.endswith(TEMP_SUFFIXES):
            os.remove(p)


def _move_all(src_dir: str, dest_dir: str) -> list[str]:
    os.makedirs(dest_dir, exist_ok=True)
    moved = []
    for name in os.listdir(src_dir):
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            dest = os.path.join(dest_dir, f"{stem}-{int(time.time())}{ext}")
        shutil.move(os.path.join(src_dir, name), dest)
        moved.append(dest)
    return moved


def finalize(cfg, base: str, keep_originals: bool, keep_converted: bool) -> dict:
    """Apply the keep policy to one episode folder. Returns kept file paths."""
    kept = {"originals": [], "converted": []}
    if not os.path.isdir(base):
        return kept
    _remove_temp(base)
    keep_dir = cfg.get("general.keep_dir")
    for sub, keep in (("original", keep_originals), ("converted", keep_converted)):
        folder = os.path.join(base, sub)
        if not os.path.isdir(folder):
            continue
        label = "originals" if sub == "original" else "converted"
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
        elif keep_dir:
            kept[label] = _move_all(folder, os.path.join(os.path.expanduser(keep_dir), label))
            shutil.rmtree(folder, ignore_errors=True)
        else:
            kept[label] = [os.path.join(folder, f) for f in os.listdir(folder)]
    # remove anything else left behind and the folder itself if now empty
    for name in os.listdir(base):
        p = os.path.join(base, name)
        if name not in ("original", "converted"):
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        elif os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)
    if not os.listdir(base):
        os.rmdir(base)
    return kept


def expire(folders: list[str], keep_days: float):
    """Delete kept files older than keep_days (0 = keep forever)."""
    if not keep_days or keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for p in glob.glob(os.path.join(folder, "**", "*"), recursive=True):
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
        # prune empty folders (deepest first), but keep the top folder
        for d in sorted(glob.glob(os.path.join(folder, "**", ""), recursive=True), reverse=True):
            d = d.rstrip(os.sep)
            if d != folder.rstrip(os.sep) and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
