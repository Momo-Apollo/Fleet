#!/usr/bin/env python3
"""Bridge collab listener — persistent daemon that monitors the Bridge DM for
peer-initiated ::collab-task:: sentinels and drives the collab response loop."""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────

FLEET_PAIRING_CHANNEL = "C0BK59E8XLZ"
HEARTBEAT_INTERVAL    = 300   # 5 minutes
CLAUDE_BIN            = (
    os.environ.get("CLAUDE_BIN")
    or next((p for p in [
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
    ] if os.path.isfile(p)), None)
    or "claude"
)

def _load_bridge_cfg() -> dict:
    p = Path.home() / ".fleet" / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("bridge", {})
        except Exception:
            pass
    return {}

def _read_soul_identity() -> tuple[str, str]:
    """Read agent/human name from SOUL.md, fall back to bridge config, then empty."""
    cfg = _load_bridge_cfg()
    agent = cfg.get("self_name", "")
    human = cfg.get("self_human", "")
    soul = Path.home() / ".claude" / "SOUL.md"
    if soul.exists():
        for line in soul.read_text().splitlines():
            if line.startswith("**Name:**") and not agent:
                agent = line.split("**Name:**", 1)[1].strip()
            if line.startswith("**Human:**") and not human:
                human = line.split("**Human:**", 1)[1].split("@")[0].strip()
    return (agent or "Agent", human or "Human")

_bridge_cfg = _load_bridge_cfg()

BRIDGE_DM = _bridge_cfg.get("channel", "")
PEER_UID  = _bridge_cfg.get("peer_uid", "")
SELF_UID  = _bridge_cfg.get("self_uid", "")
PEER_NAME = _bridge_cfg.get("peer_name", "Peer")

_SELF_NAME, _SELF_HUMAN = _read_soul_identity()

if not BRIDGE_DM:
    sys.stderr.write(
        "bridge-collab-listener: no bridge channel in ~/.fleet/config.json — "
        "open Fleet and use Pair to configure the Bridge first.\n"
    )
    sys.exit(1)

SLACK_TOOLS = (
    "mcp__plugin_slack_slack__slack_read_channel,"
    "mcp__plugin_slack_slack__slack_send_message,"
    "mcp__plugin_slack_slack__slack_read_thread"
)
SLACK_TOOLS_WITH_FS = (
    "mcp__plugin_slack_slack__slack_read_channel,"
    "mcp__plugin_slack_slack__slack_send_message,"
    "mcp__plugin_slack_slack__slack_read_thread,"
    "Bash,Edit,Write,Read"
)
POLL_INTERVAL   = 60   # seconds between watch-mode polls
COLLAB_INTERVAL = 60   # seconds between collab-mode polls
MAX_EXCHANGES   = 20   # circuit-breaker per session
CLAUDE_TIMEOUT  = 300  # seconds per claude --print call (extended for FS work)

DEFAULT_WORKDIR = str(Path.home())

STATE_DIR = Path.home() / ".claude" / "monitor-state"
ENV_FILE  = STATE_DIR / ".env"
LOG_FILE  = STATE_DIR / "bridge-collab-listener.log"

_SSL_CTX = ssl._create_unverified_context()

# ── env / token ───────────────────────────────────────────────────────────────

def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def _slack_token() -> str | None:
    p = Path.home() / ".fleet" / "secrets.json"
    if p.exists():
        try:
            tok = json.loads(p.read_text()).get("slack_token")
            if tok:
                return tok
        except Exception:
            pass
    return os.environ.get("SLACK_BOT_TOKEN")


# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bridge-collab-listener")


# ── Slack API ─────────────────────────────────────────────────────────────────

def _dm_history(limit: int = 25) -> list[dict]:
    token = _slack_token()
    if not token:
        raise RuntimeError("no Slack token (SLACK_BOT_TOKEN or ~/.fleet/secrets.json)")
    url = f"https://slack.com/api/conversations.history?channel={BRIDGE_DM}&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"conversations.history error: {data.get('error')}")
    return list(reversed(data.get("messages", [])))


def _post_message(channel: str, text: str) -> None:
    """Post directly via Slack API — no claude --print round-trip."""
    token = _slack_token()
    if not token:
        log.warning("no Slack token — cannot post message")
        return
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            log.warning("chat.postMessage error: %s", data.get("error"))
    except Exception as e:
        log.warning("_post_message failed: %s", e)


def _post_heartbeat() -> None:
    """Post a presence announcement to #fleet-pairing."""
    text = f"::fleet-presence:: agent={_SELF_NAME} human={_SELF_HUMAN} uid={SELF_UID}"
    _post_message(FLEET_PAIRING_CHANNEL, text)


def _post_via_claude(text: str) -> None:
    prompt = (
        f'Send this exact message to Slack channel {BRIDGE_DM}: "{text}"\n'
        "Do not add 'Sent using Claude' — it is appended automatically."
    )
    try:
        subprocess.run(
            [CLAUDE_BIN, "--print", "--allowedTools",
             "mcp__plugin_slack_slack__slack_send_message"],
            input=prompt, capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log.warning("_post_via_claude failed: %s", e)


# ── workdir ───────────────────────────────────────────────────────────────────

def _parse_workdir(task_text: str) -> str:
    """Extract workdir=<path> from the sentinel text, defaulting to home."""
    m = re.search(r'workdir=(\S+)', task_text)
    if m:
        p = Path(m.group(1)).expanduser()
        if p.exists():
            return str(p)
        log.warning("workdir %s does not exist — using home dir", p)
    return DEFAULT_WORKDIR


# ── detection ─────────────────────────────────────────────────────────────────

def _find_open_peer_task(msgs: list[dict]) -> str | None:
    """Returns the ::collab-task:: message text if there's an open task where
    the peer's last signed message is newer than this agent's last reply, else None."""
    task_idx = None
    task_text = None
    for i, m in enumerate(msgs):
        if m.get("user") == PEER_UID and "::collab-task::" in (m.get("text") or ""):
            task_idx = i
            task_text = m.get("text") or ""
    log.info("detection: %d msgs, task_idx=%s", len(msgs), task_idx)
    if task_idx is None:
        return None

    post_task = msgs[task_idx + 1:]

    # Explicit close signal — must start the message, not appear mid-prose
    for m in post_task:
        if (m.get("text") or "").strip().startswith("::task complete::"):
            return None

    # Find timestamps of peer's and self's last signed messages
    last_peer_ts: float | None = None
    last_self_ts: float | None = None
    for m in post_task:
        text = m.get("text") or ""
        ts = float(m.get("ts", 0))
        if m.get("user") == PEER_UID and f"— {PEER_NAME}" in text:
            last_peer_ts = ts
        if m.get("user") == SELF_UID and f"— {_SELF_NAME}" in text:
            last_self_ts = ts

    log.info("detection: task_idx=%s last_peer_ts=%s last_self_ts=%s",
             task_idx, last_peer_ts, last_self_ts)

    if last_peer_ts is None:
        return None
    if last_self_ts is not None and last_self_ts > last_peer_ts:
        return None

    return task_text


# ── collab prompt ─────────────────────────────────────────────────────────────

def _make_collab_prompt(workdir: str) -> str:
    peer_auto_sentinel = f"::{PEER_NAME.lower()}-auto::"
    return (
        f"Working directory: {workdir}\n"
        f"Check Slack channel {BRIDGE_DM} (limit 20). "
        f"{PEER_UID} = {PEER_NAME} (peer agent). {SELF_UID} = {_SELF_NAME} (you). "
        "A collab session is active (::collab-task:: was posted). Rules:\n"
        "1. If '::task complete::' appears after the most recent '::collab-task::' sentinel, "
        "output exactly: TASK_COMPLETE\n"
        f"2. Find the most recent message from {PEER_UID} that ends with '— {PEER_NAME}'. "
        f"If {_SELF_NAME} ({SELF_UID}) has already replied after it, output exactly: NO_OP\n"
        f"3. If a {PEER_NAME}-signed message needs a reply: respond substantively. "
        f"You have full tool access — Bash, Edit, Write, Read. "
        f"Working directory is {workdir}. "
        "If the task requires building something, actually do it using your tools "
        "(create files, run git commands, scaffold repos, etc.), then report what you "
        f"did in the channel. Sign with ' — {_SELF_NAME}'. Do NOT add 'Sent using Claude'.\n"
        f"4. Never reply to messages that are control signals "
        f"(::collab-task::, ::task complete::, {peer_auto_sentinel}).\n"
        "When in doubt, NO_OP."
    )


# ── collab session ────────────────────────────────────────────────────────────

def _run_collab_session(workdir: str) -> None:
    log.info("collab session open — workdir=%s, max %d exchanges", workdir, MAX_EXCHANGES)
    prompt = _make_collab_prompt(workdir)
    cwd = workdir if Path(workdir).exists() else DEFAULT_WORKDIR
    exchanges = 0
    while True:
        time.sleep(COLLAB_INTERVAL)
        if exchanges >= MAX_EXCHANGES:
            log.warning("collab: %d exchange cap hit — posting ::task complete::", MAX_EXCHANGES)
            _post_via_claude(f"::task complete:: — {_SELF_NAME}")
            break
        try:
            r = subprocess.run(
                [CLAUDE_BIN, "--print", "--allowedTools", SLACK_TOOLS_WITH_FS],
                input=prompt,
                capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT,
                cwd=cwd,
            )
            # strip permission-noise lines
            out = "\n".join(
                l for l in r.stdout.splitlines()
                if not l.startswith("Permission allow rule")
            ).strip()
            log.info("collab poll: %r", out[:200])
            if out == "TASK_COMPLETE":
                log.info("collab: TASK_COMPLETE — returning to watch mode")
                break
            elif out and out != "NO_OP":
                exchanges += 1
                log.info("collab: exchange %d/%d responded", exchanges, MAX_EXCHANGES)
        except subprocess.TimeoutExpired:
            log.warning("collab: claude timed out after %ds", CLAUDE_TIMEOUT)
        except Exception as e:
            log.exception("collab: unexpected error: %s", e)


# ── watch loop ────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("bridge-collab-listener starting — polling every %ds", POLL_INTERVAL)

    def _heartbeat_loop():
        while True:
            try:
                _post_heartbeat()
            except Exception as e:
                log.warning("heartbeat error: %s", e)
            time.sleep(HEARTBEAT_INTERVAL)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    try:
        _post_heartbeat()
    except Exception as e:
        log.warning("initial heartbeat error: %s", e)

    no_task_streak = 0
    while True:
        try:
            msgs = _dm_history(limit=25)
            task_text = _find_open_peer_task(msgs)
            if task_text is not None:
                no_task_streak = 0
                workdir = _parse_workdir(task_text)
                log.info("open collab task detected — workdir=%s", workdir)
                _post_message(
                    BRIDGE_DM,
                    f"[{_SELF_NAME}] Collab session started — workdir: `{workdir}`"
                )
                _run_collab_session(workdir)
                log.info("collab session ended — resuming watch mode")
            else:
                no_task_streak += 1
                if no_task_streak % 10 == 0:
                    log.info("watch: %d consecutive no-task polls", no_task_streak)
        except Exception as e:
            log.exception("watch loop error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
