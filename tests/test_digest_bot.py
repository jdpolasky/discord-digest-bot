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
import urllib.request
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
            self.assertNotIn(SM, cfg.digest_instructions)
        wk, dy = db.load_config(ex, "weekly"), db.load_config(ex, "daily")
        self.assertEqual((wk.min_digest_chars, wk.min_messages, wk.header), (40, 0, "Weekly Digest, {range}"))
        self.assertEqual((dy.header, dy.anchor_grace_minutes, dy.max_quiet_runs), ("Daily Digest, {date}", 30, 3))
        self.assertTrue(dy.carryover and not dy.tag_all_named)
        self.assertEqual((wk.writer_model, wk.writer_max_tokens), ("claude-opus-5-5", 16000))
        self.assertFalse(wk.link_enabled)
        self.assertNotIn("Do not use @ mentions", wk.digest_instructions)

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
                for bad in ("Users" + "\\", "/" + "home/", "sk-" + "ant-"):
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


# ── 5. Reading ────────────────────────────────────────────────────────────────

class ReadTests(Base):
    def read(self, cfg, data, start=None, **env):
        fake = FakeDiscord(cfg, channels=data, **env)
        err = io.StringIO()
        with mock.patch.object(db, "api", fake), redirect_stderr(err):
            w = db.read_window(cfg, "tok", start, (TODAY_START, END))
        return w, fake, err.getvalue()

    def test_earlier_flag_drops_and_order(self):
        cfg = self.make_cfg("daily")
        carry = TODAY_START - timedelta(days=1)
        data = {
            "general": [msg(1, TODAY_START + timedelta(hours=2)),
                        msg(2, TODAY_START - timedelta(hours=3)),
                        msg(3, TODAY_START),                       # exactly the start: earlier
                        msg(4, TODAY_START + timedelta(hours=1), bot=True),
                        msg(5, carry - timedelta(hours=1))],       # before carry start
            "help": [msg(6, END), msg(7, END + timedelta(minutes=1))],
        }
        w, _, _ = self.read(cfg, data, carry)
        by_text = {m["text"]: m for v in w.values() for m in v}
        self.assertEqual(set(by_text), {"message 1", "message 2", "message 3", "message 6"})
        self.assertTrue(by_text["message 2"]["earlier"] and by_text["message 3"]["earlier"])
        self.assertNotIn("earlier", by_text["message 1"])
        self.assertNotIn("earlier", by_text["message 6"])
        self.assertEqual([m["text"] for m in w["general"]], ["message 2", "message 3", "message 1"])
        w, _, _ = self.read(cfg, data)
        self.assertEqual({m["text"] for v in w.values() for m in v}, {"message 1", "message 6"})

    def test_missing_channel_warns_and_continues(self):
        cfg = self.make_cfg("daily")
        data = {"general": [msg(1, TODAY_START + timedelta(hours=1))]}
        w, _, err = self.read(cfg, data, names=["general", "help-desk"])
        self.assertEqual(list(w), ["general"])
        self.assertIn("WARNING: #help", err)
        self.assertIn("WARNING: #help", self.log_text(cfg))

    def test_threads_merged_labeled_and_scoped(self):
        cfg = self.make_cfg("daily")
        data = {"general": [msg(1, TODAY_START + timedelta(hours=1)),
                            msg(3, TODAY_START + timedelta(hours=3))],
                "deep-dive": [msg(2, TODAY_START + timedelta(hours=2))],
                "off-list": [msg(4, TODAY_START + timedelta(hours=4))]}
        threads = [{"id": "t-deep-dive", "name": "deep-dive", "parent_id": "c-general"},
                   {"id": "t-off-list", "name": "off-list", "parent_id": "c-random"}]
        w, _, _ = self.read(cfg, data, threads=threads)
        got = w["general"]
        self.assertEqual([m["text"] for m in got], ["message 1", "message 2", "message 3"])
        self.assertEqual(got[1]["thread"], "deep-dive")
        self.assertNotIn("thread", got[0])
        self.assertNotIn("message 4", json.dumps(w))

    def test_thread_lookup_failure_warns(self):
        cfg = self.make_cfg("daily")
        data = {"general": [msg(1, TODAY_START + timedelta(hours=1))]}
        w, _, _ = self.read(cfg, data, threads=RuntimeError("threads down"))
        self.assertEqual([m["text"] for m in w["general"]], ["message 1"])
        self.assertIn("could not list active threads", self.log_text(cfg))

    def test_read_threads_off_makes_no_thread_call(self):
        cfg = self.make_cfg("daily", discord={"read_threads": False})
        _, fake, _ = self.read(cfg, {}, threads=RuntimeError("must not be called"))
        self.assertFalse(any("threads/active" in p for _, p in fake.calls))

    def test_substantive_counts(self):
        got = db.substantive_counts({"c": [{"text": "a"}, {"text": ""}, {"text": "b", "earlier": True},
                                           {"text": " ", "earlier": True}]})
        self.assertEqual(got, (1, 1))


# ── 6. Carryover and quiet runs ───────────────────────────────────────────────

class CarryTests(Base):
    def test_carry_start_for(self):
        f = db.carry_start_for
        self.assertEqual(f(TODAY_START, END, TODAY_START + timedelta(seconds=5), 7), TODAY_START)
        two = TODAY_START - timedelta(days=1) + timedelta(seconds=5)
        self.assertEqual(f(TODAY_START, END, two, 7), two)
        self.assertEqual(f(TODAY_START, END, END - timedelta(days=20), 7), END - timedelta(days=7))
        self.assertEqual(f(TODAY_START, END, END - timedelta(days=20), 3), END - timedelta(days=3))
        self.assertEqual(f(TODAY_START, END, None, 7), TODAY_START)

    def test_quiet_skip(self):
        cfg = self.make_cfg("daily")
        qs = db.quiet_skip
        two_back = END - timedelta(days=2) + timedelta(minutes=2)
        three_back = END - timedelta(days=3) + timedelta(minutes=2)
        self.assertFalse(qs(cfg, 2, 6, END, two_back))        # 8 together posts
        self.assertTrue(qs(cfg, 2, 5, END, two_back))         # 7, last post 2 back
        self.assertFalse(qs(cfg, 1, 2, END, three_back))      # 3 runs back: anything posts
        self.assertTrue(qs(cfg, 0, 0, END, END - timedelta(days=7)))  # zero never posts
        self.assertTrue(qs(cfg, 1, 2, END, None))             # no last post: count rule
        self.assertFalse(qs(cfg, 3, 5, END, None))
        off = self.make_cfg("daily", max_quiet_runs=0)
        self.assertTrue(qs(off, 1, 2, END, END - timedelta(days=5)))
        wk = self.make_cfg("weekly")
        self.assertFalse(qs(wk, 0, 0, None, None))

    @unittest.skipUnless(CHI is not None, "tzdata for America/Chicago not available")
    def test_carry_label_across_anchor(self):
        cfg = self.make_cfg("daily")
        at = datetime(2026, 9, 27, 23, 0).replace(tzinfo=CHI)
        self.assertEqual(db.carry_label(cfg, at, tz=CHI), ("Mon Sep 28", "2026-09-28"))
        before = datetime(2026, 9, 27, 22, 0).replace(tzinfo=CHI)
        self.assertEqual(db.carry_label(cfg, before, tz=CHI), ("Sun Sep 27", "2026-09-27"))

    def test_carry_paragraph_only_with_earlier(self):
        cfg = self.make_cfg("daily")
        s0, _ = db.build_prompt(cfg, {}, DAILY_HEAD, 0, None)
        s2, _ = db.build_prompt(cfg, {}, DAILY_HEAD, 2, "Mon Sep 28")
        self.assertNotIn("Since the last digest", s0)
        self.assertIn("Since the last digest", s2)
        self.assertIn("since Mon Sep 28", s2)


# ── Main-flow harness ─────────────────────────────────────────────────────────

class MainBase(Base):
    """Runs main() against FakeDiscord with a fake writer. The window is fixed
    at (TODAY_START, END) and the run's day at 2026-09-29."""

    def run_main(self, period="daily", channels=None, store=None, armed=False,
                 digest_text=None, writer_error=None, post_fail_at=None,
                 posts_fail=False, **over):
        data = copy.deepcopy(OLD_CONFIG)
        data["periods"][period]["armed"] = armed
        for k, v in over.items():
            if k in ("discord", "writer", "limits", "link_context"):
                data.setdefault(k, {}).update(v)
            else:
                data["periods"][period][k] = v
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps(data), encoding="utf-8")
        cfg = db.load_config(cfg_path, period)
        fake = FakeDiscord(cfg, channels=channels or {}, store=store,
                           accept_posts=armed, post_fail_at=post_fail_at)
        seen = {"writer": []}
        if digest_text is None:
            head = db.render_header(cfg, (TODAY_START, END), date(2026, 9, 29))
            digest_text = (head + "\n\n" if head else "") + "Summary" + body_text()

        def writer(c, system, user):
            seen["writer"].append((system, user))
            if writer_error:
                raise RuntimeError(writer_error)
            return digest_text

        bounds = (TODAY_START, END) if cfg.anchored else (END - timedelta(days=7), None)
        patches = [mock.patch.object(db, "api", fake),
                   mock.patch.object(db, "window_bounds", lambda c: bounds),
                   mock.patch.object(db, "window_day", lambda c, b: date(2026, 9, 29)),
                   mock.patch.object(db, "get_token", lambda c: "tok"),
                   mock.patch.object(db, "call_writer", writer)]
        if posts_fail:
            def boom(*a):
                raise RuntimeError("lookup failed")
            patches.append(mock.patch.object(db, "own_posts", boom))
        out, err = io.StringIO(), io.StringIO()
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    db.main(period, cfg_path)
                except SystemExit as e:
                    seen["exit"] = e.code
        finally:
            for p in reversed(patches):
                p.stop()
        seen.update(cfg=cfg, fake=fake, stdout=out.getvalue(), stderr=err.getvalue(),
                    log=self.log_text(cfg), dry=sorted(db.OUT_DIR.glob("*.md")) if db.OUT_DIR.exists() else [])
        return seen

    @staticmethod
    def busy(n=8, start=TODAY_START):
        return {"general": [msg(i, start + timedelta(minutes=10 * i)) for i in range(1, n + 1)]}


class MainFlowTests(MainBase):
    def test_today_plus_carried_counts(self):
        three_ten = {"general": [msg(i, TODAY_START + timedelta(hours=i)) for i in range(1, 4)]
                     + [msg(i, TODAY_START - timedelta(hours=i - 3)) for i in range(4, 14)]}
        got = self.run_main(channels=three_ten, store=[own(END - timedelta(days=2))])
        self.assertEqual(len(got["writer"]), 1)
        self.assertIn("Since the last digest", got["writer"][0][0])
        self.assertEqual(len(got["dry"]), 1)

    def test_quiet_day_skips(self):
        got = self.run_main(channels=self.busy(3), store=[own(END - timedelta(days=1))])
        self.assertEqual(got["writer"], [])
        self.assertIn("skipped, quiet day for 2026-09-29 (3 msgs)", got["log"])
        self.assertNotIn("exit", got)

    def test_carryover_off_reads_today_only(self):
        data = self.busy(2)
        data["general"] += [msg(50 + i, TODAY_START - timedelta(hours=i)) for i in range(1, 7)]
        got = self.run_main(channels=data, store=[own(END - timedelta(days=2))], carryover=False)
        self.assertEqual(got["writer"], [])
        self.assertIn("quiet day", got["log"])

    def test_another_bots_post_is_not_our_last_digest(self):
        data = {"general": [msg(i, TODAY_START + timedelta(hours=i)) for i in range(1, 3)]
                + [msg(50 + i, TODAY_START - timedelta(hours=i)) for i in range(1, 7)]}
        store = [own(END - timedelta(hours=1), "Some other bot says hi", author=OTHER_BOT_ID),
                 own(END - timedelta(days=2) + timedelta(minutes=2))]
        got = self.run_main(channels=data, store=store)
        self.assertEqual(len(got["writer"]), 1)

    def test_own_posts_filters_by_bot_id(self):
        cfg = self.make_cfg("daily")
        store = [own(END, author=OTHER_BOT_ID), own(END - timedelta(days=1))]
        with mock.patch.object(db, "api", FakeDiscord(cfg, store=store)):
            mine = db.own_posts("tok", cfg, db.bot_user_id("tok"))
        self.assertEqual([m["author"]["id"] for m in mine], [BOT_ID])
        self.assertEqual(db.last_digest_time(mine), END - timedelta(days=1))

    def test_success_log_has_carryover_count(self):
        data = self.busy(8)
        data["general"] += [msg(60, TODAY_START - timedelta(hours=5)),
                            msg(61, TODAY_START - timedelta(hours=6))]
        got = self.run_main(channels=data, store=[own(END - timedelta(days=2))], armed=True)
        self.assertNotIn("exit", got, got["stderr"])
        self.assertRegex(got["log"], r"posted 1 message\(s\), \d+ chars, digest for 2026-09-29, "
                                     r"including 2 carryover msg\(s\) since \d{4}-\d\d-\d\d")

    def test_rejected_draft_fails_loud(self):
        got = self.run_main(channels=self.busy(), armed=True, store=[],
                            digest_text="Sure! Here is the digest.\n" + DAILY_HEAD + body_text())
        self.assertEqual(got["exit"], 1)
        self.assertEqual(got["fake"].posts, [])
        fails = [ln for ln in got["log"].splitlines() if "FAILED" in ln]
        self.assertEqual(len(fails), 1)
        self.assertIn("preamble", fails[0])

    def test_secret_scan_failure_posts_nothing(self):
        for armed in (True, False):
            with self.subTest(armed=armed):
                got = self.run_main(channels=self.busy(), armed=armed, store=[],
                                    digest_text=DAILY_HEAD + body_text() + "\nghp_" + "a" * 36)
                self.assertEqual(got["exit"], 1)
                self.assertEqual(got["fake"].posts, [])
                self.assertEqual(got["dry"], [])
                self.assertIn("Secret scan", got["log"])

    def test_failed_log_line_is_one_line_and_capped(self):
        got = self.run_main(channels=self.busy(), writer_error="boom\nsecond line " + "y" * 600)
        line = [ln for ln in got["log"].splitlines() if "FAILED" in ln][0]
        self.assertIn("FAILED, nothing posted: boom second line", line)
        self.assertLess(len(line), 360)
        self.assertIn("y" * 500, got["stderr"])


# ── 7. Duplicates ─────────────────────────────────────────────────────────────

class DuplicateTests(MainBase):
    def test_header_mode(self):
        store = [own(END + timedelta(minutes=5), DAILY_HEAD + "\n\nposted earlier")]
        got = self.run_main(channels=self.busy(), armed=True, store=store)
        self.assertEqual(got["writer"], [])
        self.assertNotIn("exit", got)
        self.assertIn(f"skipped, already posted ({DAILY_HEAD})", got["log"])
        self.assertEqual(len(store), 1)

    def test_another_bot_does_not_block(self):
        store = [own(END + timedelta(minutes=5), DAILY_HEAD + "\n\nfake", author=OTHER_BOT_ID)]
        got = self.run_main(channels=self.busy(), armed=True, store=store)
        self.assertEqual(len(got["fake"].posts), 1)

    def test_rerun_after_part_two_failure(self):
        two_part = DAILY_HEAD + "\n\n" + "a" * 300 + "\n" + SM + "\n" + "b" * 300
        store = [own(END - timedelta(days=1), "Daily Digest, Mon Sep 28\n\nlast night")]
        got = self.run_main(channels=self.busy(), armed=True, store=store, post_fail_at=2,
                            digest_text=two_part)
        self.assertEqual(got["exit"], 1)
        self.assertIn("Post failed on message 2 of 2", got["log"])
        got = self.run_main(channels=self.busy(), armed=True, store=store, digest_text=two_part)
        self.assertEqual(got["writer"], [])
        self.assertEqual(len([m for m in store if m["content"].startswith(DAILY_HEAD)]), 1)

    def test_anchored_no_header_mode(self):
        store = [own(END + timedelta(minutes=10), "a digest with no header")]
        got = self.run_main(channels=self.busy(), armed=True, store=store, header="")
        self.assertEqual(got["writer"], [])
        self.assertIn("already posted (2026-09-29)", got["log"])
        store = [own(END - timedelta(hours=2), "yesterday's late post")]
        got = self.run_main(channels=self.busy(), armed=True, store=store, header="")
        self.assertEqual(len(got["fake"].posts), 1)

    def test_rolling_mode(self):
        today_noon = datetime(2026, 9, 29, 12, 0).astimezone()
        store = [own(today_noon, "Weekly recap")]
        got = self.run_main("weekly", channels=self.busy(), armed=True, store=store)
        self.assertEqual(got["writer"], [])
        self.assertIn("already posted", got["log"])
        store = [own(today_noon - timedelta(days=7), "Weekly recap")]
        got = self.run_main("weekly", channels=self.busy(), armed=True, store=store)
        self.assertEqual(len(got["fake"].posts), 1)

    def test_unarmed_still_writes_dry_run(self):
        store = [own(END + timedelta(minutes=5), DAILY_HEAD + "\n\nx")]
        got = self.run_main(channels=self.busy(), store=store)
        self.assertEqual(len(got["dry"]), 1)
        self.assertIn("2026-09-29", got["dry"][0].name)

    def test_failed_own_posts_read_means_no_duplicate_check(self):
        store = [own(END + timedelta(minutes=5), DAILY_HEAD + "\n\nposted earlier")]
        got = self.run_main(channels=self.busy(), armed=True, store=store, posts_fail=True)
        self.assertEqual(len(got["fake"].posts), 1)
        self.assertIn("could not read this bot's recent posts", got["log"])


# ── 11. Split, trim, validate ─────────────────────────────────────────────────

def sized(n, title=DAILY_HEAD):
    """A digest of exactly n chars: title plus four paragraphs, padded."""
    d = "\n\n".join([title] + ["p%d " % i + "w" * 440 for i in range(4)])
    return d + "e" * (n - len(d))


class SplitTrimValidateTests(Base):
    def fin(self, cfg, text, header=DAILY_HEAD):
        try:
            return db.finalize_digest(cfg, text, {}, {}, header)
        except RuntimeError as e:
            return e

    def test_auto_split(self):
        cfg = self.make_cfg("daily")
        blocks = [DAILY_HEAD] + ["p%d " % i + "w" * 490 for i in range(4)]
        long_d = "\n\n".join(blocks)
        long_d += "e" * (2011 - len(long_d))
        got = self.fin(cfg, long_d)
        parts = [p.strip() for p in got.split(SM)]
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(p) <= 2000 and len(p) >= 0.3 * 2011 for p in parts))
        self.assertEqual(parts[0] + "\n\n" + parts[1], long_d)

    def test_split_follows_message_char_limit(self):
        cfg = self.make_cfg("daily", limits={"message_char_limit": 1000, "trim_max_over": 0})
        got = self.fin(cfg, sized(1500))
        self.assertIsInstance(got, str)
        self.assertTrue(all(len(p.strip()) <= 1000 for p in got.split(SM)))

    def test_title_break_never_chosen(self):
        cfg = self.make_cfg("daily")
        title = DAILY_HEAD
        got = self.fin(cfg, title + "\n\n" + "r" * 1960 + "\n\n" + "q" * 60, header=title)
        self.assertIsInstance(got, str)
        self.assertIn("\n", got.split(SM)[0].strip())

    def test_no_split_when_max_messages_1(self):
        cfg = self.make_cfg("daily", limits={"max_messages": 1})
        got = self.fin(cfg, sized(2011))
        self.assertIsInstance(got, RuntimeError)
        self.assertIn("exceeds", str(got))

    def trim(self, first, trim, **over):
        cfg = self.make_cfg("daily", **over)
        calls = []

        def writer(c, system, user):
            calls.append(user)
            if len(calls) == 1:
                return first
            if isinstance(trim, Exception):
                raise trim
            return trim
        out = io.StringIO()
        with mock.patch.object(db, "call_writer", writer), redirect_stdout(out):
            try:
                got = db.write_digest(cfg, {}, {}, {}, DAILY_HEAD)
            except RuntimeError as e:
                got = e
        return got, calls, out.getvalue()

    def nparts(self, d):
        return len([p for p in d.split(SM) if p.strip()])

    def test_trim_one_call(self):
        got, calls, out = self.trim(sized(2010), sized(1900))
        self.assertEqual((len(calls), got), (2, sized(1900)))
        self.assertIn("10 characters over", calls[1])
        self.assertIn("at least 50 characters", calls[1])
        self.assertIn("{{name}}", calls[1])
        self.assertIn(sized(2010), calls[1])
        self.assertIn("2010 -> 1900", out)

    def test_trim_still_over_then_split(self):
        got, calls, _ = self.trim(sized(2010), sized(2005))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.nparts(got), 2)

    def test_over_band_no_trim(self):
        got, calls, out = self.trim(sized(2201), sized(1900))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.nparts(got), 2)
        self.assertNotIn("Trim", out)

    def test_trim_failure_falls_back(self):
        for bad in (RuntimeError("writer down"), ""):
            with self.subTest(bad=bad):
                got, calls, out = self.trim(sized(2010), bad)
                self.assertEqual(len(calls), 2)
                self.assertEqual(got.replace("\n" + SM + "\n", "\n\n"), sized(2010))
                self.assertIn("Trim pass failed; the split was used.", out)

    def test_trim_off(self):
        got, calls, _ = self.trim(sized(2010), sized(1900), limits={"trim_max_over": 0})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.nparts(got), 2)

    def test_validation_cases(self):
        cfg = self.make_cfg("daily")
        body = body_text()
        self.assertEqual(self.fin(cfg, DAILY_HEAD + body), DAILY_HEAD + body)
        cases = [
            ("wrong first line", "Daily Digest for Tuesday" + body, "first line"),
            ("wrong date", "Daily Digest, Mon Sep 28" + body, "first line"),
            ("preamble", "Sure! Here is the digest.\n" + DAILY_HEAD + body, "preamble"),
            ("code fence", DAILY_HEAD + "\n```\n" + body + "\n```", "code fence"),
            ("refusal", DAILY_HEAD + "\n\nI can't write this digest because " + "x" * 200, "refusal"),
            ("curly refusal", DAILY_HEAD + body + "\nI" + chr(0x2019) + "m unable to finish.", "refusal"),
            ("as an AI", DAILY_HEAD + body + "\nAs an AI, I should note...", "refusal"),
            ("tiny second part", DAILY_HEAD + body + "\n" + SM + "\nBye all!", "message 2"),
        ]
        for name, text, word in cases:
            with self.subTest(case=name):
                got = self.fin(cfg, text)
                self.assertIsInstance(got, RuntimeError)
                self.assertIn(word, str(got))

    def test_quoted_member_line_passes(self):
        cfg = self.make_cfg("daily")
        text = DAILY_HEAD + body_text() + '\n"I can\'t get the plugin to load," one member said.'
        self.assertIsInstance(self.fin(cfg, text), str)

    def test_empty_header_skips_header_check(self):
        cfg = self.make_cfg("weekly")
        self.assertIsInstance(self.fin(cfg, "Anything at all" + body_text(), header=""), str)

    def test_per_period_min_digest_chars(self):
        wk = self.make_cfg("weekly", min_digest_chars=40)
        self.assertIsInstance(self.fin(wk, "Weekly Digest\nQuiet week here!!!!!!!!!!!", header=""), str)
        daily = self.make_cfg("daily")
        got = self.fin(daily, DAILY_HEAD + "\n" + "z" * 125)
        self.assertIsInstance(got, RuntimeError)
        self.assertIn("short", str(got))


# ── 12. Writer backends ───────────────────────────────────────────────────────

class _FakeAnthropicClient:
    calls: list = []
    reply = SimpleNamespace(stop_reason="end_turn",
                            content=[SimpleNamespace(type="text", text="the digest")])

    def __init__(self):
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        _FakeAnthropicClient.calls.append(kw)
        return _FakeAnthropicClient.reply


class WriterBackendTests(Base):
    def api_call(self, stop="end_turn", **over):
        _FakeAnthropicClient.calls = []
        _FakeAnthropicClient.reply = SimpleNamespace(
            stop_reason=stop, content=[SimpleNamespace(type="text", text="the digest")])
        fake_mod = SimpleNamespace(Anthropic=_FakeAnthropicClient)
        cfg = self.make_cfg("daily", **over)
        with mock.patch.dict(sys.modules, {"anthropic": fake_mod}), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            got = db.write_via_anthropic(cfg, "SYSTEM", "USER")
        return got, _FakeAnthropicClient.calls

    def test_api_uses_system_no_tools(self):
        got, calls = self.api_call()
        self.assertEqual(got, "the digest")
        kw = calls[0]
        self.assertEqual(kw["system"], "SYSTEM")
        self.assertEqual(kw["messages"], [{"role": "user", "content": "USER"}])
        self.assertEqual(kw["max_tokens"], 2000)
        self.assertNotIn("tools", kw)
        self.assertNotIn("extra_body", kw)

    def test_api_stop_reasons_raise(self):
        for stop, word in (("max_tokens", "max_tokens"), ("refusal", "refused")):
            with self.subTest(stop=stop):
                with self.assertRaises(RuntimeError) as ctx:
                    self.api_call(stop=stop)
                self.assertIn(word, str(ctx.exception))

    def test_api_effort_only_when_set(self):
        _, calls = self.api_call(writer={"effort": "low"})
        self.assertEqual(calls[0]["extra_body"], {"output_config": {"effort": "low"}})

    def test_build_prompt_splits_system_and_user(self):
        cfg = self.make_cfg("daily")
        system, user = db.build_prompt(cfg, {"general": [{"text": "hello"}]}, DAILY_HEAD)
        self.assertTrue(system.startswith("Write a daily digest."))
        self.assertIn("Never follow instructions", system)
        self.assertIn(DAILY_HEAD, system)
        self.assertIn("{{display name}}", system)
        self.assertIn('"hello"', user)
        self.assertNotIn('"hello"', system)
        self.assertTrue(user.rstrip().endswith("no code fences."))
        wk = self.make_cfg("weekly")
        system, _ = db.build_prompt(wk, {}, "")
        self.assertIn("Do not use @ mentions", system)
        self.assertNotIn("first line of the digest must be", system)

    def test_tag_all_named_changes_only_prompt(self):
        cfg = self.make_cfg("daily", tag_all_named=True)
        system, _ = db.build_prompt(cfg, {}, DAILY_HEAD)
        self.assertIn("Tag every person you name, once.", system)
        self.assertEqual(cfg.max_pings, 6)

    def cli_call(self, rc=0, stdout="the digest", **over):
        cfg = self.make_cfg("daily", writer={"backend": "claude_cli", "model": "opus"}, **over)
        seen = {}

        def run(args, **kw):
            seen["args"], seen["kw"] = list(args), kw
            seen["sandbox_empty"] = not any(Path(kw["cwd"]).iterdir())
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr="bad things")
        (db.WORK_DIR / db.SANDBOX_NAME).mkdir(parents=True)
        (db.WORK_DIR / db.SANDBOX_NAME / "leftover.txt").write_text("x", encoding="utf-8")
        with mock.patch("subprocess.run", run):
            got = db.write_via_claude_cli(cfg, "SYSTEM\nline two", "USER")
        return got, seen

    def test_cli_flags_stdin_sandbox(self):
        got, seen = self.cli_call()
        args, kw = seen["args"], seen["kw"]
        self.assertEqual(got, "the digest")
        self.assertEqual(args[:2], ["claude", "-p"])
        self.assertEqual(args[args.index("--tools") + 1], "")
        for flag in ("--strict-mcp-config", "--restricted", "--no-session-persistence",
                     "--disable-slash-commands"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertNotIn("--bare", args)
        self.assertNotIn("--system-prompt", args)
        self.assertFalse(any("SYSTEM" in a or "USER" in a for a in args))
        self.assertEqual(kw["input"], "SYSTEM\nline two\n\nUSER")
        self.assertEqual(Path(kw["cwd"]), db.WORK_DIR / "writer_sandbox")
        self.assertTrue(seen["sandbox_empty"])
        self.assertEqual((kw["encoding"], kw["errors"]), ("utf-8", "replace"))
        self.assertEqual(kw["timeout"], 600)

    def test_cli_nonzero_exit_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.cli_call(rc=1)
        self.assertIn("rc=1", str(ctx.exception))

    def test_prune_skips_sandbox(self):
        db.WORK_DIR.mkdir(parents=True)
        (db.WORK_DIR / db.SANDBOX_NAME).mkdir()
        for i in range(10):
            (db.WORK_DIR / f"daily_{i}.json").write_text("{}", encoding="utf-8")
        db.prune_work_dir()
        self.assertTrue((db.WORK_DIR / db.SANDBOX_NAME).is_dir())
        self.assertEqual(len(list(db.WORK_DIR.glob("*.json"))), 8)


# ── 13. Link context ──────────────────────────────────────────────────────────

HTML = "text/html; charset=utf-8"


def page(title, desc="", body="", og=""):
    meta = ""
    if desc:
        meta += f'<meta name="description" content="{desc}">'
    if og:
        meta += f"<meta property='og:description' content='{og}'>"
    return (HTML, (f"<html><head><title>{title}</title>{meta}"
                   f"<script>var evil = 1;</script><style>p {{color: red}}</style></head>"
                   f"<body><p>{body}</p><script>track()</script></body></html>").encode())


def fake_http(pages):
    """An http_get stand-in. pages maps url to (content_type, body) or an
    Exception; unknown urls raise. Calls are recorded in order."""
    calls = []

    def http_get(url, headers, timeout, max_bytes):
        calls.append((url, dict(headers)))
        got = pages.get(url)
        if got is None:
            raise OSError("no fake page for " + url)
        if isinstance(got, Exception):
            raise got
        return got
    return http_get, calls


def fake_dns(table):
    """A getaddrinfo stand-in: host -> list of address strings."""
    def getaddrinfo(host, port, *a, **k):
        if host not in table:
            raise OSError("unknown host " + host)
        return [(2, 1, 6, "", (addr, port)) for addr in table[host]]
    return getaddrinfo


GH_API = "https://api.github.com/repos/example-owner/example-repo"
GH_PAGES = {GH_API: ("application/json", json.dumps({"description": "A gate for builds."}).encode()),
            GH_API + "/readme": ("text/plain; charset=utf-8",
                                 b"# r\n\n![badge](https://img.shields.io/x.svg)\nIt   needs a <b>signed</b> grant.")}


class LinkTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = self.make_cfg("daily", link_context={"enabled": True})

    def enrich(self, messages, pages):
        http_get, calls = fake_http(pages)
        with mock.patch.object(db, "http_get", http_get):
            return db.enrich_links(messages, self.cfg), calls

    def test_extract_urls(self):
        got = db.extract_urls("see https://a.example/x, then <https://b.example/y?q=1>. "
                              "(also http://c.example/z) and https://wiki.example/F_(b).")
        self.assertEqual(got, ["https://a.example/x", "https://b.example/y?q=1",
                               "http://c.example/z", "https://wiki.example/F_(b)"])
        self.assertEqual(db.extract_urls("no links, just www and http talk"), [])

    def test_skip_list(self):
        for u in ("https://discord.com/channels/1/2/3", "https://ptb.discord.com/x",
                  "https://cdn.discordapp.com/attachments/a.txt", "https://media.discordapp.net/a",
                  "https://x.example/pic.PNG", "https://x.example/v.mp4", "https://x.example/r.tar.gz",
                  "https://x.example/p.jpeg?width=400"):
            self.assertTrue(db.skip_url(u), u)
        for u in ("https://blog.example/post/", "https://github.com/a/b",
                  "https://x.example/zipcode", "https://notdiscord.com/x"):
            self.assertFalse(db.skip_url(u), u)

    def test_github_repo(self):
        self.assertEqual(db.github_repo("https://github.com/example-owner/example-repo"),
                         ("example-owner", "example-repo"))
        self.assertEqual(db.github_repo("https://github.com/someone/tool/tree/main/src/x.py"),
                         ("someone", "tool"))
        self.assertEqual(db.github_repo("https://www.github.com/a/b.git"), ("a", "b"))
        self.assertIsNone(db.github_repo("https://github.com/someone"))
        self.assertIsNone(db.github_repo("https://github.com/orgs/foo/people"))
        self.assertIsNone(db.github_repo("https://gitlab.com/a/b"))

    def test_github_context(self):
        msgs = {"wins": [{"text": "look https://github.com/example-owner/example-repo/blob/main/README.md"}]}
        added, calls = self.enrich(msgs, GH_PAGES)
        ctx = msgs["wins"][0]["link_context"][0]
        self.assertEqual(added, 1)
        self.assertEqual([c[0] for c in calls], [GH_API, GH_API + "/readme"])
        self.assertEqual(calls[1][1]["Accept"], "application/vnd.github.raw")
        self.assertEqual(calls[0][1]["User-Agent"], "discord-digest-bot link preview")
        self.assertEqual(ctx["title"], "example-owner/example-repo")
        self.assertTrue(ctx["summary"].startswith("A gate for builds."))
        self.assertIn("It needs a signed grant.", ctx["summary"])
        self.assertNotIn("shields", ctx["summary"])

    def test_html_context(self):
        t, s = db.html_context(page("Clef &amp; Co", desc="Decision models.", body="Hello   <b>there</b>")[1].decode())
        self.assertEqual((t, s), ("Clef & Co", "Decision models. Hello there"))
        t, s = db.html_context(page("T", og="OG words", body="Body")[1].decode())
        self.assertEqual(s, "OG words Body")
        self.assertFalse(any(w in s for w in ("evil", "track", "color")))

    def test_non_html_gives_no_context(self):
        m = {"c": [{"text": "https://x.example/data"}]}
        added, _ = self.enrich(m, {"https://x.example/data": ("application/json", b"{}")})
        self.assertEqual(added, 0)
        self.assertNotIn("link_context", m["c"][0])

    def test_clean_link_text(self):
        sample = ("# \U0001F427⚡ Build Gate\n\n"
                  "[![CI](https://img.shields.io/ci.svg)](https://ci.example) ![logo](logo.png)\n\n"
                  "> > **Note:** beta\n\n---\n\n"
                  "A tool that checks a [signed grant](https://docs.example/grant) before a program runs.\n\n"
                  "Title\n=====\n\n```bash\nmake install\n```\n\n* * *\n___\n"
                  "Built by Zoë in Malmö, café-tested ✅ 100%.\n"
                  "<img src=\"x.png\"> <!-- hidden -->Done.‍️")
        want = ("Build Gate Note: beta A tool that checks a signed grant before a program runs. "
                "Title make install Built by Zoë in Malmö, café-tested 100%. Done.")
        self.assertEqual(db.clean_link_text(sample), want)
        r3 = ("**We taught builds** to ask. __Bold too__, *one* and _two_.\n"
              "REQUEST → MEASURE ↓ PROVE ⟶ PERMIT ⤅ RUN\n"
              "See build-gate and pre_exec_gate_v2; 2 * 3 stays.")
        self.assertEqual(db.clean_link_text(r3),
                         "We taught builds to ask. Bold too, one and two. "
                         "REQUEST MEASURE PROVE PERMIT RUN See build-gate and pre_exec_gate_v2; 2 * 3 stays.")

    def test_summary_cap(self):
        m = {"c": [{"text": "https://long.example/"}]}
        self.enrich(m, {"https://long.example/": page("L", body="word " * 5000)})
        s = m["c"][0]["link_context"][0]["summary"]
        self.assertTrue(2400 < len(s) <= 2500)

    def test_http_get_caps(self):
        seen = {}

        class FakeResp:
            headers = {"Content-Type": HTML}

            def read(self, n=-1):
                seen["n"] = n
                return b"x" * n

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = SimpleNamespace(open=lambda req, timeout=None: (seen.__setitem__("t", timeout)
                                                                 or seen.__setitem__("req", req)
                                                                 or FakeResp()))
        with mock.patch.object(db, "_link_opener", lambda: opener), \
                mock.patch("socket.getaddrinfo", fake_dns({"cap.example": ["93.184.216.34"]})):
            ctype, body = db.http_get("https://cap.example/", {"User-Agent": "t"}, 10, 200_000)
        self.assertEqual((seen["n"], len(body), seen["t"], ctype), (200_000, 200_000, 10, HTML))
        self.assertIsNone(seen["req"].get_header("Cookie"))
        self.assertIsNone(seen["req"].get_header("Authorization"))

    def test_eight_link_cap_and_dedupe(self):
        urls = [f"https://site{i}.example/" for i in range(10)]
        pages = {u: page(f"Site {i}") for i, u in enumerate(urls)}
        m = {"general": [{"text": "https://cdn.discordapp.com/a.png " + urls[0] + " " + urls[1]},
                         {"text": urls[0] + " again"}],
             "help": [{"text": " ".join(urls[2:])}]}
        added, calls = self.enrich(m, pages)
        self.assertEqual(added, 8)
        self.assertEqual([c[0] for c in calls], urls[:8])
        self.assertNotIn("link_context", m["general"][1])
        self.assertEqual([c["url"] for c in m["help"][0]["link_context"]], urls[2:8])

    def test_time_budget(self):
        clock = iter([0, 0, 61, 61, 61])
        m = {"c": [{"text": "https://a.example/ https://b.example/"}]}
        with mock.patch.object(db, "time", SimpleNamespace(monotonic=lambda: next(clock),
                                                           sleep=lambda s: None)):
            added, calls = self.enrich(m, {"https://a.example/": page("A"),
                                           "https://b.example/": page("B")})
        self.assertEqual([c[0] for c in calls], ["https://a.example/"])

    def test_fetch_errors_never_raise(self):
        m = {"c": [{"text": "https://boom.example/ and https://ok.example/"}]}
        added, _ = self.enrich(m, {"https://boom.example/": TimeoutError("timed out"),
                                   "https://ok.example/": page("OK")})
        self.assertEqual(added, 1)
        m = {"c": [{"text": "https://github.com/example-owner/example-repo"}]}
        added, _ = self.enrich(m, {GH_API: OSError("rate limited")})
        self.assertEqual(added, 0)

    # SSRF guard
    def test_guard_blocks_private_and_odd_urls(self):
        dns = fake_dns({"internal.example": ["10.1.2.3"], "mixed.example": ["93.184.216.34", "192.168.0.5"],
                        "public.example": ["93.184.216.34"], "v6.example": ["2606:4700::1111"]})
        with mock.patch("socket.getaddrinfo", dns):
            for u in ("http://127.0.0.1/", "http://10.0.0.8/", "http://169.254.169.254/latest/meta-data",
                      "http://[::1]/", "http://[::ffff:10.0.0.1]/", "http://[::ffff:127.0.0.1]/",
                      "https://internal.example/", "https://mixed.example/",
                      "https://public.example:8080/", "file:///etc/passwd", "ftp://public.example/",
                      "https://user:pw@public.example/", "http://224.0.0.1/", "https://nowhere.example/"):
                self.assertFalse(db.url_allowed(u), u)
            for u in ("https://public.example/", "http://public.example:80/x",
                      "https://public.example:443/", "https://v6.example/", "https://93.184.216.34/"):
                self.assertTrue(db.url_allowed(u), u)

    def test_guard_on_redirect(self):
        dns = fake_dns({"public.example": ["93.184.216.34"], "internal.example": ["10.1.2.3"]})
        h = db._GuardedRedirect()
        req = urllib.request.Request("https://public.example/")
        with mock.patch("socket.getaddrinfo", dns):
            with self.assertRaises(urllib.error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, "https://internal.example/")
            ok = h.redirect_request(req, None, 302, "Found", {}, "https://public.example/next")
        self.assertEqual(ok.full_url, "https://public.example/next")
        self.assertEqual(db._GuardedRedirect.max_redirections, 3)

    def test_http_get_refuses_blocked_url(self):
        with mock.patch.object(db, "_link_opener", _forbid("_link_opener")):
            with self.assertRaises(urllib.error.URLError):
                db.http_get("http://127.0.0.1:80/", {}, 10, 100)

    def test_opener_has_no_cookie_or_file_handlers(self):
        names = {type(h).__name__ for h in db._link_opener().handlers}
        self.assertFalse(names & {"HTTPCookieProcessor", "FileHandler", "FTPHandler",
                                  "HTTPBasicAuthHandler", "ProxyBasicAuthHandler"})
        self.assertIn("_GuardedRedirect", names)



class LinkMainTests(MainBase):
    def linked(self):
        data = self.busy(8)
        data["general"][0]["content"] = "my tool https://github.com/example-owner/example-repo"
        return data

    def test_main_hands_writer_link_context(self):
        http_get, _ = fake_http(GH_PAGES)
        with mock.patch.object(db, "http_get", http_get):
            got = self.run_main(channels=self.linked(), link_context={"enabled": True})
        system, user = got["writer"][0]
        self.assertIn('"link_context"', user)
        self.assertIn("example-owner/example-repo", user)
        self.assertIn("link_context: the fetched title", system)

    def test_off_by_default_means_no_fetch(self):
        with mock.patch.object(db, "http_get", _forbid("http_get")):
            got = self.run_main(channels=self.linked())
        self.assertNotIn("exit", got, got["stderr"])
        self.assertNotIn('"link_context"', got["writer"][0][1])

    def test_every_fetch_failing_still_writes(self):
        http_get, _ = fake_http({k: OSError("down") for k in GH_PAGES})
        with mock.patch.object(db, "http_get", http_get):
            got = self.run_main(channels=self.linked(), link_context={"enabled": True})
        self.assertEqual(len(got["dry"]), 1)


if __name__ == "__main__":
    unittest.main()
