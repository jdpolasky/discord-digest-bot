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
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    # Period
    period: str
    digest_channel: str
    armed: bool
    window_days: float
    anchored: bool
    anchor_hour: int
    min_messages: int
    resolve_mentions: bool
    channel_name: str
    # Limits
    max_pings: int
    message_char_limit: int
    max_messages: int
    min_digest_chars: int
    # Writer
    writer_backend: str
    writer_model: str
    claude_cli_path: str
    writer_max_tokens: int
    digest_instructions: str


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
    periods = raw.get("periods", {})

    if period not in periods:
        raise RuntimeError(
            f"Period '{period}' is not defined in {config_path}. "
            f"Defined periods: {sorted(periods)}"
        )
    p = periods[period]

    return Config(
        guild_id=str(discord["guild_id"]),
        read_channels=list(discord["read_channels"]),
        token_file=discord.get("token_file"),
        period=period,
        digest_channel=str(p["digest_channel_id"]),
        armed=bool(p.get("armed", False)),
        window_days=float(p.get("window_days", 7)),
        anchored=bool(p.get("anchored", False)),
        anchor_hour=int(p.get("anchor_hour", 23)),
        min_messages=int(p.get("min_messages", 0)),
        resolve_mentions=bool(p.get("resolve_mentions", False)),
        channel_name=str(p.get("channel_name", "#digest")),
        max_pings=int(limits.get("max_pings", 6)),
        message_char_limit=int(limits.get("message_char_limit", 2000)),
        max_messages=int(limits.get("max_messages", 2)),
        min_digest_chars=int(limits.get("min_digest_chars", 200)),
        writer_backend=str(writer.get("backend", "anthropic")),
        writer_model=str(writer.get("model", "")),
        claude_cli_path=str(writer.get("claude_cli_path", "claude")),
        writer_max_tokens=int(writer.get("max_tokens", 2000)),
        digest_instructions=str(p.get("digest_instructions", writer.get("digest_instructions", ""))),
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

def api(path: str, token: str, method: str = "GET", body: dict | None = None) -> dict | list:
    for attempt in range(4):
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
                retry = json.loads(e.read()).get("retry_after", 2)
                time.sleep(float(retry) + 0.5)
                continue
            raise
    raise RuntimeError(f"Rate-limited past retries on {path}")


def window_bounds(cfg: Config) -> tuple[datetime, datetime | None]:
    """(start, end) for the read window.

    Rolling (anchored: false): start = now - window_days, end = None (no upper
    bound). Anchored (anchored: true): boundaries snap to the local anchor_hour
    clock line, start = anchor_hour local yesterday, end = anchor_hour local
    today. Boundaries are timezone-aware and compare cleanly against the
    messages' UTC timestamps.
    """
    if cfg.anchored:
        now_local = datetime.now().astimezone()
        end = now_local.replace(hour=cfg.anchor_hour, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=cfg.window_days)
        return start, end
    return datetime.now(timezone.utc) - timedelta(days=cfg.window_days), None


def channel_map(cfg: Config, token: str) -> dict:
    """{channel_name.lower(): channel_id} for the guild's text channels. Used to
    resolve {{#channel-name}} tokens to real jump-links, deterministically."""
    chans = api(f"/guilds/{cfg.guild_id}/channels", token)
    return {c["name"].lower(): c["id"] for c in chans if c.get("type") == 0}


def read_window(cfg: Config, token: str) -> dict:
    start, end = window_bounds(cfg)
    chans = api(f"/guilds/{cfg.guild_id}/channels", token)
    by_name = {c["name"]: c["id"] for c in chans if c.get("type") == 0}
    window: dict = {}
    for name in cfg.read_channels:
        cid = by_name.get(name)
        if not cid:
            continue
        msgs = api(f"/channels/{cid}/messages?limit=100", token)
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


# ── Mention resolution ────────────────────────────────────────────────────────

def resolve_mentions(text: str, authors: dict, channels: dict | None,
                     max_pings: int) -> str:
    """Turn the writer's {{...}} tokens into safe Discord pings and channel jump-
    links, and scrub any stray raw pings/links. The cheap writer is not trusted
    with IDs, so it names people and channels in {{double braces}} and we resolve
    them here against the real author and channel maps.

    - {{name}}: matched case-insensitively to an author display name. A hit
      becomes <@id>; a miss becomes the plain inner text (no ping).
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
    for author_id, name in authors.items():
        key = name.strip().lower()
        if key and key not in name_to_id:  # first wins on collision
            name_to_id[key] = author_id
    chan_ids = set(channels.values())

    def sub_brace(m: re.Match) -> str:
        inner = m.group(1)
        stripped = inner.strip()
        if stripped.startswith("#"):
            name = stripped[1:].strip().lower()
            cid = channels.get(name)
            return f"<#{cid}>" if cid else f"#{name}"
        key = stripped.lower()
        aid = name_to_id.get(key)
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

    if cfg.resolve_mentions:
        # Turn {{name}} tokens into safe pings and scrub stray IDs before any
        # length/split validation runs on the final text.
        digest = resolve_mentions(digest, authors or {}, channels or {}, cfg.max_pings)

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

def post(cfg: Config, digest: str, token: str) -> int:
    parts = [p.strip() for p in digest.split(SPLIT_MARK) if p.strip()]
    for i, p in enumerate(parts):
        try:
            api(f"/channels/{cfg.digest_channel}/messages", token, "POST", {"content": p})
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
            parts = post(cfg, digest, token)
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
