#!/usr/bin/env python3
"""Offline tests for digest_bot.py. Standard library only (unittest, mock).

Run from the repo root:  python -m unittest discover -s tests -v

Nothing here touches the network, Discord, or Anthropic. Every network edge
(urllib.request.urlopen, socket.getaddrinfo, subprocess.run) is patched to
fail loudly if a test reaches it without setting up a fake first. All names,
ids and channels below are invented.
"""

from __future__ import annotations

import copy
import io
import json
import re
import shutil
import sys
import tempfile
import time as real_time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import digest_bot as db  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    CHI = ZoneInfo("America/Chicago")
except Exception:  # no tzdata on this machine
    CHI = None

SM = db.SPLIT_MARK
ZW = chr(0x200B)
EM = chr(0x2014)

# A config shaped like the first public release (commit ebc56c6): none of the
# keys added since. It must still load.
OLD_CONFIG = {
    "discord": {"guild_id": "G1", "token_file": "./secrets/discord_token.txt",
                "read_channels": ["general", "help"]},
    "writer": {"backend": "anthropic", "model": "test-model", "max_tokens": 2000,
               "claude_cli_path": "claude"},
    "limits": {"max_pings": 6, "message_char_limit": 2000, "max_messages": 2,
               "min_digest_chars": 200, "max_fetch_per_channel": 1000},
    "periods": {
        "weekly": {"digest_channel_id": "W1", "armed": False, "window_days": 7,
                   "anchored": False, "min_messages": 0, "resolve_mentions": False,
                   "channel_name": "#weekly-digest", "digest_instructions": "Write a weekly digest."},
        "daily": {"digest_channel_id": "D1", "armed": False, "window_days": 1,
                  "anchored": True, "anchor_hour": 23, "min_messages": 8,
                  "resolve_mentions": True, "channel_name": "#daily-digest",
                  "digest_instructions": "Write a daily digest."},
    },
}

BOT_ID = "900"
OTHER_BOT_ID = "901"
CT = timezone(timedelta(hours=-5))  # fixed offset for fake message timestamps
END = datetime(2026, 9, 29, 23, 0, tzinfo=CT)
TODAY_START = END - timedelta(days=1)
DAILY_HEAD = "Daily Digest, Tue Sep 29"


def _forbid(name):
    def fail(*a, **k):
        raise AssertionError(f"test reached the real {name}")
    return fail


def msg(i, when, bot=False, text=None, author=None):
    """A raw Discord message as the REST API returns it."""
    aid = str(author if author is not None else i)
    return {"id": str(1000 + i), "timestamp": when.isoformat(),
            "author": {"id": aid, "username": f"user{aid}", "bot": bot},
            "content": f"message {i}" if text is None else text, "reactions": []}


def own(when, content=DAILY_HEAD.replace("Tue Sep 29", "Sun Sep 27") + "\n\nold digest",
        author=BOT_ID):
    """A message in the digest channel, by default this bot's own post."""
    return {"id": "d" + when.isoformat(), "timestamp": when.isoformat(),
            "author": {"id": author, "bot": True}, "content": content}


def body_text(n=8):
    return "\n\nToday\n" + ("Plenty happened in the room today. " * n).strip()


class FakeDiscord:
    """A stand-in for api(). channels maps a channel or thread name to its raw
    messages; store is the digest channel, newest first; threads is the
    active-thread list or an Exception to raise."""

    def __init__(self, cfg, channels=None, store=None, threads=None, names=None,
                 accept_posts=False, post_fail_at=None):
        self.cfg = cfg
        self.channels = channels or {}
        self.store = store if store is not None else []
        self.threads = threads
        self.names = names
        self.accept_posts = accept_posts
        self.post_fail_at = post_fail_at
        self.posts = []
        self.calls = []

    def __call__(self, path, token, method="GET", body=None):
        self.calls.append((method, path))
        cfg = self.cfg
        if method != "GET":
            if not self.accept_posts:
                raise AssertionError("test tried to POST: " + path)
            self.posts.append(body)
            if self.post_fail_at == len(self.posts):
                raise RuntimeError("HTTP Error 502: Bad Gateway")
            self.store.insert(0, own(datetime.now(timezone.utc), body["content"]))
            return {}
        if path == "/users/@me":
            return {"id": BOT_ID, "bot": True}
        if path == f"/guilds/{cfg.guild_id}/threads/active":
            if isinstance(self.threads, Exception):
                raise self.threads
            return {"threads": list(self.threads or []), "members": []}
        if path == f"/guilds/{cfg.guild_id}/channels":
            names = cfg.read_channels if self.names is None else self.names
            return [{"name": n, "id": f"c-{n}", "type": 0} for n in names]
        if path.startswith(f"/channels/{cfg.digest_channel}/messages"):
            return list(self.store)
        m = re.match(r"/channels/([ct])-([^/]+)/messages", path)
        if m:
            if "before=" in path:
                return []
            msgs = self.channels.get(m.group(2), [])
            return sorted(msgs, key=lambda x: x["timestamp"], reverse=True)
        raise AssertionError("unexpected api path: " + path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ddb_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("WORK_DIR", "LOGS_DIR", "OUT_DIR"):
            p = mock.patch.object(db, name, self.tmp / name.lower())
            p.start()
            self.addCleanup(p.stop)
        patches = [
            mock.patch.object(db, "time", SimpleNamespace(sleep=lambda s: None,
                                                          monotonic=real_time.monotonic)),
            mock.patch("urllib.request.urlopen", _forbid("urlopen")),
            mock.patch("socket.getaddrinfo", _forbid("getaddrinfo")),
            mock.patch("subprocess.run", _forbid("subprocess.run")),
            mock.patch.dict("os.environ", {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        db.os.environ.pop("ANTHROPIC_API_KEY", None)
        db.os.environ.pop("DISCORD_BOT_TOKEN", None)

    def make_cfg(self, period="daily", base=None, **over):
        """A Config from a temp JSON. Keys named discord, writer, limits or
        link_context merge into that section; anything else goes into the
        period block."""
        data = copy.deepcopy(base or OLD_CONFIG)
        for k, v in over.items():
            if k in ("discord", "writer", "limits", "link_context"):
                data.setdefault(k, {}).update(v)
            else:
                data["periods"][period][k] = v
        path = self.tmp / f"cfg_{len(list(self.tmp.glob('cfg_*')))}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return db.load_config(path, period)

    def log_text(self, cfg):
        p = db.LOGS_DIR / f"{cfg.period}_digest.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


# ── 1. Config ─────────────────────────────────────────────────────────────────

class ConfigTests(Base):
    def test_old_config_loads_with_defaults(self):
        d = self.make_cfg("daily")
        w = self.make_cfg("weekly")
        self.assertTrue(d.read_threads)
        self.assertEqual(d.header, "Daily Digest, {date}")
        self.assertEqual(w.header, "")
        self.assertTrue(d.carryover)
        self.assertFalse(w.carryover)
        self.assertEqual((d.anchor_grace_minutes, d.max_carry_days, d.max_quiet_runs), (30, 7, 3))
        self.assertFalse(d.tag_all_named)
        self.assertEqual((d.target_chars, d.trim_max_over, d.min_part_chars), (1600, 200, 40))
        self.assertEqual(d.min_digest_chars, 200)
        self.assertEqual(d.writer_timeout_seconds, 600)
        self.assertIsNone(d.writer_effort)
        self.assertEqual(d.writer_max_tokens, 2000)  # the file's own value wins
        self.assertFalse(d.link_enabled)
        self.assertEqual((d.link_max_links, d.link_max_bytes, d.link_summary_chars), (8, 200_000, 2500))

    def test_max_tokens_default(self):
        data = copy.deepcopy(OLD_CONFIG)
        del data["writer"]["max_tokens"]
        self.assertEqual(self.make_cfg("daily", base=data).writer_max_tokens, 16000)

    def test_period_min_digest_chars_overrides_limits(self):
        self.assertEqual(self.make_cfg("weekly", min_digest_chars=40).min_digest_chars, 40)

    def test_example_config_loads_both_periods(self):
        ex = ROOT / "config.example.json"
        for period in ("weekly", "daily"):
            cfg = db.load_config(ex, period)
            self.assertEqual(cfg.period, period)
            self.assertTrue(cfg.digest_instructions)

    def test_missing_period_is_a_clear_error(self):
        path = self.tmp / "c.json"
        path.write_text(json.dumps(OLD_CONFIG), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            db.load_config(path, "monthly")


# ── 2. Hygiene ────────────────────────────────────────────────────────────────

class HygieneTests(unittest.TestCase):
    FILES = ("digest_bot.py", "config.example.json", "README.md")

    def test_no_ids_paths_or_keys(self):
        for name in self.FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"(?<!\d)\d{17,20}(?!\d)", text))
                for bad in ("Users" + "\\", "/home/", "sk-" + "ant-"):
                    self.assertFalse(bad in text, f"{name} contains {bad!r}")


# ── 3. api() retries ──────────────────────────────────────────────────────────

class _Resp:
    def read(self):
        return b'{"ok": 1}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_err(code, body=b"{}"):
    return urllib.error.HTTPError("https://discord.com/x", code, "err", {}, io.BytesIO(body))


class ApiRetryTests(Base):
    def run_api(self, script, method="GET"):
        calls, sleeps = [], []

        def urlopen(req, timeout=None):
            calls.append(req.get_method())
            if len(calls) <= len(script):
                raise script[len(calls) - 1]
            return _Resp()
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(db, "time", SimpleNamespace(sleep=sleeps.append)), \
                redirect_stderr(io.StringIO()):
            try:
                got = db.api("/x", "tok", method, {"a": 1} if method != "GET" else None)
            except Exception as e:
                got = e
        return got, len(calls), sleeps

    def test_502_then_success(self):
        self.assertEqual(self.run_api([_http_err(502)]), ({"ok": 1}, 2, [2]))

    def test_timeout_and_connection_error(self):
        got = self.run_api([TimeoutError("t"), urllib.error.URLError("reset")])
        self.assertEqual(got, ({"ok": 1}, 3, [2, 5]))

    def test_three_server_errors_fail(self):
        got, n, sleeps = self.run_api([_http_err(502), _http_err(503), _http_err(500)])
        self.assertIsInstance(got, urllib.error.HTTPError)
        self.assertEqual((got.code, n, sleeps), (500, 3, [2, 5]))

    def test_404_not_retried(self):
        got, n, _ = self.run_api([_http_err(404)])
        self.assertIsInstance(got, urllib.error.HTTPError)
        self.assertEqual(n, 1)

    def test_post_server_error_not_retried(self):
        got, n, _ = self.run_api([_http_err(502)], method="POST")
        self.assertIsInstance(got, urllib.error.HTTPError)
        self.assertEqual(n, 1)

    def test_rate_wait_capped_at_60(self):
        got, _, sleeps = self.run_api([_http_err(429, b'{"retry_after": 600}')])
        self.assertEqual((got, sleeps), ({"ok": 1}, [60]))

    def test_total_wait_capped(self):
        got, n, sleeps = self.run_api([_http_err(429, b'{"retry_after": 59.5}') for _ in range(3)])
        self.assertIsInstance(got, RuntimeError)
        self.assertLessEqual(sum(sleeps), 120)
        self.assertEqual(n, 3)

    def test_four_rate_limit_attempts(self):
        got, n, sleeps = self.run_api([_http_err(429, b'{"retry_after": 0.1}') for _ in range(4)])
        self.assertIsInstance(got, RuntimeError)
        self.assertEqual((n, len(sleeps)), (4, 3))


# ── 4. Window ─────────────────────────────────────────────────────────────────

@unittest.skipUnless(CHI is not None, "tzdata for America/Chicago not available")
class WindowTests(Base):
    def wb(self, cfg, now):
        return db.window_bounds(cfg, now=now, tz=CHI)

    def local(self, dt):
        return dt.astimezone(CHI).replace(tzinfo=None)

    def test_anchor_23(self):
        cfg = self.make_cfg("daily")
        s1, e1 = self.wb(cfg, datetime(2026, 9, 29, 23, 0, 1))
        self.assertEqual((self.local(s1), self.local(e1)),
                         (datetime(2026, 9, 28, 23), datetime(2026, 9, 29, 23)))
        self.assertEqual(self.wb(cfg, datetime(2026, 9, 30, 7, 0)), (s1, e1))
        self.assertEqual(self.wb(cfg, datetime(2026, 9, 29, 22, 45)), (s1, e1))
        _, e4 = self.wb(cfg, datetime(2026, 9, 29, 22, 29))
        self.assertEqual(self.local(e4), datetime(2026, 9, 28, 23))

    def test_dst_nights_meet_exactly(self):
        cfg = self.make_cfg("daily")
        for first in (datetime(2026, 10, 29, 23, 0, 1), datetime(2027, 3, 12, 23, 0, 1)):
            nights = [self.wb(cfg, first + timedelta(days=i)) for i in range(5)]
            with self.subTest(first=first):
                self.assertTrue(all(nights[i][1] == nights[i + 1][0] for i in range(4)))
                spans = sorted({e - s for s, e in nights})
                self.assertEqual(len(spans), 2)
                self.assertIn(timedelta(hours=24), spans)
                self.assertTrue(timedelta(hours=23) in spans or timedelta(hours=25) in spans)

    def test_anchor_midnight(self):
        cfg = self.make_cfg("daily", anchor_hour=0)
        _, e = self.wb(cfg, datetime(2026, 10, 14, 23, 40))
        self.assertEqual(self.local(e), datetime(2026, 10, 15, 0))
        _, e = self.wb(cfg, datetime(2026, 10, 15, 0, 5))
        self.assertEqual(self.local(e), datetime(2026, 10, 15, 0))

    def test_window_day_is_midpoint(self):
        cfg = self.make_cfg("daily")
        b = self.wb(cfg, datetime(2026, 9, 30, 7, 0))
        self.assertEqual(db.window_day(cfg, b, tz=CHI), date(2026, 9, 29))
        cfg0 = self.make_cfg("daily", anchor_hour=0)
        b = self.wb(cfg0, datetime(2026, 10, 15, 0, 5))
        self.assertEqual(db.window_day(cfg0, b, tz=CHI), date(2026, 10, 14))

    def test_render_header_date_and_range(self):
        cfg = self.make_cfg("daily")
        b = self.wb(cfg, datetime(2026, 9, 30, 7, 0))
        day = db.window_day(cfg, b, tz=CHI)
        self.assertEqual(db.render_header(cfg, b, day, tz=CHI), DAILY_HEAD)
        wk = self.make_cfg("weekly", header="Weekly Digest, {range}")
        b = self.wb(wk, datetime(2026, 10, 14, 18, 0))
        self.assertEqual(db.render_header(wk, b, date(2026, 10, 14), tz=CHI), "Weekly Digest, Oct 8-14")
        self.assertEqual(db.render_header(wk, b, date(2026, 10, 6), tz=CHI), "Weekly Digest, Sep 30-Oct 6")
        self.assertEqual(db.render_header(self.make_cfg("weekly"), b, date(2026, 10, 6)), "")

    def test_header_is_scrubbed(self):
        cfg = self.make_cfg("weekly", header="@everyone Weekly " + EM + " {range}")
        b = self.wb(cfg, datetime(2026, 10, 14, 18, 0))
        got = db.render_header(cfg, b, date(2026, 10, 14), tz=CHI)
        self.assertNotIn(EM, got)
        self.assertIn("@" + ZW + "everyone", got)


# ── 8. Mentions ───────────────────────────────────────────────────────────────

class MentionTests(Base):
    def rm(self, text, authors, channels=None, max_pings=6):
        return db.resolve_mentions(text, authors, channels, max_pings)

    def test_name_key(self):
        self.assertEqual(db.name_key("Rook" + chr(0x2019) + "s Nest"), "rook's nest")
        self.assertEqual(db.name_key(chr(0x1F680) + "Lantern"), "lantern")
        self.assertEqual(db.name_key("Zo" + chr(0xEB)), "zo" + chr(0xEB))
        self.assertNotEqual(db.name_key("Zo" + chr(0xEB)), "zoe")

    def test_tags_match_past_quotes_and_emoji(self):
        authors = {"1": "Rook" + chr(0x2019) + "s Nest", "2": chr(0x1F680) + "Lantern",
                   "3": "Pine Cone"}
        self.assertEqual(self.rm("{{Rook's Nest}}", authors), "<@1>")
        self.assertEqual(self.rm("{{Lantern}}", authors), "<@2>")
        self.assertEqual(self.rm("{{pine cone}}", authors), "<@3>")
        self.assertEqual(self.rm("{{Nobody}}", authors), "Nobody")

    def test_shared_display_name_stays_plain(self):
        self.assertEqual(self.rm("{{Sam}} and {{Robin}}", {"71": "Sam", "72": "sam", "73": "Robin"}),
                         "Sam and <@73>")

    def test_mention_once_and_cap(self):
        authors = {"1": "Ann", "2": "Bob"}
        self.assertEqual(self.rm("{{Ann}} then {{Ann}}", authors), "<@1> then Ann")
        crowd = {str(500 + i): f"Person{i:02d}" for i in range(8)}
        got = self.rm(" ".join("{{" + n + "}}" for n in crowd.values()), crowd, max_pings=6)
        self.assertEqual(len(re.findall(r"<@\d+>", got)), 6)
        self.assertTrue(got.endswith("Person07"))

    def test_unknown_id_dropped_and_channels(self):
        got = self.rm("hi <@999> see {{#help}} and {{#nowhere}} and <#77>",
                      {"1": "Ann"}, {"help": "55"})
        self.assertNotIn("<@999>", got)
        self.assertIn("<#55>", got)
        self.assertIn("#nowhere", got)
        self.assertNotIn("<#77>", got)


# ── 9. Containment ────────────────────────────────────────────────────────────

class ContainmentTests(Base):
    AUTHORS = {"111": "Ann", "222": "Bob"}

    def test_scrub(self):
        raw = (f"Week: @everyone and @Here, role <@&555>, nick <@!111>, ghost <@999> {EM} done")
        got = db.scrub(raw, self.AUTHORS)
        self.assertIn("@" + ZW + "everyone", got)
        self.assertIn("@" + ZW + "Here", got)
        self.assertNotIn("<@&", got)
        self.assertIn("<@111>", got)
        self.assertNotIn("<@!", got)
        self.assertNotIn("<@999>", got)
        self.assertNotIn(EM, got)
        self.assertEqual(db.scrub(got, self.AUTHORS), got)

    def test_prepare_scrubs_both_periods(self):
        for period in ("weekly", "daily"):
            cfg = self.make_cfg(period)
            got = db.prepare_digest(cfg, "x @everyone <@&1> <@!111> {{Ann}} {{Ann}}", self.AUTHORS, {})
            with self.subTest(period=period):
                self.assertIn("@" + ZW + "everyone", got)
                self.assertNotIn("<@&", got)
                self.assertEqual(got.count("<@111>"), 1)

    def test_allowed_mentions_per_part(self):
        sent = []
        cfg = self.make_cfg("daily")
        with mock.patch.object(db, "api", lambda p, t, m="GET", b=None: sent.append(b) or {}):
            n = db.post(cfg, "hi <@111> and <@999> and <@111>\n" + SM + "\npart two <@222>",
                        "tok", self.AUTHORS)
        self.assertEqual(n, 2)
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": [], "users": ["111"]})
        self.assertEqual(sent[1]["allowed_mentions"], {"parse": [], "users": ["222"]})

    def test_no_users_when_mentions_off(self):
        sent = []
        cfg = self.make_cfg("weekly")
        with mock.patch.object(db, "api", lambda p, t, m="GET", b=None: sent.append(b) or {}):
            db.post(cfg, "hi <@111>", "tok", self.AUTHORS)
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": [], "users": []})

    def test_mention_users_capped_at_100(self):
        many = {str(10 ** 5 + i): "x" for i in range(150)}
        self.assertEqual(len(db.mention_users(" ".join(f"<@{k}>" for k in many), many)), 100)


# ── 10. Secret scan ───────────────────────────────────────────────────────────

class SecretScanTests(Base):
    def blocks(self, text, secrets=("tok-not-in-text",)):
        try:
            db.secret_scan(text, list(secrets))
            return False
        except RuntimeError as e:
            self.assertNotIn(text[-12:], str(e))
            return True

    def test_patterns(self):
        fake_tok = ".".join(['MTAxMjM0NTY3ODkwMTIzNDU2Nzg', 'GAbcDe', 'abcdefghijklmnopqrstuvwxyz0123456'])  # built at runtime so push protection does not flag a fake token
        self.assertTrue(self.blocks("leak: s3cr3t-bot-token-value", ["s3cr3t-bot-token-value"]))
        self.assertTrue(self.blocks(f"see {fake_tok} here"))
        self.assertTrue(self.blocks("key sk-" + "ant-api03-ABCDEFGHIJKLMNOPQRSTUV"))
        self.assertTrue(self.blocks("ghp_" + "a" * 36))
        self.assertTrue(self.blocks("AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertTrue(self.blocks("-----BEGIN RSA PRIVATE KEY"))
        self.assertFalse(self.blocks("A task flow; keys start with sk- and ghp_ per the docs."))

    def test_api_key_literal_and_empty_secret(self):
        self.assertTrue(self.blocks("the key is my-own-api-key-value", ["tok", "my-own-api-key-value"]))
        self.assertFalse(self.blocks("plain text", ["", None]))


if __name__ == "__main__":
    unittest.main()
