"""castsync command line.

  castsync run [--dry-run] [--no-upload] [--force]   one polling pass (what the timer runs)
  castsync auth restream | libsyn                    one-time sign-in
  castsync status                                    upload usage + recent history
  castsync shows                                     list your Transistor shows (to find show_id)
  castsync check-config                              check config.yaml for mistakes
  castsync retitle [--days N] [--apply]              update uploaded Transistor episodes to
                                                     match title_template, episode_number, season
  castsync renumber [--apply]                        set episode number/season on EVERY episode
                                                     of the Transistor show (not just castsync's)
  castsync retry KEY | ignore KEY                    manage a specific stream
  castsync retry --all                               retry every failed stream (incl. gave up)
  castsync allow KEY                                 upload it even though it looks like a duplicate
  castsync test-email                                send a test report
  castsync uninstall [--purge|--keep-data] [--yes]   remove castsync (asks about your data)
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import ConfigError, load_config
from .report import Report, send_email
from .state import State

DEFAULT_CONFIG = os.environ.get("CASTSYNC_CONFIG", "~/.config/castsync/config.yaml")


def _setup_logging(cfg, verbose):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stderr)]
    log_file = cfg.state_dir / "castsync.log"
    handlers.append(RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5))
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_run(cfg, args):
    from .runner import run

    lock_path = cfg.state_dir / "castsync.lock"
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("Another castsync run is still going (long upload?); skipping this one")
        return 0
    report = run(cfg, dry_run=args.dry_run, no_upload=args.no_upload, force=args.force)
    show = cfg.get("general.show_name", "Podcast")
    print(report.text())
    if report.did_work or args.always_email:
        path = report.save(cfg.state_dir / "reports")
        logging.info("Report saved to %s", path)
        if not args.no_email:
            try:
                send_email(cfg, report, show)
            except Exception as e:
                logging.error("Could not send report email: %s", e)
    return 1 if (report.failed or report.errors) else 0


def cmd_auth(cfg, args):
    if args.service == "restream":
        from .restream import RestreamClient
        RestreamClient(cfg, cfg.state_dir).authorize_interactive()
    else:
        from .libsyn import LibsynPublisher
        user = cfg.get("libsyn.api.username") or input("Libsyn login email: ")
        pw = cfg.get("libsyn.api.password") or getpass.getpass("Libsyn password: ")
        LibsynPublisher(cfg, cfg.state_dir).authorize_password(user, pw)
    return 0


def cmd_status(cfg, args):
    st = State(cfg.state_dir)
    try:
        from .destinations import dest_name, existing_items
        from .quota import Quota
        import time as _t
        print(f"Destination: {dest_name(cfg)}")
        print(Quota(cfg, st, existing_items(cfg), _t.time()).summary() + "\n")
    except Exception as e:
        print(f"(Couldn't read the destination: {e})\n")
    rows = st.recent(args.limit)
    if not rows:
        print("No history yet.")
    for r in rows:
        print(f"{r['status']:<17} {r['key']:<48} {r['title'][:60]}")
        if r["detail"] and r["status"] in ("failed", "published", "exists_in_libsyn", "waiting_quota"):
            print(f"{'':17} {r['detail'][:120]}")
    return 0


def cmd_set(cfg, args, status):
    st = State(cfg.state_dir)
    row = st.get(args.key)
    if not row:
        print("Unknown key. See `castsync status` for keys.")
        return 1
    st.db.execute("UPDATE episodes SET status=?, attempts=0 WHERE key=?", (status, args.key))
    st.db.commit()
    print(f"{args.key} -> {status}")
    return 0


def cmd_retry_all(cfg, args):
    """Reset the try count of every failed stream so the next run tries them again."""
    st = State(cfg.state_dir)
    max_attempts = int(cfg.get("general.max_attempts", 3))
    rows = [r for r in st.rows_with_status("failed")] + [
        r for r in st.rows_with_status("allowed") if r["attempts"]]
    if not rows:
        print("No failed streams to retry.")
        return 0
    gave_up = sum(1 for r in rows if r["attempts"] >= max_attempts)
    for r in sorted(rows, key=lambda r: r["started_at"] or 0, reverse=True):
        mark = "gave up" if r["attempts"] >= max_attempts else f"{r['attempts']} tr{'y' if r['attempts'] == 1 else 'ies'}"
        print(f"  {r['key']:<44} {r['title'][:60]}  ({mark})")
        st.db.execute("UPDATE episodes SET attempts=0 WHERE key=?", (r["key"],))
    st.db.commit()
    print(f"\n{len(rows)} stream(s) reset ({gave_up} had given up). They'll be tried again on the "
          f"next run, newest first, {cfg.get('general.max_episodes_per_run', 2)} per run.\n"
          "Run `castsync run` to start now instead of waiting for the hourly timer.")
    return 0


def cmd_check_config(cfg, args):
    """Config parsed fine if we got here; also check the values that matter."""
    from .audio import audio_settings
    from .destinations import check_settings, dest_name
    from .files import keep_policy
    from .models import download_order
    from .titles import check_filename_template, check_template
    from .report import smtp_settings
    problems = []
    for label, fn in (("destination", check_settings), ("audio", audio_settings),
                      ("keep_files", keep_policy), ("email", smtp_settings),
                      ("download_order", download_order),
                      ("transistor batches",
                       lambda c: __import__("castsync.transistor", fromlist=["x"]).batch_settings(c)),
                      ("transistor numbering",
                       lambda c: __import__("castsync.transistor", fromlist=["x"])
                       .episode_numbering(c, None) if c.get("destination") == "transistor" else None),
                      ("title_template",
                       lambda c: check_template(c.get("episode.title_template", "{title}"))),
                      ("filename_template", check_filename_template)):
        try:
            fn(cfg)
        except Exception as e:
            problems.append(f"{label}: {e}")
    try:
        print(f"Config file: {cfg.path}\nDestination: {dest_name(cfg)}")
        names = {"youtube": "YouTube", "facebook": "Facebook", "twitch": "Twitch"}
        print("Audio sources, in order: Restream recording -> "
              + " -> ".join(names[k] + " replay" for k in download_order(cfg)))
    except Exception:
        pass
    if problems:
        print("\nProblems found:")
        for p in problems:
            print("  - " + p)
        return 1
    print("Config OK.")
    return 0


def cmd_retitle(cfg, args):
    """Recompute titles of episodes castsync published to Transistor and fix them."""
    import time as _t
    from .destinations import dest_key
    from .report import Report
    from .runner import gather
    from .titles import render_title
    from .transistor import TransistorClient, episode_numbering

    if dest_key(cfg) != "transistor":
        print("retitle works with destination: transistor only.")
        return 1
    st = State(cfg.state_dir)
    rows = [r for r in st.rows_with_status("published")
            if (r["libsyn_ref"] or "").startswith("transistor:")]
    if not rows:
        print("castsync hasn't published any Transistor episodes yet.")
        return 0
    since = int(_t.time() - args.days * 86400)
    rep = Report()
    originals = {c.key: c for c in gather(cfg, cfg.state_dir, since, rep)}
    for e in rep.errors:
        print("warning:", e)
    client = TransistorClient(cfg)
    changes, missing, unchanged = [], [], []
    for r in rows:
        c = originals.get(r["key"])
        if not c:
            missing.append(r)
            continue
        want = {"title": render_title(cfg, c.title, c.started_at),
                **episode_numbering(cfg, c.started_at)}
        ep_id = r["libsyn_ref"].split(":", 1)[1]
        try:
            attrs = client.episode(ep_id)
        except Exception as e:
            print(f"warning: couldn't read episode {ep_id} from Transistor ({e})")
            attrs = {"title": r["title"]}
        have = {k: ("" if attrs.get(k) is None else str(attrs.get(k))) for k in want}
        have.setdefault("number", "" if attrs.get("number") is None else str(attrs.get("number")))
        diff = {k: v for k, v in want.items() if have.get(k) != v}
        if diff:
            changes.append((r, ep_id, have, diff))
        else:
            unchanged.append((r, ep_id, have))
    numbering = episode_numbering(cfg, None)
    if "number" not in numbering:
        print("Note: episode numbers are left as they are (transistor.episode_number: none). "
              "Set it to date to number episodes YYYYMMDD.\n")
    if not changes:
        print("All published episodes already match title_template, episode_number and season.")
    labels = {"title": "name", "number": "episode #", "season": "season"}
    for r, ep_id, have, diff in changes:
        print(f"episode {ep_id} ({have.get('title') or r['title']}):")
        for k, v in diff.items():
            print(f"    {labels[k]:<9}  now: {have.get(k) or '-'}")
            print(f"    {'':<9}  new: {v}")
        if "number" not in diff:
            print(f"    {'episode #':<9}  {have.get('number') or '-'} (unchanged)")
    if unchanged:
        print(f"\nAlready correct ({len(unchanged)}):")
        for r, ep_id, have in unchanged:
            print(f"    #{have.get('number') or '-':<9} {have.get('title') or r['title']}")
    if missing:
        print(f"\n{len(missing)} episode(s) skipped: their original stream is older than "
              f"--days {args.days} or no longer listed on Restream/your channels:")
        for r in missing:
            print(f"    {r['title']}")
    if changes and not args.apply:
        from .transistor import estimate_seconds
        print(f"\nNothing changed yet. Run `castsync retitle --apply` to update "
              f"{len(changes)} episode(s) on Transistor (about "
              f"{_duration(estimate_seconds(cfg, len(changes)))}).")
        return 0
    failed = 0
    for r, ep_id, have, diff in changes:
        try:
            client.update(ep_id, diff)
            if "title" in diff:
                from .models import normalize_title
                st.db.execute("UPDATE episodes SET title=?, norm_title=? WHERE key=?",
                              (diff["title"], normalize_title(diff["title"]), r["key"]))
                st.db.commit()
            print(f"updated {ep_id}")
        except Exception as e:
            failed += 1
            print(f"FAILED {ep_id}: {e}")
    if changes:
        print(f"\nDone: {len(changes) - failed} updated, {failed} failed.")
    return 1 if failed else 0


def _duration(secs: float) -> str:
    secs = int(round(secs))
    if secs < 60:
        return f"{max(secs, 1)} seconds"
    m, sec = divmod(secs, 60)
    return f"{m} min {sec} s" if sec else f"{m} min"


def cmd_renumber(cfg, args):
    """Apply transistor.episode_number / season to all episodes on the show, using
    the air date castsync knows, else a date in the title, else the publish date."""
    from collections import defaultdict
    from datetime import datetime
    from .dates import extract_dates
    from .destinations import dest_key
    from .transistor import TransistorClient, episode_numbering

    if dest_key(cfg) != "transistor":
        print("renumber works with destination: transistor only.")
        return 1
    try:
        probe = episode_numbering(cfg, None)
    except ValueError as e:
        print(e)
        return 1
    if not probe:
        print("Nothing to do: transistor.episode_number is none and transistor.season is leave.\n"
              "Set e.g.  episode_number: date  (and optionally  season: year) in config.yaml first.")
        return 1

    client = TransistorClient(cfg)
    episodes = client.episodes()
    st = State(cfg.state_dir)
    ours = {(r["libsyn_ref"] or "").split(":", 1)[1]: r for r in st.rows_with_status("published")
            if (r["libsyn_ref"] or "").startswith("transistor:")}
    order = cfg.get("matching.date_order", "mdy")

    plans, undated = [], []
    for ep in episodes:
        when, how = None, ""
        if ep["id"] in ours and ours[ep["id"]]["started_at"]:
            when, how = int(ours[ep["id"]]["started_at"]), "air date"
        else:
            found = sorted(extract_dates(ep["title"], ep.get("year"), order))
            if len(found) == 1:
                d = found[0]
                when, how = int(datetime(d.year, d.month, d.day, 12).timestamp()), "date in title"
            elif ep.get("pub_ts") and ep.get("status") != "draft":
                when, how = int(ep["pub_ts"]), "publish date"
                if len(found) > 1:
                    how += " (title has several dates)"
        if when is None:
            undated.append(ep)
            continue
        want = episode_numbering(cfg, when)
        have = {k: ("" if ep.get(k) is None else str(ep.get(k))) for k in want}
        plans.append((ep, how, have, want))

    # two episodes on the same day would get the same number: leave those alone
    by_number = defaultdict(list)
    for p in plans:
        if "number" in p[3]:
            by_number[p[3]["number"]].append(p)
    clashes = {n: ps for n, ps in by_number.items() if len(ps) > 1}
    changes = []
    for ep, how, have, want in plans:
        diff = {k: v for k, v in want.items() if have.get(k) != v}
        if "number" in diff and diff["number"] in clashes:
            diff.pop("number")
        if diff:
            changes.append((ep, how, have, diff))

    labels = {"number": "episode #", "season": "season"}
    print(f"{len(episodes)} episode(s) on the show; {len(changes)} need changes.\n")
    for ep, how, have, diff in changes:
        print(f"{ep['title']}  [{ep['status'] or '?'}; dated by {how}]")
        for k, v in diff.items():
            print(f"    {labels[k]:<9}  {have.get(k) or '-'}  ->  {v}")
    if clashes:
        print("\nSame date, so they'd get the same number - left unchanged (set these by hand):")
        for n, ps in sorted(clashes.items()):
            print(f"  {n}: " + " | ".join(p[0]['title'] for p in ps))
    if undated:
        print("\nNo date found (draft with no date in its title) - left unchanged:")
        for ep in undated:
            print(f"  {ep['title']}")
    if not changes:
        return 0
    if not args.apply:
        from .transistor import batch_settings, estimate_seconds
        size, pause = batch_settings(cfg)
        secs = estimate_seconds(cfg, len(changes))
        pace = (f"{size} at a time with a {pause:g}-second pause between batches"
                if size else "about one per second")
        print(f"\nNothing changed yet. Run `castsync renumber --apply` to update "
              f"{len(changes)} episode(s), {pace} (about {_duration(secs)}).")
        return 0
    from .transistor import estimate_seconds
    print(f"\nUpdating {len(changes)} episode(s); this takes about "
          f"{_duration(estimate_seconds(cfg, len(changes)))}...")
    failed = 0
    for ep, how, have, diff in changes:
        try:
            client.update(ep["id"], diff)
        except Exception as e:
            failed += 1
            print(f"FAILED {ep['title']}: {e}")
    print(f"\nDone: {len(changes) - failed} updated, {failed} failed.")
    return 1 if failed else 0


def cmd_shows(cfg, args):
    from .transistor import TransistorClient
    try:
        shows = TransistorClient(cfg).shows()
    except Exception as e:
        print(f"FAILED: {e}")
        return 1
    if not shows:
        print("No shows found on this Transistor account.")
    for sh in shows:
        print(f"show_id: {sh['id']:<10} {sh['title']}   {sh.get('feed') or ''}")
    print("\nPut the right one in config.yaml as transistor.show_id")
    return 0


def cmd_test_email(cfg, args):
    from .report import smtp_settings

    s = smtp_settings(cfg)
    to = cfg.get("email.to")
    print(f"Server:   {s.get('smtp_host') or '(not set)'}:{s['smtp_port']}  security={s['security']}"
          f"  provider={s['provider']}")
    print(f"Login:    {s['username'] or '(none - unauthenticated)'}")
    print(f"From:     {s['from']}")
    print(f"To:       {to}")
    if not to or not s.get("smtp_host"):
        print("Set email.to and email.provider (or email.smtp_host) in config.yaml first.")
        return 1
    r = Report()
    r.published.append({"title": "Test episode", "source": "test", "mp3": "-",
                        "result": "This is a castsync test email."})
    try:
        send_email(cfg, r, cfg.get("general.show_name", "Podcast"))
    except Exception as e:
        print(f"\nFAILED: {e}")
        return 1
    print("\nSent. Check the inbox (and spam folder).")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="castsync", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="one polling pass")
    r.add_argument("--dry-run", action="store_true", help="only show what would be done")
    r.add_argument("--no-upload", action="store_true", help="download + make MP3, don't upload")
    r.add_argument("--force", action="store_true", help="ignore history/duplicate matches (careful)")
    r.add_argument("--no-email", action="store_true")
    r.add_argument("--always-email", action="store_true", help="email even if nothing happened")

    a = sub.add_parser("auth", help="one-time sign-in")
    a.add_argument("service", choices=["restream", "libsyn"])

    s = sub.add_parser("status", help="recent history")
    s.add_argument("--limit", type=int, default=25)
    for name in ("retry", "ignore", "allow"):
        x = sub.add_parser(name, help=f"{name} a stream by key")
        if name == "retry":
            x.add_argument("key", nargs="?", help="stream key from `castsync status`")
            x.add_argument("--all", action="store_true",
                           help="retry every stream that failed, including ones that gave up")
        else:
            x.add_argument("key")
    sub.add_parser("test-email")
    sub.add_parser("shows", help="list Transistor shows and their ids")
    sub.add_parser("check-config", help="check config.yaml for mistakes")
    rt = sub.add_parser("retitle", help="rename published Transistor episodes to match the template")
    rt.add_argument("--apply", action="store_true", help="actually rename (default: just show)")
    rt.add_argument("--days", type=int, default=180, help="how far back to look (default 180)")
    rn = sub.add_parser("renumber", help="set episode number/season on every Transistor episode")
    rn.add_argument("--apply", action="store_true", help="actually change (default: just show)")

    u = sub.add_parser("uninstall", help="remove castsync from this computer")
    u.add_argument("extra", nargs="*", help="--purge, --keep-data, --yes")

    args, unknown = p.parse_known_args(argv)
    if args.cmd == "uninstall":  # works even if the config is missing or broken
        script = os.path.expanduser("~/.local/share/castsync/uninstall.sh")
        if not os.path.exists(script):
            print("Uninstaller not found; run ./uninstall.sh from the castsync download folder.")
            return 1
        os.execvp("bash", ["bash", script, *args.extra, *unknown])
    if unknown:
        p.error("unrecognized arguments: " + " ".join(unknown))
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    _setup_logging(cfg, args.verbose)
    if args.cmd == "run":
        return cmd_run(cfg, args)
    if args.cmd == "auth":
        return cmd_auth(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "retry":
        if args.all:
            return cmd_retry_all(cfg, args)
        if not args.key:
            p.error("retry needs a KEY, or --all")
        return cmd_set(cfg, args, "failed")
    if args.cmd == "ignore":
        return cmd_set(cfg, args, "ignored")
    if args.cmd == "allow":
        return cmd_set(cfg, args, "allowed")
    if args.cmd == "renumber":
        return cmd_renumber(cfg, args)
    if args.cmd == "retitle":
        return cmd_retitle(cfg, args)
    if args.cmd == "check-config":
        return cmd_check_config(cfg, args)
    if args.cmd == "shows":
        return cmd_shows(cfg, args)
    if args.cmd == "test-email":
        return cmd_test_email(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
