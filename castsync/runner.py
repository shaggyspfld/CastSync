"""One polling pass: find finished streams, decide which are missing from the
destination (Libsyn or Transistor),
make MP3s, publish, report."""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import time
from datetime import datetime

from .audio import audio_settings, describe, probe_duration, to_mp3
from .files import episode_dirs, expire, finalize, keep_policy
from .destinations import check_settings, dest_key, dest_name, existing_items, make_publisher
from .matching import ExistingIndex, air_date
from .models import Candidate, download_order, normalize_title, order_urls, platform_name
from .quota import MB, Quota, estimate_bytes
from .platforms import download_audio_with_ytdlp, scan_platforms
from .report import Report
from .restream import restream_candidates
from .state import State
from .titles import check_template, render_title

log = logging.getLogger(__name__)

SOURCE_PRIORITY = {"restream": 0, "youtube": 1, "twitch": 2, "facebook": 3, "ytdlp": 4}


def episode_title(cfg, c: Candidate) -> str:
    return render_title(cfg, c.title, c.started_at)


def build_index(cfg, dest_items: list[dict], state: State) -> ExistingIndex:
    """Everything that already exists: destination episodes + what castsync uploaded."""
    idx = ExistingIndex(cfg.get("matching.date_order", "mdy"), dest_name(cfg))
    for it in dest_items:
        idx.add(it["title"] or it["file"], [it["title"], it["file"]], it.get("year"))
    use_air = bool(cfg.get("matching.match_air_date", True))
    for row in state.published_rows():
        yr = datetime.fromtimestamp(row["started_at"]).year if row["started_at"] else None
        idx.add(row["title"], [row["title"], os.path.basename(row["mp3_path"] or "")], yr,
                air_date(row["started_at"]) if use_air else None)
    return idx


def select(cfg, candidates: list[Candidate], index: ExistingIndex, state: State,
           now: float, force: bool = False, persist: bool = True):
    """Return (todo, skipped). Decision logic; only side effect is noting skips in state."""
    skip_re = [re.compile(p, re.I) for p in cfg.get("filters.skip_title_regex", []) or []]
    min_dur = float(cfg.get("filters.min_duration_minutes", 10)) * 60
    settle = float(cfg.get("filters.settle_minutes", 20)) * 60
    max_attempts = int(cfg.get("general.max_attempts", 3))
    use_air = bool(cfg.get("matching.match_air_date", True))

    todo, skipped = [], []
    note = state.record if persist else (lambda *a, **k: None)
    for c in sorted(candidates, key=lambda c: (SOURCE_PRIORITY.get(c.source, 9),
                                               -(c.started_at or 0))):
        ep_title = episode_title(cfg, c)
        norm = normalize_title(ep_title)
        texts = [c.title, ep_title]
        year = datetime.fromtimestamp(c.started_at).year if c.started_at else None
        extra = air_date(c.started_at) if use_air else set()
        row = state.get(c.key)
        if row and row["status"] in ("published", "exists_in_libsyn", "ignored") and not force:
            continue  # already handled in an earlier run; stay quiet
        if any(r.search(c.title) for r in skip_re):
            skipped.append({"title": c.title, "reason": "matches skip_title_regex"})
            note(c.key, c.source, ep_title, norm, c.started_at, "ignored",
                 detail="matches skip_title_regex")
            continue
        if c.duration_s is not None and c.duration_s < min_dur:
            skipped.append({"title": c.title, "reason": f"shorter than {min_dur/60:.0f} min"})
            note(c.key, c.source, ep_title, norm, c.started_at, "ignored", detail="too short")
            continue
        allowed = bool(row and row["status"] == "allowed")  # user said "upload anyway"
        reason = None if (force or allowed) else index.match(texts, year, extra)
        if reason:
            if "[this castsync run]" in reason:
                continue  # copy of a stream already picked this run (e.g. YouTube vs Twitch)
            note(c.key, c.source, ep_title, norm, c.started_at, "exists_in_libsyn",
                 detail=reason)
            skipped.append({"title": ep_title, "reason": reason})
            continue
        # From here on this stream "exists" for the rest of the run, so its copies on
        # other platforms (even under a different title) are not uploaded too.
        index.add(f"{ep_title} [this castsync run]", texts, year, extra)
        if c.finished_at and now - c.finished_at < settle:
            continue  # just ended; give platforms time to finish the VOD, check next hour
        if (row and row["status"] in ("failed", "allowed")
                and row["attempts"] >= max_attempts and not force):
            continue  # gave up; reported when it hit the limit
        todo.append(c)
    # Work newest -> oldest. (The loop above ran in source-priority order only so the
    # Restream copy of a stream wins over its YouTube/Twitch copies.)
    todo.sort(key=lambda c: c.started_at or 0, reverse=True)
    return todo, skipped


def _human_size(path):
    n = os.path.getsize(path)
    return f"{n/1e6:.1f} MB"


class QuotaHold(Exception):
    def __init__(self, nbytes, mp3=None, estimated=False):
        super().__init__(f"needs {nbytes / MB:.1f} MB")
        self.nbytes, self.mp3, self.estimated = nbytes, mp3, estimated


def safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def process(cfg, c: Candidate, publisher, work_root: str,
            quota: Quota | None = None) -> dict:
    """Download -> convert -> (upload). Files go in <work_root>/<key>/original|converted;
    the caller applies the keep_files policy afterwards, whatever happened."""
    title = episode_title(cfg, c)
    _, orig_dir, conv_dir = episode_dirs(work_root, safe_key(c.key))
    src = None
    notes = []
    if c.fetch_audio:
        try:
            src = c.fetch_audio(orig_dir)
            if src:
                notes.append("audio from Restream recording")
        except Exception as e:
            notes.append(f"Restream recording unavailable ({e}); falling back to platform VOD")
    if not src:
        if not c.urls:
            raise RuntimeError("No Restream recording and no replay link (YouTube/Twitch/Facebook) "
                               "to download from. Add your channel to platforms.channels so "
                               "castsync can find the replay.")
        urls = order_urls(c.urls, download_order(cfg))
        if not urls:
            raise RuntimeError("No Restream recording, and the replay links found are all on "
                               "platforms left out of platforms.download_order: "
                               + ", ".join(platform_name(u) for u in c.urls))
        src = download_audio_with_ytdlp(cfg, urls, orig_dir, "source")
        notes.append(f"audio via yt-dlp from {platform_name(urls[0])} replay")

    dur = probe_duration(src) or 0
    min_dur = float(cfg.get("filters.min_duration_minutes", 10)) * 60
    if dur and dur < min_dur:
        raise RuntimeError(f"Audio only {dur/60:.1f} min long; below min_duration_minutes")

    date = datetime.fromtimestamp(c.started_at).strftime("%Y-%m-%d") if c.started_at else ""
    fname = (cfg.get("episode.filename_template", "{date}-{slug}.mp3")
             .format(date=date or "undated", slug=re.sub(r"[^a-z0-9]+", "-",
                     normalize_title(title))[:60].strip("-")))
    mp3 = os.path.join(conv_dir, fname)
    to_mp3(src, mp3, cfg, title=title, artist=cfg.get("episode.artist", ""),
           album=cfg.get("general.show_name", ""), date=date)

    result = "not uploaded (--no-upload)"
    ref = None
    size = os.path.getsize(mp3)
    if publisher and quota is not None and not quota.fits(size):
        raise QuotaHold(size, mp3)
    if publisher:
        desc = c.description or ""
        footer = cfg.get("episode.description_footer")
        if footer:
            desc = (desc + "\n\n" + footer).strip()
        ref, result = publisher.publish(mp3, title, desc, started_at=c.started_at)
    return {"title": title, "source": c.source, "mp3": mp3, "size": _human_size(mp3),
            "duration": f"{(probe_duration(mp3) or 0)/60:.0f} min",
            "quality": describe(audio_settings(cfg)), "result": result,
            "ref": ref, "notes": notes, "bytes": size, "file": os.path.basename(mp3),
            "when": datetime.fromtimestamp(c.started_at).strftime("%Y-%m-%d %H:%M")
            if c.started_at else ""}


def gather(cfg, state_dir, since_ts, report: Report) -> list[Candidate]:
    cands: list[Candidate] = []
    if cfg.get("restream.enabled", True):
        try:
            cands += restream_candidates(cfg, state_dir, since_ts)
        except Exception as e:
            report.errors.append(f"Restream: {e}")
    if cfg.get("platforms.channels"):
        try:
            found, errs = scan_platforms(cfg, since_ts)
            cands += found
            report.errors += [f"Platform scan: {e}" for e in errs]
        except Exception as e:
            report.errors.append(f"Platform scan: {e}")
    attach_platform_urls(cands, order=download_order(cfg))
    return cands


def attach_platform_urls(cands: list[Candidate], window_s: int = 3 * 3600, order=None):
    """Give each Restream event the replay URLs found by the platform scan for the same
    stream (started within 3 h), so there's always a fallback if Restream's recording
    is gone - even when Restream didn't record a replay link itself."""
    platform = [c for c in cands if c.source != "restream" and c.started_at and c.urls]
    for c in cands:
        if c.source != "restream" or not c.started_at:
            continue
        extra = [u for p in platform if abs(p.started_at - c.started_at) <= window_s
                 for u in p.urls]
        if extra:
            c.urls = order_urls(c.urls + extra, order or download_order())


def describe_audio_plan(c: Candidate, cfg=None) -> str:
    """Where the audio would come from, without downloading (for --dry-run)."""
    first = None
    if c.preview_audio:
        first = c.preview_audio()
        if first.startswith("Restream recording"):
            return first
    urls = order_urls(c.urls, download_order(cfg))
    if urls:
        names = []
        for u in urls:
            n = platform_name(u)
            if n not in names:
                names.append(n)
        fallback = f"download the replay from {' -> '.join(names)} with yt-dlp ({urls[0]})"
        return f"{first}; will {fallback}" if first else fallback.capitalize()
    return (f"{first}; " if first else "") + "NO AUDIO SOURCE - no Restream recording and no replay link"


def run(cfg, dry_run=False, no_upload=False, force=False) -> Report:
    report = Report(dry_run=dry_run)
    state = State(cfg.state_dir)
    now = time.time()
    since = now - float(cfg.get("general.lookback_days", 7)) * 86400
    # Episodes waiting for the Libsyn quota to reset must stay in view even if older
    # than lookback_days.
    waiting = [r["started_at"] for r in state.rows_with_status("waiting_quota") if r["started_at"]]
    if waiting:
        since = min(since, min(waiting) - 86400)
    since = int(since)

    try:
        audio = audio_settings(cfg)  # catch a bad setting before downloading anything
        keep_name, keep_orig, keep_conv = keep_policy(cfg)
        check_settings(cfg)
        download_order(cfg)
        check_template(cfg.get("episode.title_template", "{title}"))
        if dest_key(cfg) == "transistor":
            from .transistor import episode_numbering
            episode_numbering(cfg, None)
        report.dest = dest_name(cfg)
    except ValueError as e:
        report.errors.append(f"Settings: {e}")
        return report
    if no_upload:
        keep_conv = True  # --no-upload exists so you can listen to the MP3
    try:
        dest_items = existing_items(cfg)
        if dest_key(cfg) == "libsyn" and not cfg.get("libsyn.feed_url"):
            report.errors.append("libsyn.feed_url not set: only castsync's own history is used "
                                 "to detect existing episodes")
    except Exception as e:
        report.errors.append(f"Could not read existing episodes from {dest_name(cfg)}: {e}. "
                             "Nothing uploaded this run to avoid duplicates.")
        return report

    quota = Quota(cfg, state, dest_items, now)
    candidates = gather(cfg, cfg.state_dir, since, report)
    index = build_index(cfg, dest_items, state)
    todo, report.skipped = select(cfg, candidates, index, state, now,
                                  force=force, persist=not dry_run)
    max_per_run = int(cfg.get("general.max_episodes_per_run", 2))
    uploading = not dry_run and not no_upload

    def hold(c, nbytes, estimated):
        title = episode_title(cfg, c)
        prev = state.get(c.key)
        newly = not (prev and prev["status"] == "waiting_quota")
        if not dry_run:
            state.record(c.key, c.source, title, normalize_title(title), c.started_at,
                         "waiting_quota", detail=f"needs {nbytes / MB:.1f} MB; "
                         f"{quota.remaining / MB:.1f} MB left until {quota.reset_text()}")
        if newly or dry_run:
            report.held.append({"title": title, "source": c.source,
                                "mb": f"{'~' if estimated else ''}{nbytes / MB:.1f}"})

    publisher = make_publisher(cfg, cfg.state_dir) if uploading else None
    max_attempts = int(cfg.get("general.max_attempts", 3))
    done = visited = 0
    for c in todo:  # newest first
        if done >= max_per_run:
            break  # the rest are picked up next run (still newest first)
        visited += 1
        title = episode_title(cfg, c)
        norm = normalize_title(title)
        est = estimate_bytes(c.duration_s, audio)
        if (uploading or dry_run) and est and not quota.fits(est):
            hold(c, est, estimated=True)   # no point downloading it this month
            continue
        if dry_run:
            try:
                plan = describe_audio_plan(c, cfg)
            except Exception as e:
                plan = f"couldn't check ({e})"
            report.published.append({
                "title": title, "source": c.source, "mp3": "-", "audio": plan,
                "when": datetime.fromtimestamp(c.started_at).strftime("%Y-%m-%d %H:%M")
                if c.started_at else "",
                "result": "would download, convert and upload"
                          + (f" (~{est / MB:.1f} MB)" if est else "")})
            if est:
                quota.add(est)
            done += 1
            continue
        done += 1
        base = os.path.join(str(cfg.work_dir), safe_key(c.key))
        res = None
        try:
            log.info("Processing %s (%s)", title, c.key)
            res = process(cfg, c, publisher, str(cfg.work_dir), quota if uploading else None)
            status = "published" if publisher else "converted"
            state.record(c.key, c.source, title, norm, c.started_at, status,
                         libsyn_ref=res["ref"], mp3_path=res["mp3"], detail=res["result"])
            if publisher:
                state.record_upload(c.key, os.path.basename(res["mp3"]), res["bytes"])
                quota.add(res["bytes"])
            report.published.append(res)
        except QuotaHold as h:
            hold(c, h.nbytes, estimated=False)
        except Exception as e:
            log.exception("Failed: %s", title)
            prev = state.get(c.key)
            fail_status = "allowed" if prev and prev["status"] == "allowed" else "failed"
            state.record(c.key, c.source, title, norm, c.started_at, fail_status,
                         detail=str(e)[:1000], bump_attempt=True)
            row = state.get(c.key)
            msg = str(e)
            if row["attempts"] >= max_attempts:
                msg += f"  [gave up after {row['attempts']} attempts; use `castsync retry {c.key}`]"
            report.failed.append({"title": title, "source": c.source, "error": msg})
        finally:
            try:
                kept = finalize(cfg, base, keep_orig, keep_conv)
            except OSError as e:
                kept = {"originals": [], "converted": []}
                report.errors.append(f"Cleaning up {base}: {e}")
            if res is not None:
                res["mp3"] = (kept["converted"][0] if kept["converted"]
                              else f"{res['file']} (deleted locally; keep_files: {keep_name})")
                if kept["originals"]:
                    res["original"] = kept["originals"][0]
    report.waiting = len(todo) - visited
    report.quota_line = quota.summary()
    report.quota_reset = quota.reset_text()
    keep_days = cfg.get("general.keep_days", cfg.get("general.keep_mp3_days", 14))
    keep_dir = cfg.get("general.keep_dir")
    expire([str(cfg.work_dir)] + ([os.path.expanduser(keep_dir)] if keep_dir else []),
           float(keep_days or 0))
    return report
