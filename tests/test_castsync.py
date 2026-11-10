import os
import sys
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from castsync import runner
from castsync.config import Config
from castsync.libsyn import LibsynPublisher, _find_file_id, feed_items, feed_titles
from castsync.matching import ExistingIndex
from castsync.models import Candidate, normalize_title
from castsync.state import State

FEED = b"""<?xml version="1.0"?><rss><channel>
<item><title>Ep 41: Old News</title></item>
<item><title>Ep 40 - Older</title></item></channel></rss>"""


def make_cfg(tmp, **over):
    raw = {
        "general": {"state_dir": str(tmp / "state"), "show_name": "Test Show",
                    "max_episodes_per_run": 5},
        "filters": {"min_duration_minutes": 0.05, "settle_minutes": 20,
                    "skip_title_regex": [r"\btest\b"]},
        "audio": {"loudnorm": False},
        "libsyn": {"feed_url": "https://x.libsyn.com/rss", "mode": "ftp",
                   "ftp": {"username": "u", "password": "p"}},
        "restream": {"enabled": True},
    }
    for k, v in over.items():
        raw.setdefault(k, {}).update(v)
    return Config(raw=raw, path=tmp / "config.yaml")


def idx(*titles, year=2026):
    i = ExistingIndex()
    for t in titles:
        i.add(t, [t], year)
    return i


def tone(path, seconds=5):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "aac", path], check=True)
    return path


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.state = State(self.cfg.state_dir)
        self.now = time.time()

    def c(self, key, title, src="restream", ended_ago=3600, dur=3600):
        return Candidate(key=key, source=src, title=title, started_at=int(self.now - ended_ago - dur),
                         finished_at=int(self.now - ended_ago), duration_s=dur)

    def test_rules(self):
        cands = [
            self.c("restream:1", "Ep 42: Big News | LIVE"),
            self.c("youtube:a", "Ep 42: Big News", src="youtube"),       # dup of restream:1
            self.c("restream:2", "Ep 41: Old News!"),                   # already on Libsyn
            self.c("restream:3", "Mic test"),                           # skip regex
            self.c("restream:4", "Ep 43", ended_ago=60),                # still settling
        ]
        cfg = make_cfg(self.tmp, episode={"strip_from_title": [r"\s*\|\s*LIVE\s*$"]},
                       matching={"match_air_date": False})
        todo, skipped = runner.select(cfg, cands, idx("Ep 41: Old News"),
                                      self.state, self.now)
        self.assertEqual([t.key for t in todo], ["restream:1"])
        reasons = {s["reason"] for s in skipped}
        self.assertTrue(any("already on Libsyn" in r for r in reasons), reasons)
        self.assertIn("matches skip_title_regex", reasons)
        # second pass: handled ones are quiet
        todo2, skipped2 = runner.select(cfg, cands, idx("Ep 41: Old News"),
                                        self.state, self.now)
        self.assertEqual(skipped2, [])

    def test_settling_restream_blocks_platform_duplicate(self):
        cands = [self.c("restream:9", "Ep 50", ended_ago=60),
                 self.c("youtube:z", "Ep 50", src="youtube", ended_ago=60 * 30)]
        todo, _ = runner.select(self.cfg, cands, idx(), self.state, self.now)
        self.assertEqual(todo, [])

    def test_date_template_distinguishes_repeating_titles(self):
        cfg = make_cfg(self.tmp, episode={"title_template": "{title} ({date})"})
        c = self.c("restream:5", "Weekly Show")
        todo, _ = runner.select(cfg, [c], idx("Weekly Show (2020-01-01)"),
                                self.state, self.now)
        self.assertEqual(len(todo), 1)

    def test_failed_gives_up(self):
        c = self.c("restream:6", "Ep 60")
        for _ in range(3):
            self.state.record(c.key, "restream", "Ep 60", "ep 60", 0, "failed", bump_attempt=True)
        todo, _ = runner.select(self.cfg, [c], idx(), self.state, self.now)
        self.assertEqual(todo, [])


class DateMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = State(make_cfg(self.tmp).state_dir)
        # A stream that aired at noon on Nov 9 2026 and ended 2 hours ago (relative to `now`)
        from datetime import datetime
        self.start = int(datetime(2026, 11, 9, 12, 0).timestamp())
        self.now = self.start + 3 * 3600

    def cand(self, title, key="restream:x", src="restream", start=None):
        start = start or self.start
        return Candidate(key=key, source=src, title=title, started_at=start,
                         finished_at=start + 3600, duration_s=3600)

    def sel(self, cfg, cands, index):
        return runner.select(cfg, cands, index, self.state, self.now)

    def test_different_names_same_date_in_both(self):
        cfg = make_cfg(self.tmp, matching={"match_air_date": False})
        todo, skipped = self.sel(cfg, [self.cand("Big News Night 11/9/2026")],
                                 idx("Episode 42 - November 9th, 2026"))
        self.assertEqual(todo, [])
        self.assertIn("same date 2026-11-09", skipped[0]["reason"])

    def test_date_in_libsyn_file_name(self):
        cfg = make_cfg(self.tmp, matching={"match_air_date": False})
        i = ExistingIndex()
        i.add("Totally Renamed", ["Totally Renamed", "2026-11-09-big-news.mp3"], 2026)
        todo, _ = self.sel(cfg, [self.cand("Ep 42 (Nov 9)")], i)
        self.assertEqual(todo, [])

    def test_air_date_counts_by_default(self):
        # stream title has no date, but it aired the day the Libsyn episode is named for
        todo, skipped = self.sel(make_cfg(self.tmp), [self.cand("Ep 42: Big News")],
                                 idx("Big News - 2026-11-09"))
        self.assertEqual(todo, [])
        self.assertIn("same date", skipped[0]["reason"])

    def test_different_dates_still_upload(self):
        todo, _ = self.sel(make_cfg(self.tmp), [self.cand("Show 11/9/2026")],
                           idx("Show 11/2/2026", "Show Nov 16, 2026"))
        self.assertEqual(len(todo), 1)

    def test_no_dates_and_different_titles_upload(self):
        cfg = make_cfg(self.tmp, matching={"match_air_date": False})
        todo, _ = self.sel(cfg, [self.cand("Ep 42")], idx("Ep 41", "Part 1/2"))
        self.assertEqual(len(todo), 1)

    def test_cross_platform_copy_with_different_title(self):
        cands = [self.cand("Ep 42: Big News"),
                 self.cand("BIG NEWS (full stream)", key="youtube:y", src="youtube")]
        todo, _ = self.sel(make_cfg(self.tmp), cands, idx())
        self.assertEqual([t.key for t in todo], ["restream:x"])

    def test_already_uploaded_by_castsync_under_old_name(self):
        self.state.record("restream:old", "restream", "Old Name", "old name", self.start,
                          "published", mp3_path="/w/2026-11-09-old-name.mp3")
        cfg = make_cfg(self.tmp, matching={"match_air_date": False})
        index = runner.build_index(cfg, [], self.state)
        todo, _ = self.sel(cfg, [self.cand("Renamed 2026-11-09", key="youtube:n", src="youtube")],
                           index)
        self.assertEqual(todo, [])

    def test_allow_overrides_date_match(self):
        c = self.cand("Second show 11/9/2026", key="restream:two")
        cfg = make_cfg(self.tmp)
        self.sel(cfg, [c], idx("Main show 11/9/2026"))
        self.assertEqual(self.state.get(c.key)["status"], "exists_in_libsyn")
        self.state.db.execute("UPDATE episodes SET status='allowed' WHERE key=?", (c.key,))
        todo, _ = self.sel(cfg, [c], idx("Main show 11/9/2026"))
        self.assertEqual(len(todo), 1)

    def test_date_match_is_reported(self):
        from castsync.report import Report
        r = Report(skipped=[{"title": "X", "reason": "already on Libsyn as 'Y' (same date 2026-11-09)"}])
        self.assertTrue(r.did_work)
        self.assertIn("castsync allow", r.text())


class EmailTests(unittest.TestCase):
    def cfg(self, **email):
        return Config(raw={"email": {"to": ["me@x.com"], **email}}, path=Path("c"))

    def test_presets_and_overrides(self):
        from castsync.report import smtp_settings
        s = smtp_settings(self.cfg(provider="zoho", username="me@zohomail.com"))
        self.assertEqual((s["smtp_host"], s["smtp_port"], s["security"]),
                         ("smtp.zoho.com", 465, "ssl"))
        self.assertEqual(s["from"], "me@zohomail.com")
        s = smtp_settings(self.cfg(provider="zoho_pro"))
        self.assertEqual(s["smtp_host"], "smtppro.zoho.com")
        s = smtp_settings(self.cfg(provider="zoho", smtp_host="smtp.zoho.eu", smtp_port=587))
        self.assertEqual((s["smtp_host"], s["smtp_port"], s["security"]),
                         ("smtp.zoho.eu", 587, "starttls"))
        s = smtp_settings(self.cfg(smtp_host="mail.example.com", smtp_port=465))
        self.assertEqual(s["security"], "ssl")  # inferred from port
        s = smtp_settings(self.cfg(smtp_host="mail.example.com", smtp_port=587, security="TLS"))
        self.assertEqual(s["security"], "starttls")
        with self.assertRaises(ValueError):
            smtp_settings(self.cfg(provider="nope"))

    def test_auth_error_hint(self):
        import smtplib
        from castsync.report import Report, send_email
        with mock.patch("smtplib.SMTP_SSL") as S:
            S.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
            with self.assertRaises(RuntimeError) as cm:
                send_email(self.cfg(provider="zoho", username="a@b.com", password="x"),
                           Report(), "Show")
        self.assertIn("app-specific", str(cm.exception))
        self.assertIn("smtppro.zoho.com", str(cm.exception))

    @unittest.skipUnless(__import__("importlib").util.find_spec("aiosmtpd"), "pip install aiosmtpd")
    def test_real_send_plain_smtp_no_auth(self):
        # A local SMTP server (like a postfix relay): no TLS, no login.
        from aiosmtpd.controller import Controller
        from castsync.report import Report, send_email
        got = []

        class H:
            async def handle_DATA(self, server, session, envelope):
                got.append(envelope)
                return "250 OK"

        ctl = Controller(H(), hostname="127.0.0.1", port=8925)
        ctl.start()
        try:
            r = Report()
            r.published.append({"title": "Ep 1", "source": "t", "mp3": "-", "result": "ok"})
            send_email(self.cfg(smtp_host="127.0.0.1", smtp_port=8925, security="none",
                                **{"from": "castsync@studio.lan", "from_name": "Pod Bot"}),
                       r, "Show")
        finally:
            ctl.stop()
        self.assertEqual(got[0].mail_from, "castsync@studio.lan")
        self.assertIn(b"Pod Bot", got[0].content)
        self.assertIn(b"Ep 1", got[0].content)


class AudioQualityTests(unittest.TestCase):
    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=codec_name,sample_rate,channels,bit_rate", "-of",
                              "default=nw=1", path], capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in out.split())

    def convert(self, **audio):
        tmp = Path(tempfile.mkdtemp())
        src = tone(str(tmp / "in.m4a"), seconds=4)
        cfg = Config(raw={"audio": {"loudnorm": False, **audio}}, path=tmp / "c")
        from castsync.audio import to_mp3
        return self.probe(to_mp3(src, str(tmp / "out.mp3"), cfg, title="t"))

    def test_default_is_64k_44k_mono(self):
        p = self.convert()
        self.assertEqual((p["codec_name"], p["sample_rate"], p["channels"], p["bit_rate"]),
                         ("mp3", "44100", "1", "64000"))

    def test_custom_friendly_values(self):
        p = self.convert(bitrate="128kbps", sample_rate="48k", channels="stereo")
        self.assertEqual((p["sample_rate"], p["channels"], p["bit_rate"]), ("48000", "2", "128000"))
        p = self.convert(bitrate=32, sample_rate=22050, channels=1)
        self.assertEqual((p["sample_rate"], p["bit_rate"]), ("22050", "32000"))

    def test_vbr(self):
        p = self.convert(bitrate_mode="vbr", vbr_quality=7)
        self.assertEqual(p["codec_name"], "mp3")

    def test_invalid_combinations_explained(self):
        from castsync.audio import audio_settings, describe
        def cfg(**a):
            return Config(raw={"audio": a}, path=Path("c"))
        self.assertEqual(describe(audio_settings(cfg())), "64 kbps, 44.1 kHz, mono")
        self.assertEqual(audio_settings(cfg(sample_rate="44khz"))["sample_rate"], 44100)
        for bad, msg in [({"bitrate": "65k"}, "isn't possible"),
                         ({"bitrate": "320k", "sample_rate": 22050}, "isn't possible"),
                         ({"sample_rate": 96000}, "not allowed"),
                         ({"channels": "surround"}, "mono")]:
            with self.assertRaises(ValueError) as cm:
                audio_settings(cfg(**bad))
            self.assertIn(msg, str(cm.exception))

    def test_bad_setting_stops_run_before_download(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, audio={"bitrate": "65k"})
        with mock.patch.object(runner, "gather") as g:
            rep = runner.run(cfg)
        g.assert_not_called()
        self.assertIn("isn't possible", rep.errors[0])


class OrderAndQuotaTests(unittest.TestCase):
    """Newest first, 2 per run, Libsyn monthly upload limit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = tone(str(self.tmp / "src.m4a"), seconds=3)
        self.now = time.time()

    def cand(self, n, days_ago, dur=3600):
        start = int(self.now - days_ago * 86400 - 7200)
        src = self.src

        def fetch(work_dir):
            dest = os.path.join(work_dir, "a.m4a")
            subprocess.run(["cp", src, dest], check=True)
            return dest
        return Candidate(key=f"restream:{n}", source="restream", title=f"Show number {n}",
                         started_at=start, finished_at=start + (dur or 3600), duration_s=dur,
                         fetch_audio=fetch)

    def go(self, cands, cfg, feed=(), **kw):
        uploads = []
        with mock.patch.object(runner, "existing_items", return_value=list(feed)), \
             mock.patch.object(runner, "gather", return_value=cands), \
             mock.patch.object(LibsynPublisher, "ftp_upload",
                               side_effect=lambda p: uploads.append(os.path.basename(p))
                               or os.path.basename(p)):
            rep = runner.run(cfg, **kw)
        return rep, uploads

    def cfg(self, **libsyn):
        c = make_cfg(self.tmp, matching={"match_air_date": False}, libsyn=libsyn)
        c.raw["general"].pop("max_episodes_per_run", None)  # use the default (2)
        return c

    def test_newest_first_two_per_run(self):
        cands = [self.cand(1, 6, dur=60), self.cand(2, 1, dur=60), self.cand(3, 3, dur=60),
                 self.cand(4, 2, dur=60)]
        rep, _ = self.go(cands, self.cfg())
        self.assertEqual([p["title"] for p in rep.published], ["Show number 2", "Show number 4"])
        self.assertEqual(rep.waiting, 2)
        rep, _ = self.go(cands, self.cfg())
        self.assertEqual([p["title"] for p in rep.published], ["Show number 3", "Show number 1"])

    def test_estimate_holds_without_downloading(self):
        # 60-min show at 64 kbps is ~29 MB; only ~25 MB left of a 30 MB plan (5 MB margin)
        c = self.cand(1, 1)
        c.fetch_audio = mock.Mock(side_effect=AssertionError("should not download"))
        rep, ups = self.go([c], self.cfg(monthly_limit_mb=30))
        self.assertEqual(ups, [])
        self.assertEqual(len(rep.held), 1)
        self.assertIn("29.", rep.held[0]["mb"])
        self.assertTrue(rep.did_work)
        self.assertIn("WAITING FOR LIBSYN MONTHLY LIMIT", rep.text())
        # next hour: still waiting, but no repeat email
        rep2, _ = self.go([c], self.cfg(monthly_limit_mb=30))
        self.assertFalse(rep2.did_work, rep2.text())

    def test_actual_size_checked_and_usage_counted(self):
        from castsync.state import State
        st = State(self.cfg().state_dir)
        st.record_upload("restream:old", "earlier.mp3", 20 * 10**6)       # this month
        st.record_upload("restream:older", "last-month.mp3", 900 * 10**6,
                         when=self.now - 40 * 86400)                       # previous month
        feed = [{"title": "Manual ep", "file": "manual.mp3", "year": 2026,
                 "pub_ts": int(self.now - 3600), "bytes": 30 * 10**6},
                {"title": "Our ep", "file": "earlier.mp3", "year": 2026,
                 "pub_ts": int(self.now - 3600), "bytes": 20 * 10**6}]    # not double counted
        c1, c2 = self.cand(1, 0.5, dur=None), self.cand(2, 0.2, dur=None)
        c1.duration_s = c2.duration_s = None  # unknown length -> decided after conversion
        rep, ups = self.go([c1, c2], self.cfg(monthly_limit_mb=56), feed)
        # used 50 MB, limit 56 - 5 margin = 1 MB available; each 3 s MP3 is ~24 KB
        self.assertEqual(len(ups), 2)
        self.assertIn("50.0 of 56 MB", rep.quota_line)
        rep, ups = self.go([self.cand(3, 0.1, dur=None)],
                           self.cfg(monthly_limit_mb=55.04), feed)
        self.assertEqual(ups, [])
        self.assertEqual(len(rep.held), 1)
        self.assertNotIn("~", rep.held[0]["mb"])  # measured, not estimated

    def test_held_episode_older_than_lookback_is_still_found(self):
        c = self.cand(9, 1)
        self.go([c], self.cfg(monthly_limit_mb=30))
        seen = {}
        def gather(cfg, state_dir, since, report):
            seen["since"] = since
            return []
        cfg = self.cfg(monthly_limit_mb=30)
        cfg.raw["general"]["lookback_days"] = 0.01
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", side_effect=gather):
            runner.run(cfg)
        self.assertLess(seen["since"], c.started_at)

    def test_dry_run_shows_quota(self):
        rep, ups = self.go([self.cand(1, 1), self.cand(2, 2)], self.cfg(monthly_limit_mb=40),
                           dry_run=True)
        self.assertEqual(ups, [])
        self.assertEqual(len(rep.published), 1)
        self.assertEqual(len(rep.held), 1)

    def test_period_math(self):
        from datetime import datetime
        from castsync.quota import _period
        self.assertEqual(_period(datetime(2026, 11, 9, 12), 1, 6),
                         (datetime(2026, 11, 1, 6), datetime(2026, 12, 1, 6)))
        self.assertEqual(_period(datetime(2026, 12, 1, 3), 1, 6),   # before 6 AM on the 1st
                         (datetime(2026, 11, 1, 6), datetime(2026, 12, 1, 6)))
        self.assertEqual(_period(datetime(2027, 1, 15), 1, 0)[1], datetime(2027, 2, 1))
        self.assertEqual(_period(datetime(2026, 2, 28, 12), 31, 0),
                         (datetime(2026, 2, 28), datetime(2026, 3, 31)))


class KeepFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = tone(str(self.tmp / "src.m4a"), seconds=3)

    def run_one(self, fail=False, no_upload=False, **general):
        cfg = make_cfg(self.tmp, general=general)
        now = time.time()
        src = self.src

        def fetch(work_dir):
            dest = os.path.join(work_dir, "restream-audio.m4a")
            subprocess.run(["cp", src, dest], check=True)
            open(os.path.join(work_dir, "junk.part"), "w").write("x")  # partial download
            return dest
        c = Candidate(key="restream:k1", source="restream", title="Ep 7",
                      started_at=int(now - 7200), finished_at=int(now - 3600),
                      fetch_audio=fetch)
        upload = mock.Mock(side_effect=RuntimeError("FTP down")) if fail else \
            mock.Mock(side_effect=lambda p: os.path.basename(p))
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=[c]), \
             mock.patch.object(LibsynPublisher, "ftp_upload", upload):
            rep = runner.run(cfg, no_upload=no_upload)
        return cfg, rep

    def files(self, root):
        return sorted(os.path.relpath(p, root) for p in
                      __import__("glob").glob(os.path.join(root, "**", "*"), recursive=True)
                      if os.path.isfile(p))

    def test_nothing_is_default_and_leaves_no_trace(self):
        cfg, rep = self.run_one()
        self.assertEqual(len(rep.published), 1, rep.text())
        self.assertEqual(self.files(cfg.work_dir), [])
        self.assertEqual(os.listdir(cfg.work_dir), [])
        self.assertIn("deleted locally", rep.published[0]["mp3"])

    def test_each_choice(self):
        expect = {"originals": ["restream_k1/original/restream-audio.m4a"],
                  "converted": ["restream_k1/converted/undated-ep-7.mp3"],
                  "originals_and_converted": ["restream_k1/converted/undated-ep-7.mp3",
                                              "restream_k1/original/restream-audio.m4a"]}
        for choice, files in expect.items():
            self.tmp = Path(tempfile.mkdtemp())
            self.src = tone(str(self.tmp / "src.m4a"), seconds=3)
            cfg, rep = self.run_one(keep_files=choice)
            got = [f.replace(f"-{time.strftime('%Y')}", "") for f in self.files(cfg.work_dir)]
            # filename has the air date in it; compare without it
            got = [re.sub(r"\d{4}-\d{2}-\d{2}", "undated", f) for f in self.files(cfg.work_dir)]
            self.assertEqual(got, files, choice)

    def test_keep_dir_and_aliases(self):
        keep = self.tmp / "Podcast Audio"
        cfg, rep = self.run_one(keep_files="Originals + Converted", keep_dir=str(keep))
        self.assertEqual(os.listdir(cfg.work_dir), [])
        self.assertEqual(sorted(os.listdir(keep)), ["converted", "originals"])
        self.assertTrue(rep.published[0]["mp3"].startswith(str(keep)))
        self.assertIn("(kept)", rep.text())

    def test_cleanup_after_failure_too(self):
        cfg, rep = self.run_one(fail=True)
        self.assertEqual(len(rep.failed), 1)
        self.assertEqual(self.files(cfg.work_dir), [])

    def test_no_upload_keeps_mp3_to_listen_to(self):
        cfg, rep = self.run_one(no_upload=True)
        self.assertTrue(rep.published[0]["mp3"].endswith(".mp3"))
        self.assertTrue(os.path.exists(rep.published[0]["mp3"]))

    def test_bad_choice(self):
        cfg, rep = self.run_one(keep_files="some")
        self.assertIn("keep_files", rep.errors[0])
        self.assertEqual(rep.published, [])

    def test_expire(self):
        from castsync.files import expire
        d = self.tmp / "kept" / "converted"
        d.mkdir(parents=True)
        old, new = d / "old.mp3", d / "new.mp3"
        old.write_text("x"); new.write_text("y")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        expire([str(self.tmp / "kept")], 14)
        self.assertEqual(os.listdir(d), ["new.mp3"])
        expire([str(self.tmp / "kept")], 0)   # 0 = forever
        self.assertEqual(os.listdir(d), ["new.mp3"])


class FakeTransistor:
    """Stands in for api.transistor.fm + the S3 upload URL."""
    def __init__(self, episodes=(), rate_limit_first=False):
        self.calls, self.uploaded, self.created, self.published = [], [], [], []
        self.episodes = list(episodes)
        self.rate_limit_first = rate_limit_first

    def _resp(self, code=200, body=None):
        import json as _j
        r = mock.Mock(status_code=code, text=_j.dumps(body or {}))
        r.content = r.text.encode()
        r.json = lambda: body or {}
        return r

    def request(self, method, url, headers=None, params=None, data=None, **kw):
        self.calls.append((method, url.replace("https://api.transistor.fm/v1", ""), params, data))
        assert headers["x-api-key"] == "KEY"
        if self.rate_limit_first:
            self.rate_limit_first = False
            return self._resp(429)
        path = url.split("/v1", 1)[1]
        if path == "/episodes/authorize_upload":
            return self._resp(200, {"data": {"attributes": {
                "upload_url": "https://s3.example/put?sig=1", "content_type": "audio/mpeg",
                "audio_url": "https://transistor-uploads.example/ep.mp3", "expires_in": 600}}})
        if path == "/episodes" and method == "POST":
            self.created.append(data)
            return self._resp(201, {"data": {"id": "777", "attributes": {"status": "draft"}}})
        if method == "PATCH" and path.startswith("/episodes/") and not path.endswith("/publish"):
            self.patched = getattr(self, "patched", []) + [(path, data)]
            return self._resp(200, {"data": {"id": path.split("/")[-1]}})
        if method == "GET" and path.startswith("/episodes/") and path.count("/") == 2 \
                and path != "/episodes/authorize_upload":
            return self._resp(200, {"data": {"id": path.split("/")[-1], "attributes": {
                "title": "PTR Radio November 2, 2026 - PTR Radio (11/2/2026) - we're sooo tired",
                "number": getattr(self, "current_number", None), "season": 2018}}})
        if path.endswith("/publish"):
            self.published.append((path, data))
            return self._resp(200, {"data": {"id": "777"}})
        if path == "/episodes" and method == "GET":
            per = int(params["pagination[per]"]); page = int(params["pagination[page]"])
            chunk = self.episodes[(page - 1) * per: page * per]
            pages = max(1, -(-len(self.episodes) // per))
            return self._resp(200, {"data": [{"id": str(i), "attributes": e} for i, e in enumerate(chunk)],
                                    "meta": {"currentPage": page, "totalPages": pages}})
        if path == "/shows":
            return self._resp(200, {"data": [{"id": "42", "attributes": {"title": "My Show",
                                    "feed_url": "https://feeds.transistor.fm/my-show"}}],
                                    "meta": {"totalPages": 1}})
        raise AssertionError(path)

    def put(self, url, data=None, headers=None, **kw):
        self.uploaded.append((url, headers["Content-Type"], data.read()))
        return self._resp(200)


class TransistorTests(unittest.TestCase):
    def cfg(self, tmp, **t):
        return make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42, **t})

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def client(self, fake, **t):
        from castsync.transistor import TransistorClient
        cfg = self.cfg(self.tmp, **t)
        cfg.raw["destination"] = "transistor"
        return TransistorClient(cfg, session=fake, sleep=lambda s: None)

    def test_upload_create_publish(self):
        fake = FakeTransistor()
        mp3 = self.tmp / "2026-11-09-ep.mp3"
        mp3.write_bytes(b"ID3audio")
        ref, msg = self.client(fake, extra_fields={"season": 2}).publish(
            str(mp3), "Ep 42", "<p>Big <b>news</b> tonight</p>\nmore")
        self.assertEqual(ref, "transistor:777")
        self.assertIn("Published", msg)
        self.assertEqual(fake.uploaded, [("https://s3.example/put?sig=1", "audio/mpeg", b"ID3audio")])
        form = fake.created[0]
        self.assertEqual(form["episode[show_id]"], "42")
        self.assertEqual(form["episode[audio_url]"], "https://transistor-uploads.example/ep.mp3")
        self.assertEqual(form["episode[summary]"], "Big news tonight more")
        self.assertEqual(form["episode[season]"], "2")
        self.assertEqual(fake.published, [("/episodes/777/publish", {"episode[status]": "published"})])
        self.assertEqual(fake.calls[0][2], {"filename": "2026-11-09-ep.mp3"})

    def test_draft_mode_does_not_publish(self):
        fake = FakeTransistor()
        mp3 = self.tmp / "e.mp3"; mp3.write_bytes(b"x")
        ref, msg = self.client(fake, publish="draft").publish(str(mp3), "Ep", "")
        self.assertEqual(fake.published, [])
        self.assertIn("draft", msg)

    def test_existing_episodes_paginated_including_drafts(self):
        eps = [{"title": f"Ep {i}", "status": "published", "published_at": "2026-10-01T12:00:00Z",
                "media_url": f"https://media.transistor.fm/abc/{i}.mp3?src=x"} for i in range(60)]
        eps.append({"title": "Draft 2026-11-09", "status": "draft", "published_at": None,
                    "created_at": "2026-11-09T20:00:00Z", "media_url": None})
        items = self.client(FakeTransistor(eps)).episodes()
        self.assertEqual(len(items), 61)
        self.assertEqual(items[0]["file"], "0.mp3")
        self.assertEqual(items[-1]["title"], "Draft 2026-11-09")
        self.assertEqual(items[-1]["year"], 2026)

    def test_rate_limit_backoff(self):
        slept = []
        from castsync.transistor import TransistorClient
        cfg = self.cfg(self.tmp)
        c = TransistorClient(cfg, session=FakeTransistor(rate_limit_first=True), sleep=slept.append)
        self.assertEqual(c.shows()[0]["id"], "42")
        self.assertIn(11, slept)

    def test_full_run_to_transistor(self):
        cfg = self.cfg(self.tmp)
        cfg.raw["destination"] = "Transistor.fm"
        src = tone(str(self.tmp / "a.m4a"), seconds=3)
        now = time.time()

        def fetch(work_dir):
            d = os.path.join(work_dir, "a.m4a"); subprocess.run(["cp", src, d], check=True); return d
        c = Candidate(key="restream:t1", source="restream", title="Ep 9: Live Q&A",
                      started_at=int(now - 7200), finished_at=int(now - 3600), fetch_audio=fetch)
        dup = Candidate(key="restream:t0", source="restream", title="Already There",
                        started_at=int(now - 9 * 86400), finished_at=int(now - 9 * 86400 + 3600))
        cfg.raw["general"]["lookback_days"] = 30
        cfg.raw.setdefault("matching", {})["match_air_date"] = False
        fake = FakeTransistor([{"title": "Already There", "status": "draft",
                                "created_at": "2026-11-01T00:00:00Z"}])
        with mock.patch("requests.Session", return_value=fake), \
             mock.patch("castsync.transistor.time.sleep"), \
             mock.patch.object(runner, "gather", return_value=[c, dup]):
            rep = runner.run(cfg)
        self.assertEqual([p["title"] for p in rep.published], ["Ep 9: Live Q&A"], rep.text())
        self.assertIn("sent to Transistor", rep.subject("Show"))
        self.assertIn("transistor:", rep.text())
        self.assertIn("already on Transistor", rep.text())
        self.assertIn("Transistor: no upload limit", rep.text())
        self.assertEqual(len(fake.uploaded), 1)

    def test_missing_settings_stop_before_download(self):
        cfg = make_cfg(self.tmp)
        cfg.raw["destination"] = "transistor"
        with mock.patch.object(runner, "gather") as g:
            rep = runner.run(cfg)
        g.assert_not_called()
        self.assertIn("transistor.api_key", rep.errors[0])

    def test_unknown_destination(self):
        cfg = make_cfg(self.tmp)
        cfg.raw["destination"] = "buzzsprout"
        rep = runner.run(cfg)
        self.assertIn("must be one of", rep.errors[0])


class ConfigErrorTests(unittest.TestCase):
    def explain(self, text):
        from castsync.config import ConfigError, load_config
        d = Path(tempfile.mkdtemp()); f = d / "config.yaml"; f.write_text(text)
        with self.assertRaises(ConfigError) as cm:
            load_config(f)
        return str(cm.exception)

    def test_uncommented_with_extra_space(self):
        msg = self.explain("email:\n  provider: zoho\n   smtp_host: smtp.zoho.com\n")
        self.assertIn("line 3", msg)
        self.assertIn("indented 3 spaces but should be 2", msg)
        self.assertIn(">>    3|    smtp_host", msg)

    def test_colon_in_value(self):
        msg = self.explain('general:\n  show_name: My Show: Live\n')
        self.assertIn("Put the whole value in quotes", msg)
        self.assertNotIn("indentation", msg)

    def test_tab(self):
        msg = self.explain("general:\n\tshow_name: x\n")
        self.assertIn("TAB", msg)
        self.assertEqual(msg.count("How to fix"), 1)

    def test_template_safe_to_uncomment(self):
        import re as _re
        import yaml as _yaml
        lines = open(os.path.join(os.path.dirname(__file__), "..", "config.example.yaml")).read().splitlines()
        for i, l in enumerate(lines):
            if _re.match(r"^\s+#[a-z_]+:", l):
                t = lines[:]; t[i] = l.replace("#", "", 1)
                _yaml.safe_load("\n".join(t))  # must not raise

    def test_cli_prints_no_traceback(self):
        d = Path(tempfile.mkdtemp()); f = d / "c.yaml"; f.write_text("a:\n  b: 1\n   c: 2\n")
        out = subprocess.run([sys.executable, "-m", "castsync", "-c", str(f), "status"],
                             capture_output=True, text=True,
                             cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("How to fix", out.stderr)


class MissingRestreamRecordingTests(unittest.TestCase):
    """Restream only keeps recent recordings; older events must fall back to the replay."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp, matching={"match_air_date": False},
                            general={"lookback_days": 120, "max_episodes_per_run": 5})
        self.now = time.time()

    def restream_cands(self, recordings):
        from castsync.restream import RestreamClient, restream_candidates
        now = int(self.now)
        events = {"items": [
            {"id": "new", "status": "finished", "title": "PTR Radio (11/2/2026)",
             "startedAt": now - 2 * 86400, "finishedAt": now - 2 * 86400 + 3600,
             "destinations": [{"externalUrl": "https://www.twitch.tv/videos/111"},
                              {"externalUrl": "https://www.youtube.com/watch?v=NEW"}]},
            {"id": "old", "status": "finished", "title": "PTR Radio (8/17/2026)",
             "startedAt": now - 80 * 86400, "finishedAt": now - 80 * 86400 + 3600,
             "destinations": [{"externalUrl": "https://www.facebook.com/x/videos/9"},
                              {"externalUrl": "https://www.twitch.tv/videos/222"},
                              {"externalUrl": "https://youtu.be/OLD"}]},
            {"id": "nolinks", "status": "finished", "title": "PTR Radio (8/3/2026)",
             "startedAt": now - 94 * 86400, "finishedAt": now - 94 * 86400 + 3600,
             "destinations": []}],
            "pagination": {"pages_total": 1}}
        client = mock.Mock(spec=RestreamClient)
        client.finished_events.side_effect = lambda since: iter(events["items"])
        client.recordings.side_effect = lambda eid: recordings.get(eid, {})
        return restream_candidates(self.cfg, self.tmp, 0, client=client)

    def test_urls_youtube_first(self):
        c = {x.key: x for x in self.restream_cands({})}
        self.assertEqual(c["restream:old"].urls, ["https://youtu.be/OLD", "https://www.facebook.com/x/videos/9",
                                                  "https://www.twitch.tv/videos/222"])

    def test_dry_run_says_where_audio_comes_from(self):
        cands = self.restream_cands({"new": {"audio": [{"fileName": "a.m4a"}]}})
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=cands):
            rep = runner.run(self.cfg, dry_run=True)
        plans = {p["title"]: p["audio"] for p in rep.published}
        self.assertEqual(plans["PTR Radio (11/2/2026)"], "Restream recording (audio track)")
        self.assertIn("Restream no longer has a recording; will download the replay from "
                      "YouTube -> Facebook -> Twitch", plans["PTR Radio (8/17/2026)"])
        self.assertIn("NO AUDIO SOURCE", plans["PTR Radio (8/3/2026)"])
        self.assertIn("audio:  Restream no longer has a recording", rep.text())

    def test_real_run_falls_back_to_youtube(self):
        cands = [c for c in self.restream_cands({}) if c.key == "restream:old"]
        src = tone(str(self.tmp / "yt.m4a"), seconds=3)
        tried = []

        def fake_ytdlp(cfg, urls, work, stem):
            tried.extend(urls)
            d = os.path.join(work, stem + ".m4a"); subprocess.run(["cp", src, d], check=True); return d
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=cands), \
             mock.patch.object(runner, "download_audio_with_ytdlp", side_effect=fake_ytdlp), \
             mock.patch.object(LibsynPublisher, "ftp_upload", return_value="x.mp3"):
            rep = runner.run(self.cfg)
        self.assertEqual(len(rep.published), 1, rep.text())
        self.assertEqual(tried[0], "https://youtu.be/OLD")
        self.assertIn("YouTube replay", " ".join(rep.published[0]["notes"]))

    def test_custom_order_and_left_out_platform(self):
        self.cfg.raw.setdefault("platforms", {})["download_order"] = ["Twitch", "yt"]
        c = {x.key: x for x in self.restream_cands({})}
        self.assertEqual(c["restream:old"].urls, ["https://www.twitch.tv/videos/222", "https://youtu.be/OLD"])
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=list(c.values())):
            rep = runner.run(self.cfg, dry_run=True)
        plan = [p["audio"] for p in rep.published if "8/17" in p["title"]][0]
        self.assertIn("from Twitch -> YouTube", plan)
        self.assertNotIn("Facebook", plan)

    def test_only_left_out_links_fails_clearly(self):
        self.cfg.raw.setdefault("platforms", {})["download_order"] = ["youtube"]
        c = Candidate(key="restream:fb", source="restream", title="FB only",
                      started_at=int(self.now - 86400), finished_at=int(self.now - 82800),
                      urls=["https://www.facebook.com/x/videos/1"], fetch_audio=lambda w: None)
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=[c]):
            rep = runner.run(self.cfg)
        self.assertIn("left out of platforms.download_order: Facebook", rep.failed[0]["error"])

    def test_bad_order_name(self):
        self.cfg.raw.setdefault("platforms", {})["download_order"] = ["youtube", "tiktok"]
        rep = runner.run(self.cfg)
        self.assertIn("'tiktok' isn't a platform", rep.errors[0])

    def test_order_as_text(self):
        from castsync.models import download_order
        self.cfg.raw.setdefault("platforms", {})["download_order"] = "youtube > fb > twitch"
        self.assertEqual(download_order(self.cfg), ("youtube", "facebook", "twitch"))

    def test_platform_scan_supplies_missing_links(self):
        rs = [c for c in self.restream_cands({}) if c.key == "restream:nolinks"][0]
        yt = Candidate(key="youtube:ZZZ", source="youtube", title="different title",
                       started_at=rs.started_at + 600, urls=["https://www.youtube.com/watch?v=ZZZ"])
        cands = [rs, yt]
        runner.attach_platform_urls(cands)
        self.assertEqual(rs.urls, ["https://www.youtube.com/watch?v=ZZZ"])

    def test_no_source_fails_with_clear_message(self):
        cands = [c for c in self.restream_cands({}) if c.key == "restream:nolinks"]
        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=cands):
            rep = runner.run(self.cfg)
        self.assertIn("platforms.channels", rep.failed[0]["error"])


class TitleTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.ts = int(datetime(2026, 11, 2, 17, 56).timestamp())

    def render(self, tmpl, stream="PTR Radio (11/2/2026) - we're sooo tired", **ep):
        from castsync.titles import render_title
        cfg = Config(raw={"general": {"show_name": "PTR Radio"},
                          "episode": {"title_template": tmpl, **ep}}, path=Path("c"))
        return render_title(cfg, stream, self.ts)

    def test_users_goal(self):
        self.assertEqual(self.render("PTR Radio ({short_date}) - {topic}"),
                         "PTR Radio (11/2/2026) - we're sooo tired")
        self.assertEqual(self.render("{show} ({short_date}) - {topic}"),
                         "PTR Radio (11/2/2026) - we're sooo tired")

    def test_old_template_explains_doubling(self):
        self.assertEqual(self.render("PTR Radio {pretty_date} - {title}"),
                         "PTR Radio November 2, 2026 - PTR Radio (11/2/2026) - we're sooo tired")

    def test_date_formats(self):
        self.assertEqual(self.render("{date} | {date:%m/%d/%y} | {date:%b %-d} | {pretty_date}"),
                         "2026-11-02 | 11/02/26 | Nov 2 | November 2, 2026")

    def test_topic_is_consistent_even_if_stream_title_varies(self):
        t = "PTR Radio ({short_date}) - {topic}"
        self.assertEqual(self.render(t, stream="PTR Radio November 2, 2026: we're sooo tired"),
                         "PTR Radio (11/2/2026) - we're sooo tired")
        self.assertEqual(self.render(t, stream="we're sooo tired | PTR Radio"),
                         "PTR Radio (11/2/2026) - we're sooo tired")

    def test_empty_topic_leaves_clean_title(self):
        self.assertEqual(self.render("PTR Radio ({short_date}) - {topic}", stream="PTR Radio (11/2/2026)"),
                         "PTR Radio (11/2/2026)")

    def test_stream_title_and_literal_braces(self):
        self.assertEqual(self.render("{{LIVE}} {stream_title}"),
                         "{LIVE} PTR Radio (11/2/2026) - we're sooo tired")

    def test_bad_placeholder_explained(self):
        with self.assertRaises(ValueError) as cm:
            self.render("{episode_name}")
        self.assertIn("{episode_name}", str(cm.exception))
        self.assertIn("{topic}", str(cm.exception))
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, episode={"title_template": "{nope}"})
        with mock.patch.object(runner, "gather") as g:
            rep = runner.run(cfg)
        g.assert_not_called()
        self.assertIn("{nope}", rep.errors[0])


class NumberingTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.ts = int(datetime(2026, 11, 2, 17, 56).timestamp())

    def num(self, **t):
        from castsync.transistor import episode_numbering
        return episode_numbering(Config(raw={"transistor": t}, path=Path("c")), self.ts)

    def test_default_is_date_number(self):
        self.assertEqual(self.num(), {"number": "20261102"})
        self.assertEqual(self.num(episode_number="none"), {})

    def test_date_number_and_season_year(self):
        self.assertEqual(self.num(episode_number="date", season="year"),
                         {"number": "20261102", "season": "2026"})
        self.assertEqual(self.num(episode_number="{date:%y%m%d}", season=1),
                         {"number": "261102", "season": "1"})

    def test_bad_values(self):
        with self.assertRaises(ValueError):
            self.num(episode_number="{date:%b}")       # letters
        with self.assertRaises(ValueError):
            self.num(season="spring")

    def test_sent_on_publish(self):
        tmp = Path(tempfile.mkdtemp())
        from castsync.transistor import TransistorClient
        cfg = make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42, "episode_number": "date"})
        fake = FakeTransistor()
        mp3 = tmp / "e.mp3"; mp3.write_bytes(b"x")
        TransistorClient(cfg, session=fake, sleep=lambda s: None).publish(str(mp3), "Ep", "", started_at=self.ts)
        self.assertEqual(fake.created[0]["episode[number]"], "20261102")
        self.assertNotIn("episode[season]", fake.created[0])


class RetitleTests(unittest.TestCase):
    def test_retitle_preview_then_apply(self):
        from castsync import cli
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42, "episode_number": "date"},
                       episode={"title_template": "PTR Radio ({short_date}) - {topic}"},
                       general={"show_name": "PTR Radio"})
        cfg.raw["destination"] = "transistor"
        from datetime import datetime
        ts = int(datetime(2026, 11, 2, 17, 56).timestamp())
        st = State(cfg.state_dir)
        st.record("restream:e1", "restream", "PTR Radio November 2, 2026 - PTR Radio (11/2/2026) - we're sooo tired",
                  "x", ts, "published", libsyn_ref="transistor:777")
        st.record("restream:gone", "restream", "Old one", "old one", ts - 400 * 86400, "published",
                  libsyn_ref="transistor:555")
        orig = Candidate(key="restream:e1", source="restream",
                         title="PTR Radio (11/2/2026) - we're sooo tired", started_at=ts)
        fake = FakeTransistor()
        import io, contextlib
        for apply in (False, True):
            out = io.StringIO()
            args = mock.Mock(apply=apply, days=180)
            with mock.patch("requests.Session", return_value=fake), \
                 mock.patch("castsync.transistor.time.sleep"), \
                 mock.patch("castsync.runner.gather", return_value=[orig]), \
                 contextlib.redirect_stdout(out):
                rc = cli.cmd_retitle(cfg, args)
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertIn("new: PTR Radio (11/2/2026) - we're sooo tired", text)
            self.assertIn("new: 20261102", text)
            self.assertIn("1 episode(s) skipped", text)
            if not apply:
                self.assertIn("Nothing changed yet", text)
                self.assertFalse(getattr(fake, "patched", []))
        self.assertEqual(fake.patched, [("/episodes/777",
                                         {"episode[title]": "PTR Radio (11/2/2026) - we're sooo tired",
                                          "episode[number]": "20261102"})])
        self.assertEqual(State(cfg.state_dir).get("restream:e1")["title"],
                         "PTR Radio (11/2/2026) - we're sooo tired")


class RetitleNumberDisplayTests(unittest.TestCase):
    def run_retitle(self, numbering, current_number=None):
        from castsync import cli
        import io, contextlib
        from datetime import datetime
        tmp = Path(tempfile.mkdtemp())
        t = {"api_key": "KEY", "show_id": 42}
        if numbering:
            t["episode_number"] = numbering
        cfg = make_cfg(tmp, transistor=t, general={"show_name": "PTR Radio"},
                       episode={"title_template": "PTR Radio ({short_date}) - {topic}"})
        cfg.raw["destination"] = "transistor"
        ts = int(datetime(2026, 11, 2, 17, 56).timestamp())
        State(cfg.state_dir).record("restream:e1", "restream", "x", "x", ts, "published",
                                    libsyn_ref="transistor:777")
        orig = Candidate(key="restream:e1", source="restream",
                         title="PTR Radio (11/2/2026) - we're sooo tired", started_at=ts)
        fake = FakeTransistor()
        if current_number:
            fake.current_number = current_number
        out = io.StringIO()
        with mock.patch("requests.Session", return_value=fake), \
             mock.patch("castsync.transistor.time.sleep"), \
             mock.patch("castsync.runner.gather", return_value=[orig]), contextlib.redirect_stdout(out):
            cli.cmd_retitle(cfg, mock.Mock(apply=False, days=180))
        return out.getvalue()

    def test_default_numbers_by_date(self):
        text = self.run_retitle(None)
        self.assertIn("episode #  now: -", text)
        self.assertIn("new: 20261102", text)

    def test_correct_number_still_shown(self):
        text = self.run_retitle("date", current_number=20261102)
        self.assertIn("episode #  20261102 (unchanged)", text)

    def test_none_explained(self):
        text = self.run_retitle("none")
        self.assertIn("episode numbers are left as they are", text)


class BatchPauseTests(unittest.TestCase):
    def client(self, slept, **t):
        from castsync.transistor import TransistorClient
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42, **t})
        return TransistorClient(cfg, session=FakeTransistor(), sleep=slept.append)

    def test_pause_30s_after_every_10_requests(self):
        slept = []
        c = self.client(slept)
        for i in range(25):
            c.update(str(i), {"number": i})
        self.assertEqual(slept.count(30), 2)        # after request 10 and 20, not before 1st
        c2 = self.client(s2 := [])
        for i in range(10):
            c2.update(str(i), {"number": i})
        self.assertNotIn(30, s2)                    # exactly 10 -> no pause needed

    def test_configurable_and_off(self):
        slept = []
        c = self.client(slept, batch_size=5, batch_pause_seconds=12)
        for i in range(11):
            c.update(str(i), {"number": i})
        self.assertEqual(slept.count(12), 2)
        slept2 = []
        c = self.client(slept2, batch_size=0)
        for i in range(30):
            c.update(str(i), {"number": i})
        self.assertNotIn(30, slept2)

    def test_bad_setting(self):
        from castsync.transistor import batch_settings
        with self.assertRaises(ValueError):
            batch_settings(Config(raw={"transistor": {"batch_size": "lots"}}, path=Path("c")))

    def test_estimate_shown_in_preview(self):
        from castsync.transistor import estimate_seconds
        cfg = Config(raw={}, path=Path("c"))
        self.assertAlmostEqual(estimate_seconds(cfg, 25), 25 * 1.05 + 2 * 30)
        self.assertAlmostEqual(estimate_seconds(cfg, 10), 10 * 1.05)
        from castsync.cli import _duration
        self.assertEqual(_duration(86.25), "1 min 26 s")


class RenumberTests(unittest.TestCase):
    def test_renumber_whole_show(self):
        from castsync import cli
        import io, contextlib
        from datetime import datetime
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42,
                                        "episode_number": "date", "season": "year"})
        cfg.raw["destination"] = "transistor"
        eps = [
            # 0: uploaded by castsync; air date known (Nov 2) though published Nov 3 UTC
            {"title": "Ep about tiredness", "status": "published",
             "published_at": "2026-11-03T03:00:00Z", "number": None, "season": 2018},
            # 1: manual upload, date in title
            {"title": "PTR Radio (10/19/2026) - Number 3 leads to Number 2", "status": "published",
             "published_at": "2026-10-20T10:00:00Z", "number": None, "season": 2018},
            # 2: no date in title -> publish date
            {"title": "Christmas special", "status": "published",
             "published_at": "2025-12-24T18:00:00Z", "number": 7, "season": 2018},
            # 3: draft without a date -> left alone
            {"title": "Untitled draft", "status": "draft", "published_at": None,
             "created_at": "2026-11-05T00:00:00Z"},
            # 4+5: same day -> same number -> left alone (season still fixed)
            {"title": "PTR Radio 9/1/2026 part 1", "status": "published",
             "published_at": "2026-09-02T00:00:00Z", "season": 2018},
            {"title": "PTR Radio 9/1/2026 part 2", "status": "published",
             "published_at": "2026-09-02T01:00:00Z", "season": 2018},
            # 6: already right
            {"title": "PTR Radio (8/17/2026)", "status": "published",
             "published_at": "2026-08-18T00:00:00Z", "number": 20260817, "season": 2026},
        ]
        fake = FakeTransistor(eps)
        air = int(datetime(2026, 11, 2, 17, 56).timestamp())
        State(cfg.state_dir).record("restream:a", "restream", "x", "x", air, "published",
                                    libsyn_ref="transistor:0")
        outputs = []
        for apply in (False, True):
            out = io.StringIO()
            with mock.patch("requests.Session", return_value=fake), \
                 mock.patch("castsync.transistor.time.sleep"), contextlib.redirect_stdout(out):
                rc = cli.cmd_renumber(cfg, mock.Mock(apply=apply))
            self.assertEqual(rc, 0, out.getvalue())
            outputs.append(out.getvalue())
        preview = outputs[0]
        self.assertIn("7 episode(s) on the show; 5 need changes", preview)
        self.assertIn("dated by air date", preview)
        self.assertIn("dated by date in title", preview)
        self.assertIn("dated by publish date", preview)
        self.assertIn("Untitled draft", preview.split("No date found")[1])
        self.assertIn("20260901: PTR Radio 9/1/2026 part 1 | PTR Radio 9/1/2026 part 2", preview)
        self.assertIn("Nothing changed yet", preview)
        self.assertIn("10 at a time with a 30-second pause between batches", preview)
        patched = dict(fake.patched)
        self.assertEqual(patched["/episodes/0"], {"episode[number]": "20261102", "episode[season]": "2026"})
        self.assertEqual(patched["/episodes/1"], {"episode[number]": "20261019", "episode[season]": "2026"})
        self.assertEqual(patched["/episodes/2"]["episode[number]"], "20251224")
        self.assertEqual(patched["/episodes/2"]["episode[season]"], "2025")
        self.assertEqual(patched["/episodes/4"], {"episode[season]": "2026"})   # number left alone
        self.assertNotIn("/episodes/6", patched)
        self.assertNotIn("/episodes/3", patched)

    def test_needs_settings(self):
        from castsync import cli
        import io, contextlib
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, transistor={"api_key": "KEY", "show_id": 42, "episode_number": "none"})
        cfg.raw["destination"] = "transistor"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.cmd_renumber(cfg, mock.Mock(apply=False))
        self.assertEqual(rc, 1)
        self.assertIn("episode_number: date", out.getvalue())


class HelperTests(unittest.TestCase):
    def test_feed_titles(self):
        resp = mock.Mock(content=FEED, raise_for_status=lambda: None)
        sess = mock.Mock(get=mock.Mock(return_value=resp))
        self.assertEqual(feed_titles("u", session=sess), {"ep 41 old news", "ep 40 older"})

    def test_feed_items(self):
        xml = b"""<rss><channel><item><title>Ep 9</title>
        <enclosure url="https://traffic.libsyn.com/x/2026-11-09-ep.mp3?dest-id=1" length="28800000"/>
        <pubDate>Mon, 09 Nov 2026 18:00:00 +0000</pubDate></item></channel></rss>"""
        resp = mock.Mock(content=xml, raise_for_status=lambda: None)
        items = feed_items("u", session=mock.Mock(get=mock.Mock(return_value=resp)))
        self.assertEqual(items, [{"title": "Ep 9", "file": "2026-11-09-ep.mp3", "year": 2026,
                                  "pub_ts": 1794247200, "bytes": 28800000}])

    def test_find_file_id(self):
        data = {"_embedded": {"ftp": [{"content_id": 11, "file_name": "a.mp3"},
                                      {"content_id": 12, "file_name": "2026-11-09-ep.mp3"}]}}
        self.assertEqual(_find_file_id(data, "2026-11-09-ep.mp3"), 12)


class EndToEndTests(unittest.TestCase):
    def test_run_restream_to_ftp(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, general={"keep_files": "converted"})
        src_audio = tone(str(tmp / "rec.m4a"))
        now = time.time()

        def fetch(work_dir):
            dest = os.path.join(work_dir, "restream-audio.m4a")
            subprocess.run(["cp", src_audio, dest], check=True)
            return dest

        cands = [Candidate(key="restream:abc", source="restream", title="Ep 42: Big News",
                           description="Show notes", started_at=int(now - 7200),
                           finished_at=int(now - 3600), duration_s=3600, fetch_audio=fetch)]
        uploaded = []
        with mock.patch.object(runner, "existing_items", return_value=[{"title": "Ep 41: Old News", "file": "", "year": 2026}]), \
             mock.patch.object(runner, "gather", return_value=cands), \
             mock.patch.object(LibsynPublisher, "ftp_upload",
                               side_effect=lambda p: uploaded.append(p) or os.path.basename(p)):
            rep = runner.run(cfg)
            self.assertEqual(len(rep.published), 1, rep.text())
            mp3 = rep.published[0]["mp3"]
            self.assertTrue(mp3.endswith(".mp3") and os.path.exists(mp3))
            info = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                   "format=format_name:format_tags=title", "-of", "csv=p=0", mp3],
                                  capture_output=True, text=True).stdout
            self.assertIn("mp3", info)
            self.assertIn("Ep 42: Big News", info)
            self.assertEqual(uploaded, [mp3])
            self.assertIn("DONE", rep.text())
            # Next hour: nothing to do, no email-worthy work
            rep2 = runner.run(cfg)
            self.assertFalse(rep2.did_work, rep2.text())

    def test_restream_missing_recording_falls_back_to_ytdlp(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        src_audio = tone(str(tmp / "yt.webm.m4a"))
        now = time.time()
        cands = [Candidate(key="restream:def", source="restream", title="Ep 43",
                           started_at=int(now - 7200), finished_at=int(now - 3600),
                           urls=["https://youtube.com/watch?v=x"],
                           fetch_audio=lambda w: None)]

        def fake_ytdlp(cfg_, urls, work, stem):
            dest = os.path.join(work, stem + ".m4a")
            subprocess.run(["cp", src_audio, dest], check=True)
            return dest

        with mock.patch.object(runner, "existing_items", return_value=[]), \
             mock.patch.object(runner, "gather", return_value=cands), \
             mock.patch.object(runner, "download_audio_with_ytdlp", side_effect=fake_ytdlp), \
             mock.patch.object(LibsynPublisher, "ftp_upload", return_value="f.mp3"):
            rep = runner.run(cfg)
        self.assertEqual(len(rep.published), 1, rep.text())
        self.assertIn("yt-dlp", " ".join(rep.published[0]["notes"]))

    def test_feed_failure_uploads_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp)
        with mock.patch.object(runner, "existing_items", side_effect=OSError("down")), \
             mock.patch.object(runner, "gather") as g:
            rep = runner.run(cfg)
        g.assert_not_called()
        self.assertTrue(rep.errors and rep.did_work)

    def test_api_mode_creates_post(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(tmp, libsyn={"mode": "api", "publish_as_draft": False,
                                    "api": {"show_id": 77, "client_id": "c", "client_secret": "s",
                                            "username": "u", "password": "p"}})
        pub = LibsynPublisher(cfg, cfg.state_dir, session=mock.Mock())
        pub.http.post.side_effect = [
            mock.Mock(status_code=200, json=lambda: {"access_token": "T", "refresh_token": "R",
                                                     "expires_in": 3600}),
            mock.Mock(status_code=200, content=b"{}", json=lambda: {"_embedded": {"post": {"item_id": 999}}}),
        ]
        pub.http.get.return_value = mock.Mock(
            raise_for_status=lambda: None,
            json=lambda: [{"content_id": 555, "file_name": "ep.mp3"}])
        with mock.patch.object(pub, "ftp_upload", return_value="ep.mp3"):
            ref, msg = pub.publish("/x/ep.mp3", "Ep 1", "notes")
        self.assertEqual(ref, "post:999")
        self.assertIn("published", msg)
        form = pub.http.post.call_args_list[1].kwargs["files"]
        self.assertEqual(form["primary_content_url"], (None, "libsyn-ftp-555"))
        self.assertEqual(form["is_draft"], (None, "false"))
        self.assertEqual(form["show_id"], (None, "77"))


if __name__ == "__main__":
    unittest.main()
