#!/usr/bin/env python3
"""digest_bot.py: a self-hostable Discord digest bot.

One engine, two periods, selected by --period (default weekly):
  weekly  reads a rolling window (default 7 days) and posts to a digest channel.
  daily   reads an anchored window (yesterday's anchor hour -> today's anchor
          hour) and posts to a digest channel, skipping genuinely quiet days.

Pipeline (both periods):
  1. Read the period's window of messages from the configured read channels
     via the Discord REST API.
  2. Hand the raw JSON to an LLM writer (Anthropic API by default, or the
     Claude Code CLI as an alternative) to write the digest per the format
     instructions in config.
  3. Validate, then either POST to the digest channel (armed: true) or write a
     dry-run file under ./out (armed: false).

Failure rule: a broken or invalid digest never posts. The run logs the failure
under ./logs and exits nonzero.

Everything server-specific lives in a JSON config file (see config.example.json).
Nothing about any particular Discord server is baked into this file.
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
WORK_DIR = SCRIPT_DIR / "digest_work"
LOGS_DIR = SCRIPT_DIR / "logs"
OUT_DIR = SCRIPT_DIR / "out"

SPLIT_MARK = "=====SPLIT====="


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    """The fully resolved run configuration for one period.

    Every field here comes from the JSON config file plus the chosen --period;
    nothing is hardcoded to a particular server.
    """
    # Discord
    guild_id: str
    read_channels: list
    token_file: str | None
    read_threads: bool
    # Period
    period: str
    digest_channel: str
    armed: bool
    window_days: float
    anchored: bool
    anchor_hour: int
    anchor_grace_minutes: int
    min_messages: int
    resolve_mentions: bool
    channel_name: str
    header: str
    carryover: bool
    max_carry_days: int
    max_quiet_runs: int
    tag_all_named: bool
    # Limits
    max_pings: int
    message_char_limit: int
    max_messages: int
    min_digest_chars: int  # the period's value, else limits.min_digest_chars
    max_fetch_per_channel: int
    target_chars: int
    trim_max_over: int
    min_part_chars: int
    # Writer
    writer_backend: str
    writer_model: str
    claude_cli_path: str
    writer_max_tokens: int
    writer_timeout_seconds: int
    writer_effort: str | None
    digest_instructions: str
    # Link context (opt-in)
    link_enabled: bool
    link_max_links: int
    link_timeout_seconds: float
    link_max_bytes: int
    link_summary_chars: int


def load_config(config_path: Path, period: str) -> Config:
    """Parse the JSON config file and flatten it for the chosen period.

    Raises a clear error if the file is missing, is not valid JSON, or does not
    define the requested period.
    """
    if not config_path.exists():
        raise RuntimeError(
            f"Config file not found: {config_path}. Copy config.example.json to "
            f"config.json and fill in your values."
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Config file {config_path} is not valid JSON: {e}")

    discord = raw.get("discord", {})
    writer = raw.get("writer", {})
    limits = raw.get("limits", {})
    links = raw.get("link_context", {})
    periods = raw.get("periods", {})

    if period not in periods:
        raise RuntimeError(
            f"Period '{period}' is not defined in {config_path}. "
            f"Defined periods: {sorted(periods)}"
        )
    p = periods[period]

    # Every key added after the first release has a default here, so an older
    # config.json still loads unchanged.
    anchored = bool(p.get("anchored", False))
    resolve = bool(p.get("resolve_mentions", False))
    header = p.get("header")
    if header is None:
        header = "Daily Digest, {date}" if resolve else ""
    effort = writer.get("effort")

    return Config(
        guild_id=str(discord["guild_id"]),
        read_channels=list(discord["read_channels"]),
        token_file=discord.get("token_file"),
        read_threads=bool(discord.get("read_threads", True)),
        period=period,
        digest_channel=str(p["digest_channel_id"]),
        armed=bool(p.get("armed", False)),
        window_days=float(p.get("window_days", 7)),
        anchored=anchored,
        anchor_hour=int(p.get("anchor_hour", 23)),
        anchor_grace_minutes=int(p.get("anchor_grace_minutes", 30)),
        min_messages=int(p.get("min_messages", 0)),
        resolve_mentions=resolve,
        channel_name=str(p.get("channel_name", "#digest")),
        header=str(header),
        carryover=bool(p.get("carryover", anchored)),
        max_carry_days=int(p.get("max_carry_days", 7)),
        max_quiet_runs=int(p.get("max_quiet_runs", 3)),
        tag_all_named=bool(p.get("tag_all_named", False)),
        max_pings=int(limits.get("max_pings", 6)),
        message_char_limit=int(limits.get("message_char_limit", 2000)),
        max_messages=int(limits.get("max_messages", 2)),
        min_digest_chars=int(p.get("min_digest_chars", limits.get("min_digest_chars", 200))),
        max_fetch_per_channel=int(limits.get("max_fetch_per_channel", 1000)),
        target_chars=int(limits.get("target_chars", 1600)),
        trim_max_over=int(limits.get("trim_max_over", 200)),
        min_part_chars=int(limits.get("min_part_chars", 40)),
        writer_backend=str(writer.get("backend", "anthropic")),
        writer_model=str(writer.get("model", "")),
        claude_cli_path=str(writer.get("claude_cli_path", "claude")),
        writer_max_tokens=int(writer.get("max_tokens", 16000)),
        writer_timeout_seconds=int(writer.get("timeout_seconds", 600)),
        writer_effort=str(effort) if effort else None,
        digest_instructions=str(p.get("digest_instructions", writer.get("digest_instructions", ""))),
        link_enabled=bool(links.get("enabled", False)),
        link_max_links=int(links.get("max_links", 8)),
        link_timeout_seconds=float(links.get("timeout_seconds", 10)),
        link_max_bytes=int(links.get("max_bytes", 200_000)),
        link_summary_chars=int(links.get("summary_chars", 2500)),
    )


# ── Token ─────────────────────────────────────────────────────────────────────

def get_token(cfg: Config) -> str:
    """Read the Discord bot token: env var first, then the configured token file.

    Order: the DISCORD_BOT_TOKEN environment variable, then the file named by
    discord.token_file in config (blank/comment lines ignored). The token never
    lives in the repo; keep it in an env var or an ignored file.
    """
    if tok := os.environ.get("DISCORD_BOT_TOKEN"):
        return tok
    if cfg.token_file:
        token_file = Path(os.path.expanduser(cfg.token_file))
        if not token_file.is_absolute():
            token_file = (SCRIPT_DIR / token_file).resolve()
        if token_file.exists():
            for line in token_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    raise RuntimeError(
        "Discord token not found. Set the DISCORD_BOT_TOKEN environment variable "
        "or place the token in the file named by discord.token_file in config."
    )


# ── Discord REST ──────────────────────────────────────────────────────────────

RETRY_WAITS = (2, 5)    # pauses between the 3 attempts at a GET
RATE_WAIT_MAX = 60      # longest single rate-limit wait, seconds
RETRY_TOTAL_MAX = 120   # most total waiting in one api() call, seconds
RATE_ATTEMPTS = 4       # rate-limit attempts per call


def api(path: str, token: str, method: str = "GET", body: dict | None = None) -> dict | list:
    """One Discord REST call.

    Rate limits (429) wait as Discord asks, at most RATE_WAIT_MAX per wait and
    RATE_ATTEMPTS attempts. A GET that hits a server error (500-599), a timeout
    or a connection error is tried up to 3 times with RETRY_WAITS pauses.
    Non-GET calls never retry those: a POST that timed out may have landed, and
    a retry would post it twice. Total waiting per call is capped at
    RETRY_TOTAL_MAX; past any limit the error is raised.
    """
    waited = 0.0
    rate_hits = transient = 0
    while True:
        req = urllib.request.Request(
            f"https://discord.com/api/v10{path}",
            method=method,
            data=json.dumps(body).encode() if body else None,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "DiscordBot (discord-digest-bot, 1.0)",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                rate_hits += 1
                try:
                    retry = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    retry = 2.0
                wait = min(retry + 0.5, RATE_WAIT_MAX)
                if rate_hits >= RATE_ATTEMPTS or waited + wait > RETRY_TOTAL_MAX:
                    raise RuntimeError(f"Rate-limited past retries on {path}")
                time.sleep(wait)
                waited += wait
                continue
            if not (500 <= e.code <= 599 and method == "GET"):
                raise
            err: Exception = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if method != "GET":
                raise
            err = e
        transient += 1
        if transient > len(RETRY_WAITS):
            raise err
        wait = RETRY_WAITS[transient - 1]
        if waited + wait > RETRY_TOTAL_MAX:
            raise err
        print(f"WARNING: {path} failed ({err}); retrying in {wait}s.", file=sys.stderr)
        time.sleep(wait)
        waited += wait


# ── Window ────────────────────────────────────────────────────────────────────

def _localize(naive: datetime, tz: tzinfo | None) -> datetime:
    """Attach the local zone to a naive local datetime. Each call gets its own
    UTC offset, so two edges on either side of a DST change are both right.
    The result carries a fixed offset either way, so arithmetic on it is real
    elapsed time."""
    if tz is None:
        return naive.astimezone()
    aware = naive.replace(tzinfo=tz)
    return aware.astimezone(timezone(aware.utcoffset()))


def _local_naive(aware: datetime, tz: tzinfo | None) -> datetime:
    return (aware.astimezone(tz) if tz is not None else aware.astimezone()).replace(tzinfo=None)


def window_bounds(cfg: Config, now: datetime | None = None,
                  tz: tzinfo | None = None) -> tuple[datetime, datetime | None]:
    """(start, end) for the read window, both timezone-aware.

    Rolling (anchored: false): start = now - window_days, end = None.

    Anchored (anchored: true): end is the most recent anchor_hour clock line at
    or before now, unless the next one is at most anchor_grace_minutes away, in
    which case end is that next one. So a run started a little early still
    covers the current day, and a late or wake-up run covers the day it missed.
    start = end - window_days. Both edges are built as naive local times and
    localized separately, so consecutive windows meet exactly across DST
    changes.

    now is a naive local datetime (tests); tz is the local zone (tests). When
    tz is None the machine's local zone is used.
    """
    if now is None:
        now = datetime.now(tz).replace(tzinfo=None) if tz is not None else datetime.now()
    if not cfg.anchored:
        return _localize(now, tz) - timedelta(days=cfg.window_days), None
    a = now.replace(hour=cfg.anchor_hour, minute=0, second=0, microsecond=0)
    last, nxt = (a, a + timedelta(days=1)) if a <= now else (a - timedelta(days=1), a)
    grace = timedelta(minutes=cfg.anchor_grace_minutes)
    end_naive = nxt if nxt - now <= grace else last
    start_naive = end_naive - timedelta(days=cfg.window_days)
    return _localize(start_naive, tz), _localize(end_naive, tz)


def window_day(cfg: Config, bounds: tuple[datetime, datetime | None],
               tz: tzinfo | None = None) -> date:
    """The local day a run is about. Rolling: today. Anchored: the local date of
    the window's midpoint, so a 07:00 wake-up run is about the day it missed."""
    start, end = bounds
    if not cfg.anchored or end is None:
        return (datetime.now(tz) if tz is not None else datetime.now()).date()
    return _local_naive(start + (end - start) / 2, tz).date()


def _short_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def render_header(cfg: Config, bounds: tuple[datetime, datetime | None], day: date,
                  tz: tzinfo | None = None) -> str:
    """The digest's exact first line, from the period's header template.

    {date} becomes the run's day, like "Tue Sep 15" (no leading zero).
    {range} becomes the window's days, like "Oct 8-14" or "Sep 30-Oct 6".
    An empty template means no engine-set header.
    """
    if not cfg.header:
        return ""
    start, end = bounds
    if cfg.anchored and end is not None:
        last = _local_naive(end - timedelta(hours=12), tz).date()
    else:
        last = day
    first = last - timedelta(days=max(1, math.ceil(cfg.window_days - 1e-9)) - 1)
    if first == last:
        rng = _short_day(last)
    elif (first.year, first.month) == (last.year, last.month):
        rng = f"{_short_day(first)}-{last.day}"
    else:
        rng = f"{_short_day(first)}-{_short_day(last)}"
    text = cfg.header.replace("{date}", f"{day:%a %b} {day.day}").replace("{range}", rng)
    return scrub(text, {}).strip()


def channel_map(cfg: Config, token: str) -> dict:
    """{channel_name.lower(): channel_id} for the guild's text channels. Used to
    resolve {{#channel-name}} tokens to real jump-links, deterministically."""
    chans = api(f"/guilds/{cfg.guild_id}/channels", token)
    return {c["name"].lower(): c["id"] for c in chans if c.get("type") == 0}


def fetch_channel_messages(cfg: Config, cid: str, token: str,
                           start: datetime) -> tuple[list, bool]:
    """Page backward through one channel's messages, newest first.

    Discord returns up to 100 messages per request, newest first. We walk
    backward with the before=<message_id> param, page after page, and stop as
    soon as a page reaches messages older than `start` (the window is fully
    covered) or the channel runs out of messages. If we hit
    cfg.max_fetch_per_channel before reaching the window start, we stop and
    report truncation so nothing is dropped silently.

    Returns (raw_messages, truncated).
    """
    collected: list = []
    before: str | None = None
    truncated = False
    while True:
        path = f"/channels/{cid}/messages?limit=100"
        if before:
            path += f"&before={before}"
        page = api(path, token)
        if not page:
            break
        collected.extend(page)
        # Newest-first: the last item in the page is its oldest message.
        oldest = page[-1]
        oldest_ts = datetime.fromisoformat(oldest["timestamp"])
        before = oldest["id"]
        if oldest_ts < start:
            # Paged past the start of the window; every older page is out of
            # window. The window is fully covered, so this is not truncation.
            break
        if len(page) < 100:
            # No more messages in the channel.
            break
        if len(collected) >= cfg.max_fetch_per_channel:
            truncated = True
            break
        time.sleep(0.3)
    return collected, truncated


def read_window(cfg: Config, token: str) -> dict:
    start, end = window_bounds(cfg)
    chans = api(f"/guilds/{cfg.guild_id}/channels", token)
    by_name = {c["name"]: c["id"] for c in chans if c.get("type") == 0}
    window: dict = {}
    for name in cfg.read_channels:
        cid = by_name.get(name)
        if not cid:
            continue
        msgs, truncated = fetch_channel_messages(cfg, cid, token, start)
        if truncated:
            truncation_log(cfg, name)
        keep = []
        for m in msgs:
            ts = datetime.fromisoformat(m["timestamp"])
            if m["author"].get("bot"):
                continue
            if end is None:
                # rolling: keep everything at/after start.
                if ts < start:
                    continue
            else:
                # anchored: keep start < ts <= end.
                if not (start < ts <= end):
                    continue
            keep.append({
                "author": m["author"].get("global_name") or m["author"]["username"],
                "author_id": m["author"]["id"],
                "when": m["timestamp"][:16],
                "text": m.get("content", ""),
                "reactions": sum(r.get("count", 0) for r in m.get("reactions", [])),
            })
        if keep:
            window[name] = list(reversed(keep))
        time.sleep(0.3)
    return window


# ── Mention resolution and ping containment ───────────────────────────────────

# Name-tag matching key: the writer may type a straight apostrophe where a
# display name has a curly one, or leave off an emoji. Fold quotes, drop emoji
# and invisible characters, keep accented letters.
_QUOTE_FOLD = {0x2018: "'", 0x2019: "'", 0x02BC: "'", 0x0060: "'", 0x00B4: "'",
               0x201C: '"', 0x201D: '"'}
_DROP_CATS = {"So", "Sk", "Cf", "Cs", "Co"}
_DROP_CHARS = {chr(0xFE0E), chr(0xFE0F), chr(0x200D)}


def name_key(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_QUOTE_FOLD)
    s = "".join(c for c in s if c not in _DROP_CHARS
                and unicodedata.category(c) not in _DROP_CATS)
    return re.sub(r"\s+", " ", s).strip().lower()


def scrub(text: str, authors: dict) -> str:
    """Ping containment, run on every digest whatever the period.

    - @everyone and @here get a zero-width space after the @ so they cannot fire.
    - Role pings <@&id> are removed.
    - <@!id> (nickname form) becomes <@id>.
    - Any <@id> whose id is not an author in this run's window is removed.
    - Em and en dashes are normalized. Doubled spaces collapse only mid-line,
      so markdown indentation at the start of a line survives.
    Idempotent: running it twice gives the same text.
    """
    text = re.sub(r"@(everyone|here)", "@" + chr(0x200B) + r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"<@&\d+>", "", text)
    text = re.sub(r"<@!(\d+)>", r"<@\1>", text)
    text = re.sub(r"<@(\d+)>", lambda m: m.group(0) if m.group(1) in authors else "", text)
    text = text.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
    text = re.sub(r"(?<=\S) {2,}", " ", text)
    text = re.sub(r" ,", ",", text)
    return text


def mention_users(text: str, authors: dict) -> list:
    """User ids pinged as <@id> in text that are real authors, in first-seen
    order, capped at Discord's limit of 100."""
    users: list = []
    for aid in re.findall(r"<@(\d+)>", text):
        if aid in authors and aid not in users:
            users.append(aid)
    return users[:100]


# Output scan: anything token-like fails the run. Prefixes need a realistic key
# body after them, so a member merely naming a key format does not fail a run.
SECRET_PATTERNS = [
    ("Discord bot token", r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}"),
    # Built from two pieces so the literal key prefix never sits in this source.
    ("Anthropic key", r"sk-" r"ant-[A-Za-z0-9_-]{16,}"),
    ("sk- key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("GitHub token", r"\bghp_[A-Za-z0-9]{20,}"),
    ("GitHub fine-grained token", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{30,}"),
    ("private key block", r"-----BEGIN"),
]


def secret_scan(text: str, secrets: list) -> None:
    """Raise if the digest holds one of the run's own secrets (the bot token,
    the Anthropic key) or anything secret-shaped. Empty secrets are skipped.
    The error names the pattern only, never the matched text."""
    for s in secrets:
        if s and s in text:
            raise RuntimeError("Secret scan: digest contains one of this run's credentials; not posting.")
    for name, pat in SECRET_PATTERNS:
        if re.search(pat, text):
            raise RuntimeError(f"Secret scan: digest matches a {name} pattern; not posting.")


def resolve_mentions(text: str, authors: dict, channels: dict | None,
                     max_pings: int) -> str:
    """Turn the writer's {{...}} tokens into safe Discord pings and channel jump-
    links, and scrub any stray raw pings/links. The cheap writer is not trusted
    with IDs, so it names people and channels in {{double braces}} and we resolve
    them here against the real author and channel maps.

    - {{name}}: matched to an author display name with name_key (case, curly
      quotes, emoji and invisible characters ignored). A hit becomes <@id>; a
      miss becomes the plain inner text (no ping). A name two members share is
      never guessed: it stays plain text.
    - {{#channel-name}}: matched case-insensitively to a real channel name. A hit
      becomes <#channel_id> (a clickable link); a miss becomes plain "#name".
    - Mention-once: the first <@id> for each id stays; later repeats of that id
      become the plain author name. Channels have no once-rule.
    - Ping cap: at most max_pings distinct users render as pings; once the cap is
      reached every further new user falls back to the plain author name, so a
      newcomer wave can never blast a wall of pings. Channels are not capped.
    - Safety strip: any remaining raw <@digits> not a real author, or <#digits>
      not a real channel, is deleted so a hallucinated id can never render.
    """
    channels = channels or {}
    name_to_id: dict = {}
    shared: set = set()
    for author_id, name in authors.items():
        key = name_key(name)
        if not key:
            continue
        if key in name_to_id and name_to_id[key] != author_id:
            shared.add(key)  # two members, one name: never guess which one
        name_to_id.setdefault(key, author_id)
    for key in shared:
        del name_to_id[key]  # a shared name renders as plain text, no ping
    chan_ids = set(channels.values())

    def sub_brace(m: re.Match) -> str:
        inner = m.group(1)
        stripped = inner.strip()
        if stripped.startswith("#"):
            name = stripped[1:].strip().lower()
            cid = channels.get(name)
            return f"<#{cid}>" if cid else f"#{name}"
        key = name_key(stripped)
        aid = name_to_id.get(key) if key else None
        return f"<@{aid}>" if aid else inner

    text = re.sub(r"\{\{(.*?)\}\}", sub_brace, text)

    seen: set = set()

    def sub_ping(m: re.Match) -> str:
        aid = m.group(1)
        if aid not in authors:
            return ""  # hallucinated / unknown id: drop the ping entirely
        if aid in seen:
            return authors[aid]  # mention-once: later repeats become plain name
        if len(seen) >= max_pings:
            return authors[aid]  # cap reached: new users fall back to plain name
        seen.add(aid)
        return f"<@{aid}>"

    text = re.sub(r"<@(\d+)>", sub_ping, text)
    # Channel safety strip: drop any raw <#id> that is not a real channel.
    text = re.sub(r"<#(\d+)>", lambda m: m.group(0) if m.group(1) in chan_ids else "", text)
    # Dash normalizer: keep the output plain-ASCII on dashes to avoid surprises.
    # (chr() builds the codepoints so no literal special dash sits in this source.)
    text = text.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
    text = re.sub(r"  +", " ", text)  # tidy doubled spaces from dropped tokens
    text = re.sub(r" ,", ",", text)   # collapse any accidental " ," into ","
    return text


# ── LLM writer ────────────────────────────────────────────────────────────────

def build_prompt(cfg: Config, messages: dict, date_label: str | None) -> str:
    """Assemble the writer prompt from the config instructions and the messages."""
    parts = [cfg.digest_instructions.strip()]
    parts.append(
        "Here are the messages to summarize, as JSON grouped by channel:\n"
        + json.dumps(messages, indent=1, ensure_ascii=False)
    )
    if cfg.resolve_mentions and date_label:
        parts.append(
            "Begin the digest with exactly this first line and do not alter it: "
            f"Daily Digest, {date_label}"
        )
    parts.append(
        "Output only the digest text itself, with no preamble, no explanation, "
        "and no code fences."
    )
    return "\n\n".join(parts)


def write_via_anthropic(cfg: Config, prompt: str) -> str:
    """Call the Anthropic Messages API and return the digest text.

    Requires the ANTHROPIC_API_KEY environment variable and the `anthropic`
    Python package. The model is set in config (writer.model).
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "The 'anthropic' package is not installed. Run: pip install anthropic "
            "(or switch writer.backend to 'claude_cli' in config)."
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export your Anthropic API key, or "
            "switch writer.backend to 'claude_cli' in config."
        )
    if not cfg.writer_model:
        raise RuntimeError("writer.model is not set in config.")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg.writer_model,
        max_tokens=cfg.writer_max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    chunks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Anthropic API returned an empty digest; not posting.")
    return text


def write_via_claude_cli(cfg: Config, prompt: str) -> str:
    """Shell out to the Claude Code CLI and return the digest text from stdout.

    Requires the Claude Code CLI on PATH (or writer.claude_cli_path set to its
    full path). The model is set in config (writer.model).
    """
    cmd = [cfg.claude_cli_path, "-p", prompt]
    if cfg.writer_model:
        cmd += ["--model", cfg.writer_model]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, cwd=str(SCRIPT_DIR),
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Claude CLI not found at '{cfg.claude_cli_path}'. Install the Claude "
            "Code CLI, set writer.claude_cli_path in config, or switch "
            "writer.backend to 'anthropic'."
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude CLI exited rc={result.returncode}; not posting. "
            f"stderr tail: {result.stderr[-500:]}"
        )
    text = result.stdout.strip()
    if not text:
        raise RuntimeError("Claude CLI returned an empty digest; not posting.")
    return text


def prepare_digest(cfg: Config, digest: str, authors: dict | None = None,
                   channels: dict | None = None) -> str:
    """The finishing steps before any split: strip, mention resolution (when
    resolve_mentions is on), then scrub. Its output length is the length the
    digest will really post at."""
    digest = digest.strip()
    authors = authors or {}
    if cfg.resolve_mentions:
        # scrub first so <@!id> is normalized before mention-once and the cap.
        digest = resolve_mentions(scrub(digest, authors), authors, channels or {}, cfg.max_pings)
    return scrub(digest, authors)


def write_digest(cfg: Config, messages: dict, authors: dict,
                 channels: dict, date_label: str | None) -> str:
    """Produce the validated digest string, or raise so the run fails safe."""
    prompt = build_prompt(cfg, messages, date_label)
    if cfg.writer_backend == "anthropic":
        digest = write_via_anthropic(cfg, prompt)
    elif cfg.writer_backend == "claude_cli":
        digest = write_via_claude_cli(cfg, prompt)
    else:
        raise RuntimeError(
            f"Unknown writer.backend '{cfg.writer_backend}'. "
            "Use 'anthropic' or 'claude_cli'."
        )

    # Strip any stray code fences the model may have wrapped the output in.
    digest = re.sub(r"^```[a-zA-Z]*\n", "", digest)
    digest = re.sub(r"\n```$", "", digest).strip()

    digest = prepare_digest(cfg, digest, authors, channels)

    if len(digest) < cfg.min_digest_chars:
        raise RuntimeError(
            f"Digest suspiciously short ({len(digest)} chars); not posting."
        )
    parts = [p.strip() for p in digest.split(SPLIT_MARK) if p.strip()]
    if len(parts) > cfg.max_messages:
        raise RuntimeError(
            f"Digest split into {len(parts)} messages; the cap is "
            f"{cfg.max_messages}. Not posting."
        )
    for p in parts:
        if len(p) > cfg.message_char_limit:
            raise RuntimeError(
                f"Digest part exceeds {cfg.message_char_limit} chars ({len(p)}); "
                "not posting."
            )
    return digest


# ── Post ──────────────────────────────────────────────────────────────────────

def post(cfg: Config, digest: str, token: str, authors: dict) -> int:
    """POST each part. allowed_mentions lists exactly the users a part may ping;
    with resolve_mentions off the list is empty, so nothing pings, not even a
    raw <@id>. @everyone, @here and roles never ping (parse is empty)."""
    parts = [p.strip() for p in digest.split(SPLIT_MARK) if p.strip()]
    for i, p in enumerate(parts):
        users = mention_users(p, authors) if cfg.resolve_mentions else []
        body = {"content": p, "allowed_mentions": {"parse": [], "users": users}}
        try:
            api(f"/channels/{cfg.digest_channel}/messages", token, "POST", body)
        except Exception as e:
            raise RuntimeError(
                f"Post failed on message {i + 1} of {len(parts)}; "
                f"{i} message(s) are already live in the channel. {e}"
            )
        time.sleep(1)
    return len(parts)


# ── Local logs and output ─────────────────────────────────────────────────────

def append_log(cfg: Config, line: str) -> None:
    """Append one line to the period's log file under ./logs, creating it with a
    header on first write. Shared by success_log and the quiet-day skip."""
    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"{cfg.period}_digest.log"
    if not log_path.exists():
        log_path.write_text(
            f"# {cfg.period} digest log (one line per run) - written by digest_bot.py\n"
            + line,
            encoding="utf-8",
        )
    else:
        text = log_path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        log_path.write_text(text + line, encoding="utf-8")


def success_log(cfg: Config, parts: int, chars: int) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_log(cfg, f"- {stamp} posted {parts} message(s), {chars} chars\n")


def quiet_skip_log(cfg: Config, total: int) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_log(cfg, f"- {stamp} skipped, quiet day ({total} msgs)\n")


def truncation_log(cfg: Config, channel: str) -> None:
    """Record that a channel hit the fetch cap before the pager reached the
    window start, so in-window messages were left unread. This is the loud line
    the pagination fix exists to produce; a busy channel never truncates in
    silence."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_log(
        cfg,
        f"- {stamp} TRUNCATED: channel '{channel}' hit the "
        f"max_fetch_per_channel cap of {cfg.max_fetch_per_channel} messages "
        f"before reaching the window start; older in-window messages were not "
        f"read. Raise limits.max_fetch_per_channel to read the full window.\n",
    )


def fail_log(cfg: Config, err: str) -> None:
    """Record a failure under ./logs. A failed run never posts anything."""
    LOGS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    log_path = LOGS_DIR / f"{cfg.period}_digest.log"
    line = f"- {stamp} FAILED, nothing posted: {err}\n"
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        log_path.write_text(text + line, encoding="utf-8")
    else:
        log_path.write_text(
            f"# {cfg.period} digest log (one line per run) - written by digest_bot.py\n"
            + line,
            encoding="utf-8",
        )


def dry_run_note(cfg: Config, digest: str) -> Path:
    """Write the would-be digest to ./out instead of posting (armed: false)."""
    OUT_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    note = OUT_DIR / f"{cfg.period}_dry_run_{today}.md"
    note.write_text(
        f"# {cfg.period} digest dry run {today}\n\n"
        f"Generated by digest_bot.py with armed: false. Nothing was posted. The "
        f"text below is exactly what would go to {cfg.channel_name}, split at the "
        f"marker '{SPLIT_MARK}'.\n\n---\n\n{digest}\n",
        encoding="utf-8",
    )
    return note


def prune_work_dir(keep: int = 8) -> None:
    """Keep the newest files in digest_work so the folder never grows unbounded."""
    if not WORK_DIR.exists():
        return
    files = sorted(WORK_DIR.glob("*.*"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink()


# ── Main ──────────────────────────────────────────────────────────────────────

def main(period: str, config_path: Path) -> None:
    cfg = load_config(config_path, period)
    WORK_DIR.mkdir(exist_ok=True)
    prune_work_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        token = get_token(cfg)
        messages = read_window(cfg, token)
        total = sum(len(v) for v in messages.values())
        if total < cfg.min_messages:
            # Quiet day. A normal outcome, not a failure: log the skip, post
            # nothing, exit clean.
            quiet_skip_log(cfg, total)
            print(f"{cfg.period} digest skipped: quiet day ({total} msgs).")
            return
        # Keep a copy of the raw JSON for debugging.
        src_json = WORK_DIR / f"{cfg.period}_{today}.json"
        src_json.write_text(
            json.dumps(messages, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        # Real author and channel maps for deterministic mention resolution.
        authors = {m["author_id"]: m["author"] for msgs in messages.values() for m in msgs}
        channels = channel_map(cfg, token) if cfg.resolve_mentions else {}
        # Deterministic first-line date (e.g. "Tue Sep 15", no leading zero);
        # the writer must never compute the weekday itself.
        now = datetime.now()
        date_label = f"{now:%a %b} {now.day}" if cfg.resolve_mentions else None
        digest = write_digest(cfg, messages, authors, channels, date_label)
        if cfg.armed:
            parts = post(cfg, digest, token, authors)
            success_log(cfg, parts, len(digest))
            print(f"Digest posted ({parts} message(s), {len(digest)} chars).")
        else:
            note = dry_run_note(cfg, digest)
            print(f"Dry run written to {note}")
    except Exception as e:
        fail_log(cfg, str(e))
        print(f"DIGEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Self-hostable Discord digest bot (weekly or daily)."
    )
    parser.add_argument("--period", default="weekly",
                        help="Which period block in config to run (default: weekly).")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"),
                        help="Path to the JSON config file (default: ./config.json).")
    args = parser.parse_args()
    main(args.period, Path(args.config))
