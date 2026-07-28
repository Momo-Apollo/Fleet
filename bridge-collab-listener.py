#!/usr/bin/env python3
"""Bridge collab listener — persistent daemon that monitors the Bridge DM for
peer-initiated ::collab-task:: sentinels and drives the collab response loop."""

from __future__ import annotations

import datetime
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

def _claude_env() -> dict:
    """Return os.environ copy with CLAUDE_CODE_OAUTH_TOKEN injected from secrets.json."""
    env = os.environ.copy()
    p = Path.home() / ".fleet" / "secrets.json"
    if p.exists():
        try:
            tok = json.loads(p.read_text()).get("claude_token", "")
            if tok:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        except Exception:
            pass
    return env

def _check_claude_token() -> None:
    """Warn via Slack if the Claude token is missing, expired, or expiring soon."""
    p = Path.home() / ".fleet" / "secrets.json"
    if not p.exists():
        return
    try:
        secrets = json.loads(p.read_text())
    except Exception:
        return

    if not secrets.get("claude_token"):
        log.warning("claude_token missing from secrets.json — auto-respond will time out")
        return

    expires_str = secrets.get("claude_token_expires", "")
    if not expires_str:
        return

    try:
        expires = datetime.date.fromisoformat(expires_str)
    except ValueError:
        return

    days_left = (expires - datetime.date.today()).days
    if days_left > 30:
        return

    if days_left <= 0:
        msg = (
            f":rotating_light: Claude token expired {abs(days_left)} day(s) ago. "
            f"Auto-respond is down. Run `claude setup-token`, update `~/.fleet/secrets.json` as `claude_token`, "
            f"then restart the daemon."
        )
    else:
        msg = (
            f":warning: Claude token expires in {days_left} day(s). "
            f"Run `claude setup-token`, update `~/.fleet/secrets.json` as `claude_token`, "
            f"then restart the daemon."
        )

    log.warning("token check: %s", msg)
    try:
        cfg = _load_bridge_cfg()
        ch = cfg.get("channel", "")
        if ch:
            _post_message(ch, msg)
    except Exception as e:
        log.warning("token warning DM failed: %s", e)


def _is_auth_error(stderr: str) -> bool:
    """Return True if claude subprocess stderr indicates an authentication failure."""
    low = (stderr or "").lower()
    return any(k in low for k in ("authentication", "unauthorized", "invalid token", "oauth", "login required"))


def _load_bridge_cfg() -> dict:
    p = Path.home() / ".fleet" / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text()).get("bridge", {})
        except Exception:
            pass
    return {}

def _get_peer_uids(cfg: dict) -> set:
    """Build set of peer UIDs from group (peers list) or point-to-point (peer_uid) config."""
    peers = cfg.get("peers")
    if peers:
        return {p["uid"] for p in peers if p.get("uid")}
    uid = cfg.get("peer_uid", "")
    return {uid} if uid else set()

def _get_peer_labels(cfg: dict) -> dict:
    """Build uid→agent-name mapping for all configured peers."""
    peers = cfg.get("peers")
    if peers:
        return {p["uid"]: p.get("agent", "Peer") for p in peers if p.get("uid")}
    uid = cfg.get("peer_uid", "")
    name = cfg.get("peer_name", "Peer")
    return {uid: name} if uid else {}

def _read_soul_identity() -> tuple:
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

BRIDGE_DM  = _bridge_cfg.get("channel", "")
SELF_UID   = _bridge_cfg.get("self_uid", "")

_SELF_NAME, _SELF_HUMAN = _read_soul_identity()

if not BRIDGE_DM:
    sys.stderr.write(
        "bridge-collab-listener: no bridge channel yet — "
        "will poll until Fleet is paired.\n"
    )

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
STATE_DIR.mkdir(parents=True, exist_ok=True)
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


def _bot_token() -> str | None:
    p = Path.home() / ".fleet" / "secrets.json"
    if p.exists():
        try:
            secrets = json.loads(p.read_text())
            tok = secrets.get("slack_bot_token") or secrets.get("slack_token")
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

def _dm_history(limit: int = 25, channel: str = "") -> list:
    token = _slack_token()
    if not token:
        raise RuntimeError("no Slack token (SLACK_BOT_TOKEN or ~/.fleet/secrets.json)")
    ch = channel or BRIDGE_DM
    url = f"https://slack.com/api/conversations.history?channel={ch}&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"conversations.history error: {data.get('error')}")
    return list(reversed(data.get("messages", [])))


def _post_message(channel: str, text: str) -> None:
    """Post directly via Slack API — no claude --print round-trip."""
    token = _bot_token()
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


def _post_via_claude(text: str, channel: str = "") -> None:
    ch = channel or BRIDGE_DM
    prompt = (
        f'Send this exact message to Slack channel {ch}: "{text}"\n'
        "Do not add 'Sent using Claude' — it is appended automatically."
    )
    try:
        subprocess.run(
            [CLAUDE_BIN, "--print", "--allowedTools",
             "mcp__plugin_slack_slack__slack_send_message"],
            input=prompt, capture_output=True, text=True, timeout=30,
            env=_claude_env(),
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


# ── auto-respond ─────────────────────────────────────────────────────────────

def _detect_auto_signal(msgs: list, self_name: str) -> bool | None:
    """Return True if most recent ::selfName-auto:: signal is 'on', False if 'off', None if absent."""
    sentinel = f"::{self_name.lower()}-auto"
    for m in reversed(msgs):
        txt = m.get("text") or ""
        if sentinel in txt:
            return "state=on" in txt
    return None


def _make_auto_prompt(bridge_dm: str, peer_uids: set, peer_labels: dict, self_uid: str, self_name: str) -> str:
    peer_id_block = " ".join(f"{uid} = {name}" for uid, name in peer_labels.items())
    peer_names_str = "/".join(peer_labels.values())
    return (
        f"Check Slack channel {bridge_dm} (limit 10). "
        f"{self_uid} = {self_name} (you). {peer_id_block} (peer(s)). "
        "Rules — follow ALL of them:\n"
        f"1. ONLY respond to messages from peers ({peer_names_str}). "
        f"Never respond to {self_name}'s own messages.\n"
        f"2. Only reply if the most recent peer message has NO reply from {self_name} ({self_uid}) after it. "
        f"If {self_name} has already replied after the last peer message, output exactly: NO_OP\n"
        "3. If the channel's most recent message is from you or a human, output exactly: NO_OP\n"
        f"4. If a reply is genuinely warranted: send a direct, substantive response. "
        f"Sign with ' — {self_name}'. Do NOT add 'Sent using Claude'.\n"
        "When in doubt, NO_OP."
    )


def _run_auto_session(bridge_dm: str, peer_uids: set, peer_labels: dict, self_uid: str, self_name: str) -> None:
    """Poll and auto-respond until ::selfName-auto state=off:: is detected."""
    log.info("auto-respond session started")
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            fresh = _load_bridge_cfg()
            cfg = fresh if fresh else {}
            current_dm = cfg.get("channel", bridge_dm)
            msgs = _dm_history(limit=25, channel=current_dm)
            # Check if auto was turned off
            want_on = _detect_auto_signal(msgs, self_name)
            if want_on is False:
                log.info("auto-respond: off signal received — returning to watch mode")
                return
            prompt = _make_auto_prompt(current_dm, peer_uids, peer_labels, self_uid, self_name)
            r = subprocess.run(
                [CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
                 "--allowedTools", SLACK_TOOLS],
                input=prompt,
                capture_output=True, text=True,
                timeout=120,
                env=_claude_env(),
            )
            if _is_auth_error(r.stderr):
                log.error("auto-respond: auth failure — token expired or invalid")
                cfg2 = _load_bridge_cfg()
                ch2 = cfg2.get("channel", "")
                if ch2:
                    _post_message(ch2,
                        ":rotating_light: Auto-respond auth failure — Claude token expired or invalid. "
                        "Run `claude setup-token`, update `~/.fleet/secrets.json` as `claude_token`, "
                        "then restart the daemon.")
                return
            out = "\n".join(
                l for l in r.stdout.splitlines()
                if not l.startswith("Permission allow rule")
            ).strip()
            if out and out not in ("NO_OP", "TASK_COMPLETE"):
                log.info("auto-respond: replied")
        except Exception as e:
            log.exception("auto-respond error: %s", e)


# ── detection ─────────────────────────────────────────────────────────────────

def _find_open_peer_task(msgs: list, peer_uids: set, peer_labels: dict, self_uid: str, self_name: str) -> str | None:
    """Returns the ::collab-task:: message text if there's an open task where
    a peer's last signed message is newer than this agent's last reply, else None."""
    task_idx = None
    task_text = None
    for i, m in enumerate(msgs):
        if m.get("user") in peer_uids and "::collab-task::" in (m.get("text") or ""):
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

    # Find timestamps of peers' and self's last signed messages
    last_peer_ts: float | None = None
    last_self_ts: float | None = None
    for m in post_task:
        text = m.get("text") or ""
        ts = float(m.get("ts", 0))
        if m.get("user") in peer_uids and any(f"— {name}" in text for name in peer_labels.values()):
            last_peer_ts = ts
        if m.get("user") == self_uid and f"— {self_name}" in text:
            last_self_ts = ts

    log.info("detection: task_idx=%s last_peer_ts=%s last_self_ts=%s",
             task_idx, last_peer_ts, last_self_ts)

    if last_peer_ts is None:
        return None
    if last_self_ts is not None and last_self_ts > last_peer_ts:
        return None

    return task_text


# ── collab prompt ─────────────────────────────────────────────────────────────

def _make_collab_prompt(workdir: str, bridge_dm: str, peer_uids: set, peer_labels: dict, self_uid: str, self_name: str) -> str:
    peer_id_block = " ".join(f"{uid} = {name}" for uid, name in peer_labels.items())
    peer_names_str = "/".join(peer_labels.values())
    auto_sentinels = ", ".join(f"::{name.lower()}-auto::" for name in peer_labels.values())
    return (
        f"Working directory: {workdir}\n"
        f"Check Slack channel {bridge_dm} (limit 20). "
        f"{self_uid} = {self_name} (you). {peer_id_block} (peer(s)). "
        "A collab session is active (::collab-task:: was posted). Rules:\n"
        "1. If '::task complete::' appears after the most recent '::collab-task::' sentinel, "
        "output exactly: TASK_COMPLETE\n"
        f"2. Find the most recent message signed '— [peer name]' from any peer ({peer_names_str}). "
        f"If {self_name} ({self_uid}) has already replied after it, output exactly: NO_OP\n"
        f"3. If a peer-signed message needs a reply: respond substantively. "
        f"You have full tool access — Bash, Edit, Write, Read. "
        f"Working directory is {workdir}. "
        "If the task requires building something, actually do it using your tools "
        "(create files, run git commands, scaffold repos, etc.), then report what you "
        f"did in the channel. Sign with ' — {self_name}'. Do NOT add 'Sent using Claude'.\n"
        f"4. Never reply to messages that are control signals "
        f"(::collab-task::, ::task complete::, {auto_sentinels}).\n"
        "When in doubt, NO_OP."
    )


# ── collab session ────────────────────────────────────────────────────────────

def _run_collab_session(workdir: str, bridge_dm: str, peer_uids: set, peer_labels: dict, self_uid: str, self_name: str) -> None:
    log.info("collab session open — workdir=%s, max %d exchanges", workdir, MAX_EXCHANGES)
    prompt = _make_collab_prompt(workdir, bridge_dm, peer_uids, peer_labels, self_uid, self_name)
    cwd = workdir if Path(workdir).exists() else DEFAULT_WORKDIR
    exchanges = 0
    while True:
        time.sleep(COLLAB_INTERVAL)
        if exchanges >= MAX_EXCHANGES:
            log.warning("collab: %d exchange cap hit — posting ::task complete::", MAX_EXCHANGES)
            _post_via_claude(f"::task complete:: — {self_name}", bridge_dm)
            break
        try:
            r = subprocess.run(
                [CLAUDE_BIN, "--print", "--allowedTools", SLACK_TOOLS_WITH_FS],
                input=prompt,
                capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT,
                cwd=cwd,
                env=_claude_env(),
            )
            if _is_auth_error(r.stderr):
                log.error("collab: auth failure — token expired or invalid")
                _post_message(bridge_dm,
                    ":rotating_light: Collab auth failure — Claude token expired or invalid. "
                    "Run `claude setup-token`, update `~/.fleet/secrets.json` as `claude_token`, "
                    "then restart the daemon.")
                break
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
    _check_claude_token()

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

    _last_cfg = _bridge_cfg
    no_task_streak = 0
    while True:
        try:
            # hot-reload config; fall back to last good read on parse error
            fresh = _load_bridge_cfg()
            if fresh:
                _last_cfg = fresh
            cfg         = _last_cfg
            bridge_dm   = cfg.get("channel", BRIDGE_DM)
            self_uid    = cfg.get("self_uid", SELF_UID)
            self_name   = cfg.get("self_name", _SELF_NAME)
            peer_uids   = _get_peer_uids(cfg)
            peer_labels = _get_peer_labels(cfg)

            if not bridge_dm:
                log.info("watch: no bridge channel configured yet — waiting for pair")
                time.sleep(POLL_INTERVAL)
                continue

            msgs = _dm_history(limit=25, channel=bridge_dm)
            task_text = _find_open_peer_task(msgs, peer_uids, peer_labels, self_uid, self_name)
            if task_text is not None:
                no_task_streak = 0
                workdir = _parse_workdir(task_text)
                log.info("open collab task detected — workdir=%s", workdir)
                _post_message(
                    bridge_dm,
                    f"[{self_name}] Collab session started — workdir: `{workdir}`"
                )
                _run_collab_session(workdir, bridge_dm, peer_uids, peer_labels, self_uid, self_name)
                log.info("collab session ended — resuming watch mode")
            else:
                log.info("auto check: ch=%s n=%d has_sentinel=%s",
                         bridge_dm, len(msgs),
                         any(f"::{self_name.lower()}-auto" in (m.get("text") or "")
                             for m in msgs))
                want_auto = _detect_auto_signal(msgs, self_name)
                if want_auto is True:
                    log.info("auto-respond signal detected — entering auto session")
                    _run_auto_session(bridge_dm, peer_uids, peer_labels, self_uid, self_name)
                    log.info("auto session ended — resuming watch mode")
                else:
                    no_task_streak += 1
                    if no_task_streak % 10 == 0:
                        log.info("watch: %d consecutive no-task polls", no_task_streak)
        except Exception as e:
            log.exception("watch loop error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
