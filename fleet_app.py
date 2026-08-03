#!/usr/bin/env python3
"""Fleet — agent-agnostic desk app for managing launchd agents via CustomTkinter."""
from __future__ import annotations

import os
import re
import sys
import json
import subprocess
import threading
import time
import uuid as _uuid
from pathlib import Path
import ssl
import urllib.request
import urllib.error
import urllib.parse
_SSL_CTX = ssl._create_unverified_context()
from datetime import datetime, timezone, timedelta
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

import base64 as _base64

try:
    import fleet_memory as _fleet_memory
except ImportError:
    _fleet_memory = None  # type: ignore

_HAS_TKDND = False
try:
    from tkinterdnd2 import TkinterDnD as _TkDnD, DND_FILES as _DND_FILES
    _HAS_TKDND = True
except ImportError:
    pass


def _parse_drop_data(data: str) -> list:
    """Parse tkdnd event.data into a list of file paths (handles braced paths with spaces)."""
    paths = []
    i = 0
    while i < len(data):
        if data[i] == '{':
            end = data.find('}', i)
            if end == -1:
                break
            paths.append(data[i + 1:end])
            i = end + 1
        elif data[i] == ' ':
            i += 1
        else:
            end = data.find(' ', i)
            if end == -1:
                end = len(data)
            paths.append(data[i:end])
            i = end
    return [p for p in paths if p]


_IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
# Per-file character cap for text attachments (~50KB ≈ 12K tokens).
_MAX_FILE_CHARS = 50_000


def _build_content_blocks(files: list, text: str) -> list:
    """Build an Anthropic-API content-block array from local files + a text message."""
    blocks = []
    for fpath in files:
        p = Path(fpath)
        suffix = p.suffix.lower()
        media_type = _IMAGE_MEDIA_TYPES.get(suffix)
        if media_type:
            try:
                with open(fpath, "rb") as fh:
                    b64 = _base64.b64encode(fh.read()).decode()
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            except Exception as e:
                blocks.append({"type": "text", "text": f"[{p.name}: could not read — {e}]"})
        else:
            try:
                content = p.read_text(errors="replace")
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + f"\n[truncated — showing first {_MAX_FILE_CHARS:,} of {len(content):,} chars]"
                blocks.append({"type": "text", "text": f"[{p.name}]\n```\n{content}\n```"})
            except Exception as e:
                blocks.append({"type": "text", "text": f"[{p.name}: could not read — {e}]"})
    blocks.append({"type": "text", "text": text})
    return blocks


def _extract_stream_json_result(output: str) -> str:
    """Pull the 'result' text out of a --output-format stream-json response."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            return obj.get("result", "") or "(no output)"
    return "(no output)"

import customtkinter as ctk

# ── Paths ──────────────────────────────────────────────────────────────────────
FLEET_DIR = Path.home() / ".fleet"

CONFIG_FILE = FLEET_DIR / "config.json"
STATUS_DIR = FLEET_DIR / "status"
AUDIT_LOG = FLEET_DIR / "console-audit.log"
LOGS_DIR = FLEET_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
FLEET_PAIRING_CHANNEL = "C0BK59E8XLZ"
# Repo root — resolved through the symlink so git pull lands in the right place
_REPO_DIR = Path(__file__).resolve().parent
# Roster paths — read from config.json; None if not configured
def _cfg_roster_paths():
    try:
        data = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
        rf  = data.get("roster_file", "")
        raf = data.get("roster_agents_file", "")
        return (
            Path(os.path.expanduser(rf)) if rf else None,
            Path(os.path.expanduser(raf)) if raf else None,
        )
    except Exception:
        return None, None

ROSTER_FILE, ROSTER_AGENTS_FILE = _cfg_roster_paths()


def _fleet_log(path: Path, tag: str, text: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {tag} | {text}\n")
    except Exception:
        pass

# Full workbench tool set — all tools explicitly named (no blanket skip flag)
_WORKBENCH_TOOLS = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Task,NotebookRead,NotebookEdit"

_AUTO_SUMMARY_THRESHOLD = 100  # chars; below this a turn is considered tool-only/silent
_AUTO_SUMMARY_PROMPT = (
    "Recap what you just did — a short bullet list of completed actions. "
    "Be specific (file edited, command run, result). No preamble."
)


def _audit(action: str, target: str, detail: str = "") -> None:
    """Append a line to the console audit log before executing any mutation."""
    FLEET_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{action}\t{target}\t{detail}\n"
    with open(AUDIT_LOG, "a") as f:
        f.write(line)

# ── Colors — tastytrade brand ──────────────────────────────────────────────────
C_BRAND  = "#E21E26"   # tastytrade red
C_GREEN  = "#2DC653"
C_YELLOW = "#FFC72C"
C_RED    = "#E21E26"
C_GRAY   = "#9BA1A8"

C_BG       = "#1D191B"
C_CARD     = "#252022"
C_HEADER   = "#141214"
C_BORDER   = "#3A3336"
C_MUTED    = "#9B9293"
C_LOG_BG   = "#1D191B"

BTN_STOP    = ("#8B1A1A", "#A31F1F")
BTN_START   = ("#14532D", "#166534")
BTN_NEUTRAL = ("#3A3336", "#4A4547")
BTN_RESTART = ("#1E3A5F", "#1D4ED8")

UID = str(os.getuid())


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"refresh_interval_seconds": 10, "agents": []}


def _reconcile_agents() -> None:
    """Merge ~/Library/LaunchAgents plists into config.json — add new, drop removed.

    Scans all non-Apple LaunchAgents plists (avoids com.apple.* system entries).
    Config.json wins for display_name/description/log paths on existing entries.
    New plists get defaults derived from their label. Entries whose plist file no
    longer exists on disk are dropped.
    """
    import plistlib
    cfg = load_config()
    la_dir = Path.home() / "Library" / "LaunchAgents"
    home_str = str(Path.home())

    def _tilde(s: str | None) -> str | None:
        return ("~" + s[len(home_str):]) if s and s.startswith(home_str) else (s or None)

    by_label: dict[str, dict] = {a["label"]: a for a in cfg.get("agents", [])}

    # Derive Fleet-owned label prefixes from what's already in config.json
    # (e.g. "com.manouel.", "com.tastytrade.") so we don't pull in Adobe/Spotify/etc.
    prefixes: set[str] = set()
    for lbl in by_label:
        parts = lbl.split(".")
        if len(parts) >= 3:
            prefixes.add(f"{parts[0]}.{parts[1]}.")

    if la_dir.exists() and prefixes:
        for plist_path in sorted(la_dir.glob("*.plist")):
            try:
                with open(plist_path, "rb") as fh:
                    p = plistlib.load(fh)
                label = p.get("Label", "")
                if not label or not any(label.startswith(pfx) for pfx in prefixes):
                    continue
                plist_ref = f"~/Library/LaunchAgents/{plist_path.name}"
                log_out   = _tilde(p.get("StandardOutPath"))
                log_err   = _tilde(p.get("StandardErrorPath"))
                if label in by_label:
                    # Existing entry: refresh plist-derived fields so edits to the plist
                    # (log paths, program args) are picked up. Preserve display_name and
                    # description — those are the fields a rep might have customized.
                    by_label[label]["plist"]   = plist_ref
                    by_label[label]["log_out"] = log_out
                    by_label[label]["log_err"] = log_err
                else:
                    by_label[label] = {
                        "label":               label,
                        "display_name":        label.split(".")[-1].replace("-", " ").title(),
                        "description":         "",
                        "plist":               plist_ref,
                        "log_out":             log_out,
                        "log_err":             log_err,
                        "stale_after_seconds": 3600,
                    }
            except Exception:
                pass

    # Drop only prefix-owned entries whose plist is gone from disk.
    # Entries outside the known prefixes (hand-added by the rep) are left alone.
    kept = []
    for a in by_label.values():
        label     = a.get("label", "")
        plist_str = a.get("plist")
        if (any(label.startswith(pfx) for pfx in prefixes)
                and plist_str
                and not Path(os.path.expanduser(plist_str)).exists()):
            continue  # Fleet-owned plist gone — drop
        kept.append(a)

    cfg["agents"] = kept
    FLEET_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        json.dump(cfg, fh, indent=2)


def _read_frontmatter_desc(path: Path) -> str:
    """Extract the description: value from YAML frontmatter in a markdown file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return ""
        end = text.index("---", 3)
        for line in text[3:end].splitlines():
            if line.startswith("description:"):
                return line[len("description:"):].strip().strip("\"'")
    except Exception:
        pass
    return ""


def _discover_skills() -> list:
    """Return [(name, description)] from user commands and installed plugin commands."""
    results = []
    commands_dir = Path.home() / ".claude" / "commands"
    if commands_dir.exists():
        for f in sorted(commands_dir.glob("*.md")):
            results.append((f"/{f.stem}", _read_frontmatter_desc(f)))
    plugins_file = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if plugins_file.exists():
        try:
            data = json.loads(plugins_file.read_text())
            for plugin_id, installs in data.get("plugins", {}).items():
                if not installs:
                    continue
                install_path = Path(installs[-1]["installPath"])
                plugin_name = plugin_id.split("@")[0]
                cmds_dir = install_path / "commands"
                if cmds_dir.exists():
                    for f in sorted(cmds_dir.glob("*.md")):
                        results.append((f"/{plugin_name}:{f.stem}", _read_frontmatter_desc(f)))
        except Exception:
            pass
    return results


def _read_soul_identity() -> tuple:
    """Parse **Name:** and **Human:** from ~/.claude/SOUL.md; fall back to config.json."""
    soul = Path.home() / ".claude" / "SOUL.md"
    if soul.exists():
        try:
            text = soul.read_text(encoding="utf-8", errors="replace")
            name_m = re.search(r'\*\*Name:\*\*\s+(\S+)', text)
            human_m = re.search(r'\*\*Human:\*\*\s+(\S+)', text)
            name = name_m.group(1) if name_m else None
            human = human_m.group(1) if human_m else None
            if name and human:
                return name, human
        except Exception:
            pass
    cfg = load_config()
    return cfg.get("agent_name", "Agent"), cfg.get("user_name", "Human")


AGENT_NAME, HUMAN_NAME = _read_soul_identity()
_reconcile_agents()


def expand(p: str | None) -> Path | None:
    return Path(os.path.expanduser(p)) if p else None


def _load_roster() -> dict:
    if ROSTER_FILE and ROSTER_FILE.exists():
        try:
            with open(ROSTER_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_roster_agents() -> list:
    if ROSTER_AGENTS_FILE and ROSTER_AGENTS_FILE.exists():
        try:
            with open(ROSTER_AGENTS_FILE) as f:
                return list(json.load(f).get("agents", {}).keys())
        except Exception:
            pass
    return []


# ── launchctl wrapper ──────────────────────────────────────────────────────────

def lctl_list() -> dict[str, dict]:
    """Parse `launchctl list` → {label: {pid, exit_code}}."""
    try:
        raw = subprocess.check_output(["launchctl", "list"], text=True, timeout=5)
    except Exception:
        return {}
    out = {}
    for line in raw.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        pid_s, exit_s, label = parts
        try:
            pid = int(pid_s) if pid_s != "-" else 0
        except ValueError:
            pid = 0
        try:
            exit_code = int(exit_s)
        except ValueError:
            exit_code = 0
        out[label] = {"pid": pid, "exit_code": exit_code}
    return out


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        msg = (r.stdout + r.stderr).strip()
        return r.returncode == 0, msg or "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def lctl_start(label: str, plist: str | None) -> tuple[bool, str]:
    ok, msg = _run(["launchctl", "kickstart", f"gui/{UID}/{label}"])
    if ok:
        return True, "Started"
    if plist:
        ok2, msg2 = _run(["launchctl", "bootstrap", f"gui/{UID}", os.path.expanduser(plist)])
        if ok2:
            return True, "Bootstrapped"
        return False, msg2
    return False, msg


def lctl_stop(label: str, plist: str | None) -> tuple[bool, str]:
    ok, msg = _run(["launchctl", "bootout", f"gui/{UID}/{label}"])
    if ok:
        return True, "Stopped"
    if plist:
        ok2, msg2 = _run(["launchctl", "unload", os.path.expanduser(plist)])
        if ok2:
            return True, "Unloaded"
        return False, msg2
    return False, msg


def lctl_restart(label: str) -> tuple[bool, str]:
    ok, msg = _run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"])
    return (True, "Restarted") if ok else (False, msg)


def tail_log(path: str | None, n: int = 80) -> str:
    if not path:
        return "(no log configured)"
    p = expand(path)
    if not p or not p.exists():
        return f"(log not found: {p})"
    try:
        return subprocess.check_output(["tail", "-n", str(n), str(p)],
                                        text=True, timeout=5) or "(empty)"
    except Exception as e:
        return f"(error: {e})"


# ── Status derivation ──────────────────────────────────────────────────────────

def derive_status(agent: dict, lctl: dict[str, dict]) -> dict:
    label = agent["label"]
    effective_label = agent.get("parent_label", label)
    info = lctl.get(effective_label)

    # Optional rich status file (written by the agent's own heartbeat loop)
    rich = None
    sf = STATUS_DIR / f"{label}.json"
    if sf.exists():
        try:
            with open(sf) as f:
                rich = json.load(f)
        except Exception:
            pass

    if info is None:
        return {
            "state": "unloaded", "color": C_GRAY,
            "detail": "Not loaded in launchd", "pid": 0,
            "last_action": rich.get("last_successful_action") if rich else None,
        }

    pid = info["pid"]
    exit_code = info["exit_code"]

    if pid == 0:
        if exit_code == 0:
            return {
                "state": "idle", "color": C_GRAY,
                "detail": "Idle (last exit 0)",
                "pid": 0,
                "last_action": rich.get("last_successful_action") if rich else None,
            }
        detail = f"Stopped (exit {exit_code}) ⚠"
        return {
            "state": "stopped", "color": C_RED,
            "detail": detail, "pid": 0,
            "last_action": rich.get("last_successful_action") if rich else None,
        }

    # Running — check log staleness (use most-recent of out/err so healthy
    # processes don't go yellow just because nothing wrote to stderr)
    state, color = "running", C_GREEN
    detail = f"PID {pid}"
    stale_after = agent.get("stale_after_seconds", 3600)
    log_err = expand(agent.get("log_err"))
    log_out = expand(agent.get("log_out"))

    log_mtime = None
    for lf in (log_out, log_err):
        if lf and lf.exists():
            try:
                mt = lf.stat().st_mtime
                if log_mtime is None or mt > log_mtime:
                    log_mtime = mt
            except Exception:
                pass

    if log_mtime is not None:
        age = time.time() - log_mtime
        if age > stale_after:
            state, color = "stale", C_YELLOW
            detail = f"PID {pid}  log stale {int(age // 60)}m"
        else:
            secs = int(age)
            detail = f"PID {pid}  log {secs}s ago"

    if rich and rich.get("detail"):
        detail = rich["detail"]

    return {
        "state": state, "color": color, "detail": detail, "pid": pid,
        "last_action": rich.get("last_successful_action") if rich else None,
    }


# ── UI Components ──────────────────────────────────────────────────────────────

STATE_LABELS = {
    "running":  "RUNNING",
    "stale":    "STALE",
    "stopped":  "STOPPED",
    "idle":     "IDLE",
    "unloaded": "UNLOADED",
}


def _tk_safe(text: str) -> str:
    """Strip characters above U+FFFF — Tcl/Tk on Python 3.7 can't handle them."""
    return "".join(c for c in text if ord(c) <= 0xFFFF)


def _center_on_parent(win, parent):
    """Show a withdrawn CTkToplevel centered over its parent, keeping it on the same display."""
    win.update_idletasks()
    m = re.match(r'(\d+)x(\d+)', win.geometry())
    ww = int(m.group(1)) if m else win.winfo_reqwidth()
    wh = int(m.group(2)) if m else win.winfo_reqheight()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    x = px + (pw - ww) // 2
    y = py + (ph - wh) // 2
    win.geometry(f"{ww}x{wh}{x:+d}{y:+d}")
    win.deiconify()
    win.lift()


class AgentCard(ctk.CTkFrame):
    def __init__(self, parent, agent: dict, on_action, **kwargs):
        super().__init__(parent, corner_radius=8, border_width=1,
                         border_color=C_BORDER, fg_color=C_CARD, **kwargs)
        self.agent = agent
        self.on_action = on_action
        self._build()

    def _build(self):
        # Left column — dot, name, description, detail
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(14, 8), pady=10)

        top_row = ctk.CTkFrame(left, fg_color="transparent")
        top_row.pack(fill="x")

        self.dot = ctk.CTkLabel(top_row, text="●", font=("SF Pro Display", 18), width=22)
        self.dot.pack(side="left", padx=(0, 6))

        self.name_lbl = ctk.CTkLabel(
            top_row, text=self.agent["display_name"],
            font=("SF Pro Display", 14, "bold"), anchor="w"
        )
        self.name_lbl.pack(side="left")

        self.state_lbl = ctk.CTkLabel(
            top_row, text="", font=("SF Pro Mono", 10),
            text_color=C_MUTED, anchor="w"
        )
        self.state_lbl.pack(side="left", padx=(8, 0))

        desc = self.agent.get("description", "")
        if desc:
            ctk.CTkLabel(
                left, text=desc, font=("SF Pro Display", 11),
                text_color=C_MUTED, anchor="w"
            ).pack(fill="x")

        self.detail_lbl = ctk.CTkLabel(
            left, text="—", font=("SF Pro Mono", 11),
            text_color="#999999", anchor="w"
        )
        self.detail_lbl.pack(fill="x")

        # Right column — buttons
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="right", fill="y", padx=(4, 12), pady=10)

        self.logs_btn = ctk.CTkButton(
            right, text="Logs", width=68, height=28,
            font=("SF Pro Display", 12),
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=lambda: self.on_action("logs", self.agent)
        )
        self.logs_btn.pack(pady=(0, 4))

        self.restart_btn = ctk.CTkButton(
            right, text="Restart", width=68, height=28,
            font=("SF Pro Display", 12),
            fg_color=BTN_RESTART[0], hover_color=BTN_RESTART[1],
            command=lambda: self.on_action("restart", self.agent)
        )
        self.restart_btn.pack(pady=(0, 4))

        self.toggle_btn = ctk.CTkButton(
            right, text="Stop", width=68, height=28,
            font=("SF Pro Display", 12),
            command=lambda: self.on_action("toggle", self.agent)
        )
        self.toggle_btn.pack()

    def update(self, status: dict):
        state = status["state"]
        color = status["color"]
        detail = status["detail"]
        last_action = status.get("last_action")

        self.dot.configure(text_color=color)
        self.state_lbl.configure(text=STATE_LABELS.get(state, state))
        self.detail_lbl.configure(text=detail)

        is_running = state in ("running", "stale")
        if is_running:
            self.toggle_btn.configure(
                text="Stop", fg_color=BTN_STOP[0], hover_color=BTN_STOP[1]
            )
            self.restart_btn.configure(state="normal")
        else:
            self.toggle_btn.configure(
                text="Start", fg_color=BTN_START[0], hover_color=BTN_START[1]
            )
            self.restart_btn.configure(state="disabled")


class LogPanel(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color=C_LOG_BG, **kwargs)
        self._visible = False
        self._build()

    def _build(self):
        header = ctk.CTkFrame(self, fg_color="#1A1A1A", height=32)
        header.pack(fill="x")
        header.pack_propagate(False)

        self.title_lbl = ctk.CTkLabel(
            header, text="Logs", font=("SF Pro Display", 12, "bold"), anchor="w"
        )
        self.title_lbl.pack(side="left", padx=12, pady=6)

        ctk.CTkButton(
            header, text="✕", width=28, height=28,
            font=("SF Pro Display", 13), fg_color="transparent",
            hover_color=C_BORDER, command=self.hide
        ).pack(side="right", padx=8, pady=2)

        self.box = ctk.CTkTextbox(
            self, font=("SF Pro Mono", 11), wrap="none", height=200,
            fg_color=C_LOG_BG
        )
        self.box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.box.configure(state="disabled")

    def show(self, agent: dict, content: str):
        self.title_lbl.configure(text=f"Logs — {agent['display_name']}")
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("1.0", content)
        self.box.configure(state="disabled")
        self.box.see("end")
        if not self._visible:
            self.pack(fill="x", padx=8, pady=(0, 8))
            self._visible = True

    def hide(self):
        if self._visible:
            self.pack_forget()
            self._visible = False


class SummaryBar(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color="#1A1A1A", height=28, **kwargs)
        self.pack_propagate(False)
        self.lbl = ctk.CTkLabel(
            self, text="", font=("SF Pro Mono", 11), text_color=C_MUTED, anchor="w"
        )
        self.lbl.pack(side="left", padx=12, pady=4)

        self.status_lbl = ctk.CTkLabel(
            self, text="", font=("SF Pro Mono", 11), text_color=C_MUTED, anchor="e"
        )
        self.status_lbl.pack(side="right", padx=12, pady=4)

    def update(self, running: int, total: int, action_msg: str = ""):
        color = C_GREEN if running == total else (C_YELLOW if running > 0 else C_RED)
        self.lbl.configure(
            text=f"{running}/{total} running",
            text_color=color
        )
        self.status_lbl.configure(text=action_msg)


# ── File Attachment Mixin ─────────────────────────────────────────────────────

class _FileAttachMixin:
    """File picker, clipboard image paste, and chip strip UI — shared by all chat panels."""

    def _file_mixin_init(self):
        self._pending_files: list = []

    def _open_file_picker(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title="Attach files",
            initialdir=os.path.expanduser("~"),
        )
        for p in paths:
            self._add_file(p)

    def _add_file(self, path: str):
        if path not in self._pending_files:
            self._pending_files.append(path)
            self._rebuild_chips()

    def _remove_file(self, path: str):
        if path in self._pending_files:
            self._pending_files.remove(path)
            self._rebuild_chips()

    def _rebuild_chips(self):
        for w in self._chips_frame.winfo_children():
            w.destroy()
        if not self._pending_files:
            self._chips_frame.pack_forget()
            return
        for path in self._pending_files:
            name = Path(path).name
            chip = ctk.CTkFrame(self._chips_frame, fg_color=C_CARD, corner_radius=6,
                                border_width=1, border_color=C_BORDER)
            chip.pack(side="left", padx=(0, 4), pady=2)
            ctk.CTkLabel(chip, text=name, font=("SF Pro Mono", 10),
                         text_color=C_MUTED).pack(side="left", padx=(6, 2), pady=3)
            ctk.CTkButton(
                chip, text="x", width=18, height=18,
                font=("SF Pro Mono", 11),
                fg_color="transparent", hover_color=C_BORDER,
                command=lambda p=path: self._remove_file(p)
            ).pack(side="left", padx=(0, 2), pady=1)
        self._chips_frame.pack(fill="x", padx=8, pady=(0, 4), before=self._input_ref)

    def _try_clipboard_image(self):
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
            if img is not None and hasattr(img, "save"):
                FLEET_DIR.mkdir(exist_ok=True)
                tmp_path = str(FLEET_DIR / f"paste_{int(time.time())}.png")
                img.save(tmp_path, "PNG")
                return tmp_path
        except Exception:
            pass
        return None

    def _on_paste(self, event):
        img_path = self._try_clipboard_image()
        if img_path:
            self._add_file(img_path)
            return "break"
        return None

    def _register_drop_target(self, widget):
        if not _HAS_TKDND:
            return
        try:
            # CTkTextbox → ._textbox; CTkEntry → ._entry; fallback to widget itself
            target = getattr(widget, '_textbox', getattr(widget, '_entry', widget))
            target.drop_target_register(_DND_FILES)
            target.dnd_bind('<<Drop>>', self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event):
        for path in _parse_drop_data(event.data):
            self._add_file(path)


# ── Chat Window ────────────────────────────────────────────────────────────────

class ChatWindow(_FileAttachMixin, ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title(AGENT_NAME)
        self.geometry("780x600")
        self.minsize(520, 360)
        self.configure(fg_color=C_BG)
        self._first_message = True
        self._busy = False
        self._proc: subprocess.Popen | None = None
        self._build()
        self.bind("<Escape>", self._interrupt)
        self.after(50, lambda: _center_on_parent(self, parent))
        self.after(100, lambda: self.entry.focus())

    def _build(self):
        self.history = ctk.CTkTextbox(
            self, font=("SF Pro Display", 13), wrap="word",
            fg_color=C_CARD, activate_scrollbars=True
        )
        self.history.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.history.configure(state="disabled")

        self._file_mixin_init()
        self._chips_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._input_row = ctk.CTkFrame(self, fg_color="transparent")
        self._input_row.pack(fill="x", padx=8, pady=(0, 10))
        self._input_row.columnconfigure(0, weight=1)
        self._input_ref = self._input_row

        self.entry = ctk.CTkEntry(
            self._input_row, font=("SF Pro Display", 13), height=38,
            placeholder_text=f"Ask {AGENT_NAME}…"
        )
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.entry.bind("<Return>", lambda _e: self._send())
        self.entry.bind("<Command-v>", self._on_paste)
        self._register_drop_target(self.entry)

        ctk.CTkButton(
            self._input_row, text="⊕", width=38, height=38,
            font=("SF Pro Display", 16),
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_file_picker
        ).grid(row=0, column=1, padx=(0, 4))

        self.send_btn = ctk.CTkButton(
            self._input_row, text="Send", width=80, height=38,
            font=("SF Pro Display", 13, "bold"),
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._send
        )
        self.send_btn.grid(row=0, column=2)

    _SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _start_spinner(self):
        self._spinning = True
        self._spin_step = 0
        self.history.configure(state="normal")
        self.history.insert("end", self._SPINNER[0])
        self.history.configure(state="disabled")
        self._spin_id = self.after(100, self._tick_spinner)

    def _tick_spinner(self):
        if not getattr(self, "_spinning", False):
            return
        self._spin_step = (self._spin_step + 1) % len(self._SPINNER)
        self.history.configure(state="normal")
        self.history.delete("end-2c", "end-1c")
        self.history.insert("end-1c", self._SPINNER[self._spin_step])
        self.history.configure(state="disabled")
        self.history.see("end")
        self._spin_id = self.after(100, self._tick_spinner)

    def _stop_spinner(self):
        if not getattr(self, "_spinning", False):
            return
        self._spinning = False
        if hasattr(self, "_spin_id"):
            self.after_cancel(self._spin_id)
        self.history.configure(state="normal")
        self.history.delete("end-2c", "end-1c")
        self.history.configure(state="disabled")

    def _append(self, text: str):
        self._stop_spinner()
        self.history.configure(state="normal")
        self.history.insert("end", text)
        self.history.configure(state="disabled")
        self.history.see("end")

    def _append_activity(self, line: str):
        self.history.configure(state="normal")
        self.history._textbox.insert("end", f"  {line}\n", "activity")
        self.history._textbox.tag_config(
            "activity", foreground="#4A4547", font=("SF Pro Mono", 10)
        )
        self.history.see("end")
        self.history.configure(state="disabled")

    def _interrupt(self, _event=None):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            self._proc = None
            self._busy = False
            self._stop_spinner()
            self._append("\n[interrupted]\n")
            self.send_btn.configure(state="normal", text="Send")
            self.entry.configure(state="normal")
            self.entry.focus()

    def _send(self):
        if self._busy:
            return
        msg = self.entry.get().strip()
        if not msg:
            return
        self.entry.delete(0, "end")
        self._busy = True
        self.send_btn.configure(state="disabled", text="…")
        self.entry.configure(state="disabled")
        files = list(self._pending_files)
        self._pending_files.clear()
        self._rebuild_chips()

        suffix = f"  +{len(files)} file(s)" if files else ""
        self._append(f"\nYou:  {msg}{suffix}\n\n{AGENT_NAME}:  ")
        self._start_spinner()

        is_first = self._first_message
        self._first_message = False

        def run():
            cmd = ["/opt/homebrew/bin/claude", "--print", "--output-format", "stream-json", "--verbose"]
            if not is_first:
                cmd.append("--continue")

            stdin_data = None
            if files:
                cmd += ["--input-format", "stream-json"]
                blocks = _build_content_blocks(files, msg)
                stdin_data = json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": blocks},
                }) + "\n"
            else:
                cmd.append(msg)

            accumulated = ""
            got_any = False

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if stdin_data else None,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1
                )
                self._proc = proc
                if stdin_data:
                    def _write_stdin():
                        try:
                            proc.stdin.write(stdin_data)
                            proc.stdin.close()
                        except Exception:
                            pass
                    threading.Thread(target=_write_stdin, daemon=True).start()

                for raw_line in proc.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    t = obj.get("type")

                    if t == "assistant":
                        content = obj.get("message", {}).get("content", [])
                        text = "".join(
                            b.get("text", "")
                            for b in content
                            if b.get("type") == "text"
                        )
                        for b in content:
                            if b.get("type") == "tool_use" and b.get("name"):
                                tname = b["name"]
                                inp = b.get("input", {})
                                first_val = str(next(iter(inp.values()), ""))[:60] if inp else ""
                                label = f"⚙ {tname}({first_val})" if first_val else f"⚙ {tname}"
                                self.after(0, lambda l=label: self._append_activity(l))
                        if text and len(text) > len(accumulated):
                            delta = text[len(accumulated):]
                            accumulated = text
                            got_any = True
                            self.after(0, lambda d=delta: self._append(d))

                    elif t == "result":
                        final = obj.get("result", "")
                        if final and len(final) > len(accumulated):
                            delta = final[len(accumulated):]
                            accumulated = final
                            got_any = True
                            self.after(0, lambda d=delta: self._append(d))
                        break

                proc.wait()

                if not got_any:
                    err = ANSI_ESCAPE.sub("", proc.stderr.read()).strip()
                    if "too long" in err.lower() or "context" in err.lower():
                        fallback = "Prompt too long — session context is full. Start a new session (+ button) or use smaller files."
                    else:
                        fallback = err or "(no response)"
                    self.after(0, lambda t=fallback: self._append(t))

            except FileNotFoundError:
                self.after(0, lambda: self._append(
                    "claude not found at /opt/homebrew/bin/claude — is Claude Code installed?"
                ))
            except Exception as e:
                self.after(0, lambda: self._append(f"Error: {e}"))
            finally:
                self._proc = None

            self.after(0, self._on_done)

        threading.Thread(target=run, daemon=True).start()

    def _on_done(self):
        self._stop_spinner()
        self._append("\n\n" + "─" * 72 + "\n")
        self._busy = False
        self.send_btn.configure(state="normal", text="Send")
        self.entry.configure(state="normal")
        self.entry.focus()


# ── Add Agent Dialog ───────────────────────────────────────────────────────────

class AddAgentDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_save, existing_labels: list):
        super().__init__(parent)
        self.withdraw()
        self.title("Add Agent")
        self.geometry("500x440")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=C_BG)
        self.on_save = on_save
        self._existing_labels = existing_labels
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))

    def _build(self):
        ctk.CTkLabel(
            self, text="Add Agent",
            font=("SF Pro Display", 16, "bold")
        ).pack(anchor="w", padx=20, pady=(16, 12))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=20)
        form.columnconfigure(1, weight=1)

        # (label_text, key, browsable, placeholder)
        fields = [
            ("Display Name *", "display_name", False, "CAT Listener"),
            ("Label *",        "label",        False, "com.yourname.agent-name"),
            ("Description",    "description",  False, "what this agent does"),
            ("Plist path",     "plist",        True,  "~/Library/LaunchAgents/com.example.plist"),
            ("Stderr log",     "log_err",      True,  "~/.claude/monitor-state/agent.err"),
            ("Stdout log",     "log_out",      True,  "~/.claude/monitor-state/agent.out"),
            ("Stale after (s)","stale_after_seconds", False, "3600"),
        ]

        self._entries = {}
        for i, (lbl, key, browsable, placeholder) in enumerate(fields):
            ctk.CTkLabel(
                form, text=lbl, font=("SF Pro Display", 12),
                anchor="e", width=130
            ).grid(row=i, column=0, sticky="e", pady=5, padx=(0, 10))

            row = ctk.CTkFrame(form, fg_color="transparent")
            row.grid(row=i, column=1, sticky="ew", pady=5)
            row.columnconfigure(0, weight=1)

            entry = ctk.CTkEntry(
                row, font=("SF Pro Mono", 12), height=32,
                placeholder_text=placeholder
            )
            entry.grid(row=0, column=0, sticky="ew")
            self._entries[key] = entry

            if browsable:
                ctk.CTkButton(
                    row, text="…", width=32, height=32,
                    font=("SF Pro Display", 14),
                    fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
                    command=lambda e=entry: self._browse(e)
                ).grid(row=0, column=1, padx=(4, 0))

        self.err_lbl = ctk.CTkLabel(
            self, text="", font=("SF Pro Display", 11), text_color="#FF4040"
        )
        self.err_lbl.pack(pady=(10, 0))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(6, 20))

        ctk.CTkButton(
            btn_row, text="Cancel", width=100, height=36,
            font=("SF Pro Display", 13),
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self.destroy
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Add Agent", width=120, height=36,
            font=("SF Pro Display", 13, "bold"),
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._save
        ).pack(side="right")

    def _browse(self, entry):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            initialdir=os.path.expanduser("~"),
            filetypes=[("All files", "*.*"), ("Plists", "*.plist")]
        )
        if path:
            home = str(Path.home())
            if path.startswith(home):
                path = "~" + path[len(home):]
            entry.delete(0, "end")
            entry.insert(0, path)

    def _save(self):
        display_name = self._entries["display_name"].get().strip()
        label = self._entries["label"].get().strip()

        if not display_name:
            self.err_lbl.configure(text="Display Name is required")
            return
        if not label:
            self.err_lbl.configure(text="Label is required")
            return
        if label in self._existing_labels:
            self.err_lbl.configure(text=f"Label '{label}' already exists")
            return

        stale_raw = self._entries["stale_after_seconds"].get().strip()
        try:
            stale = int(stale_raw) if stale_raw else 3600
        except ValueError:
            self.err_lbl.configure(text="Stale after must be a number (seconds)")
            return

        agent = {"label": label, "display_name": display_name, "stale_after_seconds": stale}
        for key in ("description", "plist", "log_err", "log_out"):
            val = self._entries[key].get().strip()
            if val:
                agent[key] = val

        self.on_save(agent)
        self.destroy()


# ── Built Window ──────────────────────────────────────────────────────────────

class BuiltWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title("What We've Built")
        self.geometry("680x620")
        self.minsize(500, 400)
        self.configure(fg_color=C_BG)
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))

    def _build(self):
        agents   = load_config().get("agents", [])
        skills   = _discover_skills()

        sections = [
            {
                "category": "Listeners",
                "color":    "#2DC653",
                "items":    [(a["display_name"], a.get("description", "")) for a in agents],
            },
            {
                "category": "Skills",
                "color":    "#7C3AED",
                "items":    skills,
            },
            {
                "category": "Apps & Tools",
                "color":    "#FFC72C",
                "items": [
                    (f"{AGENT_NAME} Fleet", f"This app — launchd agent dashboard with built-in {AGENT_NAME} chat"),
                ],
            },
        ]

        header = ctk.CTkFrame(self, fg_color=C_HEADER, height=50)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(
            header, text="What We've Built",
            font=("SF Pro Display", 18, "bold")
        ).pack(side="left", padx=16, pady=12)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        for section in sections:
            if not section["items"]:
                continue
            cat_color = section["color"]

            cat_row = ctk.CTkFrame(scroll, fg_color="transparent")
            cat_row.pack(fill="x", pady=(12, 4))

            ctk.CTkFrame(cat_row, fg_color=cat_color, width=4, height=20,
                         corner_radius=2).pack(side="left", padx=(2, 8))
            ctk.CTkLabel(
                cat_row, text=section["category"].upper(),
                font=("SF Pro Display", 11, "bold"),
                text_color=cat_color
            ).pack(side="left")

            for name, desc in section["items"]:
                card = ctk.CTkFrame(scroll, corner_radius=8, border_width=1,
                                    border_color=C_BORDER, fg_color=C_CARD)
                card.pack(fill="x", pady=3)

                accent = ctk.CTkFrame(card, fg_color=cat_color, width=3,
                                      corner_radius=0)
                accent.pack(side="left", fill="y")

                body = ctk.CTkFrame(card, fg_color="transparent")
                body.pack(side="left", fill="both", expand=True,
                          padx=(10, 12), pady=10)

                ctk.CTkLabel(
                    body, text=_tk_safe(name),
                    font=("SF Pro Display", 13, "bold"), anchor="w"
                ).pack(fill="x")
                ctk.CTkLabel(
                    body, text=_tk_safe(desc),
                    font=("SF Pro Display", 11),
                    text_color=C_MUTED, anchor="w", wraplength=520
                ).pack(fill="x")


# ── Roster Window ─────────────────────────────────────────────────────────────

class RosterWindow(ctk.CTkToplevel):
    """Primary / secondary / tertiary bot picker — reads + writes roster.json."""

    _ROLE_COLORS = {"primary": C_YELLOW, "secondary": C_GREEN, "tertiary": "#7C3AED"}
    _ROLE_LABELS = {"primary": "PRIMARY", "secondary": "SECONDARY", "tertiary": "TERTIARY"}
    _ROLE_ICONS  = {"primary": "①", "secondary": "②", "tertiary": "③"}

    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title("Bot Roster")
        self.geometry("440x400")
        self.resizable(False, False)
        self.configure(fg_color=C_BG)
        self._agents = _load_roster_agents()
        self._roster = _load_roster()
        self._agent_names = self._read_agent_names()
        self._vars: dict = {}
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        self.wm_attributes("-topmost", True)

    def _read_agent_names(self) -> dict:
        if ROSTER_AGENTS_FILE and ROSTER_AGENTS_FILE.exists():
            try:
                with open(ROSTER_AGENTS_FILE) as f:
                    return {k: v.get("agent_name", k)
                            for k, v in json.load(f).get("agents", {}).items()}
            except Exception:
                pass
        return {}

    def _expiry_info(self) -> tuple:
        expires_at = self._roster.get("expires_at")
        if not expires_at:
            return "no expiry set", C_MUTED
        try:
            dt = datetime.fromisoformat(expires_at)
            now = datetime.now(tz=dt.tzinfo)
            secs = (dt - now).total_seconds()
            if secs < 0:
                return f"EXPIRED {int(-secs // 3600)}h ago", C_RED
            h, m = int(secs // 3600), int((secs % 3600) // 60)
            color = C_GREEN if secs > 4 * 3600 else C_YELLOW
            return f"expires in {h}h {m}m", color
        except Exception:
            return str(expires_at)[:16], C_MUTED

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=C_HEADER, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(
            hdr, text="Bot Roster", font=("SF Pro Display", 15, "bold")
        ).pack(side="left", padx=14, pady=10)

        if ROSTER_FILE is None:
            ctk.CTkLabel(
                self,
                text="Roster not configured.\n\nAdd roster_file and roster_agents_file\nto ~/.fleet/config.json to enable.",
                font=("SF Pro Display", 13), text_color=C_MUTED, justify="center",
            ).pack(expand=True)
            foot = ctk.CTkFrame(self, fg_color=C_HEADER, height=52)
            foot.pack(fill="x", side="bottom")
            foot.pack_propagate(False)
            self._set_btn = ctk.CTkButton(
                foot, text="Set Roster", width=110, height=36,
                font=("SF Pro Display", 13, "bold"),
                fg_color=C_MUTED, state="disabled",
            )
            self._set_btn.pack(side="right", padx=(8, 8), pady=8)
            self._err_lbl = ctk.CTkLabel(foot, text="", font=("SF Pro Display", 11), text_color=C_RED)
            self._err_lbl.pack(side="right", padx=(0, 8))
            return

        exp_text, exp_color = self._expiry_info()
        self._exp_lbl = ctk.CTkLabel(
            hdr, text=exp_text, font=("SF Pro Mono", 10), text_color=exp_color
        )
        self._exp_lbl.pack(side="right", padx=14, pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # Current roster
        ctk.CTkLabel(
            body, text="CURRENT", font=("SF Pro Mono", 10, "bold"),
            text_color=C_MUTED, anchor="w"
        ).pack(fill="x", pady=(0, 6))

        current_card = ctk.CTkFrame(body, fg_color=C_CARD, corner_radius=8,
                                    border_width=1, border_color=C_BORDER)
        current_card.pack(fill="x", pady=(0, 14))

        for role in ("primary", "secondary", "tertiary"):
            key   = self._roster.get(role, "—")
            name  = self._agent_names.get(key, key)
            color = self._ROLE_COLORS[role]
            row   = ctk.CTkFrame(current_card, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=5)
            ctk.CTkLabel(row, text=self._ROLE_ICONS[role],
                         font=("SF Pro Display", 14), text_color=color,
                         width=22).pack(side="left")
            ctk.CTkLabel(row, text=self._ROLE_LABELS[role],
                         font=("SF Pro Mono", 10, "bold"), text_color=color,
                         width=82, anchor="w").pack(side="left", padx=(4, 8))
            ctk.CTkLabel(row, text=key,
                         font=("SF Pro Mono", 12), text_color="#FFFFFF",
                         width=68, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=f"({name})",
                         font=("SF Pro Display", 11), text_color=C_MUTED,
                         anchor="w").pack(side="left", padx=(4, 0))

        # Pickers
        ctk.CTkLabel(
            body, text="UPDATE", font=("SF Pro Mono", 10, "bold"),
            text_color=C_MUTED, anchor="w"
        ).pack(fill="x", pady=(0, 6))

        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        for i, role in enumerate(("primary", "secondary", "tertiary")):
            color = self._ROLE_COLORS[role]
            ctk.CTkLabel(
                grid, text=self._ROLE_LABELS[role],
                font=("SF Pro Mono", 11, "bold"), text_color=color,
                anchor="e", width=90
            ).grid(row=i, column=0, sticky="e", padx=(0, 10), pady=5)
            var = ctk.StringVar(value=self._roster.get(role, self._agents[0] if self._agents else ""))
            self._vars[role] = var
            ctk.CTkOptionMenu(
                grid, variable=var,
                values=self._agents,
                font=("SF Pro Mono", 12),
                fg_color=C_CARD, button_color=C_BRAND,
                button_hover_color="#C41920",
                dropdown_fg_color=C_CARD,
                anchor="w",
            ).grid(row=i, column=1, sticky="ew", pady=5)

        # Footer
        foot = ctk.CTkFrame(self, fg_color=C_HEADER, height=52)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)

        set_by = self._roster.get("set_by", "")
        set_at = (self._roster.get("set_at") or "")[:16]
        self._meta_lbl = ctk.CTkLabel(
            foot, text=f"set by {set_by} · {set_at}" if set_by else "no roster on disk",
            font=("SF Pro Mono", 10), text_color=C_MUTED, anchor="w"
        )
        self._meta_lbl.pack(side="left", padx=14)

        self._err_lbl = ctk.CTkLabel(
            foot, text="", font=("SF Pro Display", 11), text_color=C_RED
        )
        self._err_lbl.pack(side="right", padx=(0, 8))

        self._set_btn = ctk.CTkButton(
            foot, text="Set Roster", width=110, height=36,
            font=("SF Pro Display", 13, "bold"),
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._set_roster
        )
        self._set_btn.pack(side="right", padx=(8, 8), pady=8)

    def _set_roster(self):
        primary   = self._vars["primary"].get()
        secondary = self._vars["secondary"].get()
        tertiary  = self._vars["tertiary"].get()

        if len({primary, secondary, tertiary}) < 3:
            self._err_lbl.configure(text="Each role needs a different agent")
            return
        self._err_lbl.configure(text="")

        _set_by = load_config().get("user_name", "agent")
        now     = datetime.now(tz=timezone.utc).astimezone()
        expires = now + timedelta(hours=26)
        new_roster = {
            "version":    self._roster.get("version", 0) + 1,
            "date":       now.strftime("%Y-%m-%d"),
            "primary":    primary,
            "secondary":  secondary,
            "tertiary":   tertiary,
            "set_by":     _set_by,
            "set_at":     now.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds"),
        }

        self._set_btn.configure(text="Pushing…", state="disabled")
        self._err_lbl.configure(text="")

        def _propagate():
            repo = str(ROSTER_FILE.parent)
            try:
                ROSTER_FILE.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "-C", repo, "pull", "--rebase", "--autostash"],
                    check=True, capture_output=True,
                )
                with open(ROSTER_FILE, "w") as f:
                    json.dump(new_roster, f, indent=2)
                subprocess.run(
                    ["git", "-C", repo, "add", "roster.json"],
                    check=True, capture_output=True,
                )
                commit_r = subprocess.run(
                    ["git", "-C", repo, "commit", "-m",
                     f"roster: {now.strftime('%Y-%m-%d')} set by {_set_by}"],
                    capture_output=True,
                )
                if commit_r.returncode == 0:
                    subprocess.run(
                        ["git", "-C", repo, "push"],
                        check=True, capture_output=True,
                    )
                self.after(0, _on_success)
            except subprocess.CalledProcessError as exc:
                msg = (exc.stderr or b"").decode().strip() or str(exc)
                self.after(0, lambda m=msg: _on_error(m))
            except Exception as exc:
                self.after(0, lambda m=str(exc): _on_error(m))

        def _on_success():
            self._roster = new_roster
            self._set_btn.configure(text="✓ Set", fg_color="#14532D", state="normal")
            self.after(2000, lambda: self._set_btn.configure(
                text="Set Roster", fg_color=BTN_START[0]))
            exp_text, exp_color = self._expiry_info()
            self._exp_lbl.configure(text=exp_text, text_color=exp_color)
            self._meta_lbl.configure(
                text=f"set by {_set_by} · {now.strftime('%Y-%m-%dT%H:%M')}"
            )

        def _on_error(msg):
            self._set_btn.configure(text="Set Roster", fg_color=BTN_START[0], state="normal")
            self._err_lbl.configure(text=f"Push error: {msg[:80]}")

        threading.Thread(target=_propagate, daemon=True).start()


# ── File Attachment Mixin ─────────────────────────────────────────────────────

# ── Bridge ────────────────────────────────────────────────────────────────────

class BridgeWindow(_FileAttachMixin, ctk.CTkToplevel):
    """Agent-to-agent chat panel, using a Slack DM as transport."""

    CLAUDE_BIN = "/opt/homebrew/bin/claude"
    _SLACK_TOOLS = (
        "mcp__plugin_slack_slack__slack_read_channel,"
        "mcp__plugin_slack_slack__slack_send_message,"
        "mcp__plugin_slack_slack__slack_read_thread"
    )
    _SLACK_TOOLS_WITH_FS = (
        "mcp__plugin_slack_slack__slack_read_channel,"
        "mcp__plugin_slack_slack__slack_send_message,"
        "mcp__plugin_slack_slack__slack_read_thread,"
        "Bash,Edit,Write,Read"
    )
    _CDT         = timezone(timedelta(hours=-5))
    _SENT_FOOTER = re.compile(r'\n?\*?Sent using\*? <[^>]+>[^\n]*', re.IGNORECASE)

    @staticmethod
    def _load_bridge() -> dict:
        p = FLEET_DIR / "config.json"
        if p.exists():
            try:
                b = json.loads(p.read_text()).get("bridge", {})
                if b.get("mode") == "group" and b.get("channel") and b.get("self_uid") and b.get("peers"):
                    peers = b["peers"]
                    return {
                        "mode":       "group",
                        "channel":    b["channel"],
                        "self_uid":   b["self_uid"],
                        "self_name":  b.get("self_name",  AGENT_NAME),
                        "self_human": b.get("self_human", HUMAN_NAME),
                        "peers":      peers,
                        "peer_uid":   "",
                        "peer_name":  "Group (" + ", ".join(p["agent"] for p in peers) + ")",
                        "peer_human": "",
                    }
                if b.get("channel") and b.get("self_uid") and b.get("peer_uid"):
                    return {
                        "channel":    b["channel"],
                        "self_uid":   b["self_uid"],
                        "self_name":  b.get("self_name",  AGENT_NAME),
                        "self_human": b.get("self_human", HUMAN_NAME),
                        "peer_uid":   b["peer_uid"],
                        "peer_name":  b.get("peer_name",  "—"),
                        "peer_human": b.get("peer_human", "—"),
                    }
            except Exception:
                pass
        return {
            "channel":    "",
            "self_uid":   "",
            "self_name":  AGENT_NAME,
            "self_human": HUMAN_NAME,
            "peer_uid":   "",
            "peer_name":  "—",
            "peer_human": "—",
        }

    def _auto_prompt(self) -> str:
        b = self._bridge
        ch, su, sn = b["channel"], b["self_uid"], b["self_name"]
        if b.get("mode") == "group":
            peers = b.get("peers", [])
            peer_desc = "; ".join(f"{p['uid']} = {p['agent']}" for p in peers)
            peer_uids = {p["uid"] for p in peers}
            return (
                f"Check Slack group DM {ch} (limit 10). "
                f"Peer agents: {peer_desc}. {su} = {sn} (you). "
                "Rules — follow ALL of them:\n"
                f"1. ONLY respond to messages from peer agents ({', '.join(peer_uids)}). "
                f"Never respond to {sn}'s own messages ({su}).\n"
                f"2. Only reply if the most recent peer message has NO reply from {sn} ({su}) after it. "
                f"If {sn} has already replied after the last peer message, output exactly: NO_OP\n"
                f"3. If the channel's most recent message is from {sn} or a human, output exactly: NO_OP\n"
                f"4. If a reply is genuinely warranted: send a direct, substantive response to {ch}. "
                f"Sign with ' — {sn}'. Do NOT add 'Sent using Claude'.\n"
                "When in doubt, NO_OP. A missed reply is recoverable; a double-fire is not."
            )
        pu, pn = b["peer_uid"], b["peer_name"]
        return (
            f"Check Slack DM {ch} (limit 10). "
            f"{pu} = {pn} (peer agent). {su} = {sn} (you). "
            "Rules — follow ALL of them:\n"
            f"1. ONLY ever respond to messages from {pn} ({pu}). "
            f"Never respond to {sn}'s own messages ({su}).\n"
            f"2. Only reply if {pn}'s most recent message has NO reply from {sn} ({su}) after it. "
            f"If {sn} has already replied after {pn}'s last message, output exactly: NO_OP\n"
            f"3. If the channel's most recent message is from {sn} or a human, output exactly: NO_OP\n"
            f"4. If a reply is genuinely warranted: send a direct, substantive response to {ch}. "
            f"Sign with ' — {sn}'. Do NOT add 'Sent using Claude'.\n"
            "When in doubt, NO_OP. A missed reply is recoverable; a double-fire is not."
        )
    _MAX_COLLAB_EXCHANGES = 20

    @staticmethod
    def _parse_workdir(text: str) -> str:
        m = re.search(r'workdir=(\S+)', text)
        if m:
            p = Path(os.path.expanduser(m.group(1)))
            if p.exists():
                return str(p)
        return str(Path.home())

    def _make_collab_prompt(self, workdir: str) -> str:
        b = self._bridge
        ch, su, sn, pu, pn = b["channel"], b["self_uid"], b["self_name"], b["peer_uid"], b["peer_name"]
        return (
            f"Working directory: {workdir}\n"
            f"Check Slack DM {ch} (limit 20). "
            f"{pu} = {pn} (peer agent). {su} = {sn} (you). "
            "A collab session is active (::collab-task:: was posted). Rules:\n"
            "1. If '::task complete::' appears after the most recent '::collab-task::' sentinel, "
            "output exactly: TASK_COMPLETE\n"
            f"2. Find the most recent message from {pu} that ends with '— {pn}'. "
            f"If {sn} ({su}) has already replied after it, output exactly: NO_OP\n"
            "3. If a peer-signed message needs a reply: respond substantively. "
            f"You have full tool access — Bash, Edit, Write, Read. "
            f"Working directory is {workdir}. "
            "If the task requires building something, actually do it using your tools "
            "(create files, run git commands, scaffold repos, etc.), then report what you "
            f"did in the DM. Sign with ' — {sn}'. Do NOT add 'Sent using Claude'.\n"
            "4. Never reply to messages that are control signals "
            "(::collab-task::, ::task complete::, ::rocky-auto::).\n"
            "When in doubt, NO_OP."
        )

    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.geometry("720x560")
        self.minsize(500, 380)
        self.configure(fg_color=C_BG)
        self._bridge = self._load_bridge()
        sn = self._bridge.get("self_name", AGENT_NAME)
        pn = self._bridge.get("peer_name", "—")
        self.title(f"Bridge — {sn} ↔ {pn}")
        self._loading = False
        self._poll_stop = threading.Event()
        self._collab_stop = threading.Event()
        self._collab_armed = False
        self._collab_active = False
        self._collab_exchanges = 0
        self._collab_workdir = str(Path.home())
        self._auto_stop = threading.Event()
        self._auto_active = False
        self._opened_at = time.time()
        self._write_bridge_state(auto_active=False)  # clear stale flag on window open
        self._composing = False
        self._compose_id = None
        self._compose_frame = 0
        self._proc: subprocess.Popen | None = None
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        self._refresh_history()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._load_presence, daemon=True).start()

    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color=C_HEADER, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(
            hdr, text="Bridge", font=("SF Pro Display", 15, "bold")
        ).pack(side="left", padx=(14, 4), pady=10)
        _sn = self._bridge.get("self_name", AGENT_NAME)
        _pn = self._bridge.get("peer_name", "—")
        self._peer_lbl = ctk.CTkLabel(
            hdr, text=f"{_sn} ↔ {_pn}", font=("SF Pro Mono", 11),
            text_color=C_MUTED
        )
        self._peer_lbl.pack(side="left", pady=10)

        self._collab_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            hdr, text="Collab", font=("SF Pro Display", 12),
            variable=self._collab_var, onvalue=True, offvalue=False,
            command=self._on_collab_toggle,
            button_color="#1E6B3C", button_hover_color="#155230",
            progress_color="#1E6B3C",
        ).pack(side="right", padx=(0, 4), pady=10)

        self._auto_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            hdr, text="Auto", font=("SF Pro Display", 12),
            variable=self._auto_var, onvalue=True, offvalue=False,
            command=self._on_auto_toggle,
            button_color=C_BRAND, button_hover_color="#C41920",
            progress_color=C_BRAND,
        ).pack(side="right", padx=(0, 12), pady=10)

        self._refresh_btn = ctk.CTkButton(
            hdr, text="↺", width=32, height=28,
            font=("SF Pro Display", 15), corner_radius=6,
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._refresh_history
        )
        self._refresh_btn.pack(side="right", padx=(0, 4), pady=8)

        ctk.CTkButton(
            hdr, text="Pair", width=44, height=28,
            font=("SF Pro Mono", 11), corner_radius=6,
            fg_color="#1E3A5F", hover_color="#1D4ED8",
            command=self._open_pair
        ).pack(side="right", padx=(0, 4), pady=8)

        ctk.CTkButton(
            hdr, text="Logs", width=44, height=28,
            font=("SF Pro Mono", 11), corner_radius=6,
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_logs
        ).pack(side="right", padx=(0, 4), pady=8)

        self._status_bar = ctk.CTkFrame(self, fg_color=C_CARD, height=26)
        self._status_bar.pack(fill="x")
        self._status_bar.pack_propagate(False)
        self._status_lbl = ctk.CTkLabel(
            self._status_bar, text="Ready", font=("SF Pro Mono", 10),
            text_color=C_MUTED, anchor="w"
        )
        self._status_lbl.pack(side="left", padx=12, pady=4)
        self._auto_lbl = ctk.CTkLabel(
            self._status_bar, text="", font=("SF Pro Mono", 10, "bold"),
            text_color=C_GREEN, anchor="e"
        )
        self._auto_lbl.pack(side="right", padx=12, pady=4)

        self._collab_lbl = ctk.CTkLabel(
            self._status_bar, text="", font=("SF Pro Mono", 10, "bold"),
            text_color="#4ADE80", anchor="e"
        )
        self._collab_lbl.pack(side="right", padx=(0, 4), pady=4)

        self._presence_bar = ctk.CTkFrame(self, fg_color=C_CARD, height=26)
        self._presence_bar.pack(fill="x")
        self._presence_bar.pack_propagate(False)
        self._presence_scan_lbl = ctk.CTkLabel(
            self._presence_bar, text="Scanning #fleet-pairing…",
            font=("SF Pro Mono", 10), text_color=C_MUTED, anchor="w"
        )
        self._presence_scan_lbl.pack(side="left", padx=12, pady=4)

        self._history = ctk.CTkTextbox(
            self, font=("SF Pro Display", 12), wrap="word",
            fg_color=C_CARD, activate_scrollbars=True
        )
        self._history.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self._history.configure(state="disabled")

        self._compose_lbl = ctk.CTkLabel(
            self, text="", font=("SF Pro Mono", 11),
            text_color=C_MUTED, anchor="w"
        )
        self._compose_lbl.pack(fill="x", padx=14, pady=(2, 0))

        self._file_mixin_init()
        self._chips_frame = ctk.CTkFrame(self, fg_color="transparent")

        self._send_row = ctk.CTkFrame(self, fg_color="transparent")
        self._send_row.pack(fill="x", padx=8, pady=(0, 10))
        self._send_row.columnconfigure(0, weight=1)
        self._input_ref = self._send_row

        self._entry = ctk.CTkEntry(
            self._send_row, font=("SF Pro Display", 13), height=38,
            placeholder_text=f"Message {self._bridge.get('peer_name', '—')}…"
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._entry.bind("<Return>", lambda _e: self._send())
        self._entry.bind("<Command-v>", self._on_paste)
        self._register_drop_target(self._entry)
        self.bind("<Escape>", self._interrupt)

        ctk.CTkButton(
            self._send_row, text="⊕", width=38, height=38,
            font=("SF Pro Display", 16),
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_file_picker
        ).grid(row=0, column=1, padx=(0, 4))

        self._send_btn = ctk.CTkButton(
            self._send_row, text="Send", width=72, height=38,
            font=("SF Pro Display", 13, "bold"),
            fg_color=C_BRAND, hover_color="#C41920",
            command=self._send
        )
        self._send_btn.grid(row=0, column=2)

    def _start_composing(self):
        self._composing = True
        self._compose_frame = 0
        self._tick_composing()

    def _stop_composing(self):
        self._composing = False
        if self._compose_id:
            try:
                self.after_cancel(self._compose_id)
            except Exception:
                pass
            self._compose_id = None
        try:
            self._compose_lbl.configure(text="")
        except Exception:
            pass

    def _tick_composing(self):
        if not self._composing or not self.winfo_exists():
            return
        dots = "." * (self._compose_frame % 3 + 1)
        try:
            self._compose_lbl.configure(text=f"{self._bridge['peer_name']} is composing{dots}")
        except Exception:
            return
        self._compose_frame += 1
        self._compose_id = self.after(400, self._tick_composing)

    def _last_sender(self, text: str) -> str:
        pn = self._bridge["peer_name"]
        sn = self._bridge["self_name"]
        last_peer = text.rfind(f"{pn} [")
        last_self = text.rfind(f"{sn} [")
        if last_peer == -1 and last_self == -1:
            return ""
        return pn if last_peer > last_self else sn

    def _open_logs(self):
        if hasattr(self, "_logs_win") and self._logs_win.winfo_exists():
            self._logs_win.lift()
            return
        self._logs_win = LogViewer(self, "Bridge Log",
                                   lambda: LOGS_DIR / "bridge.log")

    def _set_status(self, text: str):
        self.after(0, lambda: self._status_lbl.configure(text=text))

    @staticmethod
    def _clean(text: str) -> str:
        lines = [l for l in text.splitlines() if not l.startswith("Permission allow rule")]
        return "\n".join(lines).strip()

    def _claude(self, prompt: str, on_result, timeout: int = 120,
                allowed_tools: str | None = None, files: list | None = None):
        def run():
            tools = allowed_tools if allowed_tools is not None else self._SLACK_TOOLS
            try:
                if files:
                    blocks = _build_content_blocks(files, prompt)
                    stdin_data = json.dumps({
                        "type": "user",
                        "message": {"role": "user", "content": blocks},
                    }) + "\n"
                    cmd = [self.CLAUDE_BIN, "--print", "--allowedTools", tools,
                           "--input-format", "stream-json",
                           "--output-format", "stream-json", "--verbose"]
                    r = subprocess.run(cmd, input=stdin_data,
                                       capture_output=True, text=True, timeout=timeout)
                    out = self._clean(_extract_stream_json_result(r.stdout)) or "(no output)"
                else:
                    cmd = [self.CLAUDE_BIN, "--print", "--allowedTools", tools]
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    )
                    self._proc = proc
                    all_lines = []
                    try:
                        proc.stdin.write(prompt)
                        proc.stdin.close()
                        for raw_line in proc.stdout:
                            line = ANSI_ESCAPE.sub("", raw_line).rstrip()
                            all_lines.append(line)
                            if line and not line.startswith("Permission allow rule"):
                                self.after(0, lambda l=line: self._append_activity(l))
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        all_lines = ["(timed out)"]
                    finally:
                        self._proc = None
                    out = self._clean("\n".join(all_lines)) or "(no output)"
            except FileNotFoundError:
                out = "(claude not found at /opt/homebrew/bin/claude)"
            except Exception as e:
                out = f"(error: {e})"
            self.after(0, lambda: on_result(out))
        threading.Thread(target=run, daemon=True).start()

    def _append_activity(self, line: str):
        self._history.configure(state="normal")
        self._history._textbox.insert("end", f"  {line}\n", "activity")
        self._history._textbox.tag_config(
            "activity", foreground="#4A4547", font=("SF Pro Mono", 10)
        )
        self._history.see("end")
        self._history.configure(state="disabled")

    def _interrupt(self, _event=None):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            self._proc = None
            self._set_status("Interrupted")
            self._entry.configure(state="normal")
            self._send_btn.configure(state="normal", text="Send")
            self._stop_composing()

    @staticmethod
    def _slack_token():
        p = FLEET_DIR / "secrets.json"
        if p.exists():
            try:
                return json.loads(p.read_text()).get("slack_token")
            except Exception:
                pass
        return None

    @staticmethod
    def _bot_token() -> str | None:
        """Bot token (xoxb-) for posting to channels like #fleet-pairing."""
        p = FLEET_DIR / "secrets.json"
        if p.exists():
            try:
                tok = json.loads(p.read_text()).get("slack_bot_token") or json.loads(p.read_text()).get("bot_token")
                if tok:
                    return tok
            except Exception:
                pass
        env = Path.home() / ".claude" / "monitor-state" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("SLACK_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
        return None

    def _upload_file_to_slack(self, fpath: str, channel: str, token: str) -> str | None:
        """Upload one file to a Slack DM channel via the external upload API.
        Returns an error string on failure, None on success.
        """
        try:
            p = Path(fpath)
            data = p.read_bytes()
            # Step 1: get an upload URL
            body = urllib.parse.urlencode({
                "filename": p.name,
                "length":   str(len(data)),
            }).encode()
            req = urllib.request.Request(
                "https://slack.com/api/files.getUploadURLExternal",
                data=body,
                headers={
                    "Authorization":  f"Bearer {token}",
                    "Content-Type":   "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                r = json.loads(resp.read())
            if not r.get("ok"):
                return r.get("error", "getUploadURLExternal failed")
            upload_url = r["upload_url"]
            file_id    = r["file_id"]
            # Step 2: push bytes to the returned URL
            req2 = urllib.request.Request(
                upload_url, data=data, method="POST",
                headers={"Content-Type": "application/octet-stream"},
            )
            with urllib.request.urlopen(req2, timeout=30, context=_SSL_CTX):
                pass
            # Step 3: complete the upload and share to the DM channel
            payload = json.dumps({
                "files":      [{"id": file_id, "title": p.name}],
                "channel_id": channel,
            }).encode()
            req3 = urllib.request.Request(
                "https://slack.com/api/files.completeUploadExternal",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
            )
            with urllib.request.urlopen(req3, timeout=15, context=_SSL_CTX) as resp:
                r3 = json.loads(resp.read())
            if not r3.get("ok"):
                return r3.get("error", "completeUploadExternal failed")
            return None
        except Exception as e:
            return str(e)

    def _upload_images_to_dm(self, files: list, channel: str):
        """Upload image files to the Slack DM channel (direct API, runs in a thread)."""
        token = BridgeWindow._slack_token()
        if not token:
            self._set_status("No Slack token — images not uploaded")
            return
        errors = []
        for fpath in files:
            err = self._upload_file_to_slack(fpath, channel, token)
            if err:
                errors.append(f"{Path(fpath).name}: {err}")
        if errors:
            self._set_status(f"Image upload error: {errors[0]}")
        self.after(500, self._refresh_history)

    def _load_presence(self) -> None:
        """Post startup heartbeat to #fleet-pairing, then populate the presence strip."""
        b = self._bridge
        bot_tok = BridgeWindow._bot_token()
        if bot_tok:
            text = (f"::fleet-presence:: agent={b['self_name']} "
                    f"human={b.get('self_human', 'Momo')} uid={b['self_uid']}")
            payload = json.dumps({"channel": FLEET_PAIRING_CHANNEL, "text": text}).encode()
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=payload,
                headers={"Authorization": f"Bearer {bot_tok}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                    json.loads(resp.read())
            except Exception:
                pass

        user_tok = BridgeWindow._slack_token()
        agents = []
        if user_tok:
            try:
                req = urllib.request.Request(
                    f"https://slack.com/api/conversations.history"
                    f"?channel={FLEET_PAIRING_CHANNEL}&limit=50",
                    headers={"Authorization": f"Bearer {user_tok}"},
                )
                with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                    data = json.loads(resp.read())
                if data.get("ok"):
                    now = time.time()
                    seen: set = set()
                    self_uid = b["self_uid"]
                    for m in data.get("messages", []):
                        txt = m.get("text", "")
                        if "::fleet-presence::" not in txt:
                            continue
                        uid_m = re.search(r'uid=(\S+)', txt)
                        if not uid_m:
                            continue
                        uid = uid_m.group(1)
                        if uid == self_uid or uid in seen:
                            continue
                        seen.add(uid)
                        am = re.search(r'agent=(\S+)', txt)
                        hm = re.search(r'human=(\S+)', txt)
                        if not (am and hm):
                            continue
                        agents.append({
                            "agent": am.group(1),
                            "human": hm.group(1),
                            "age":   now - float(m.get("ts", now)),
                        })
            except Exception:
                pass

        self.after(0, lambda: self._render_presence(agents))

    def _render_presence(self, agents: list[dict]) -> None:
        for w in self._presence_bar.winfo_children():
            w.destroy()
        if not agents:
            ctk.CTkLabel(
                self._presence_bar, text="No other agents online",
                font=("SF Pro Mono", 10), text_color=C_MUTED, anchor="w"
            ).pack(side="left", padx=12, pady=4)
            return
        ctk.CTkLabel(
            self._presence_bar, text="Online:",
            font=("SF Pro Mono", 10), text_color=C_MUTED, anchor="w"
        ).pack(side="left", padx=(12, 4), pady=4)
        for a in agents:
            age = a["age"]
            if age < 60:
                age_str = "just now"
            elif age < 3600:
                age_str = f"{int(age // 60)}m ago"
            elif age < 86400:
                age_str = f"{int(age // 3600)}h ago"
            else:
                age_str = f"{int(age // 86400)}d ago"
            dot_color = C_GREEN if age < 93600 else C_MUTED  # ~26h threshold
            pill = ctk.CTkFrame(self._presence_bar, fg_color=C_BORDER, corner_radius=4)
            pill.pack(side="left", padx=(0, 4), pady=3)
            ctk.CTkLabel(pill, text="●", font=("SF Pro Mono", 8),
                         text_color=dot_color).pack(side="left", padx=(6, 2), pady=2)
            ctk.CTkLabel(pill, text=f"{a['agent']}  {age_str}",
                         font=("SF Pro Mono", 10), text_color=C_MUTED).pack(side="left", padx=(0, 6), pady=2)

    def _refresh_history(self):
        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._set_status("Loading…")

        def fetch():
            try:
                token = BridgeWindow._slack_token()
                if not token:
                    self.after(0, lambda: self._on_history("(no Slack token — add slack_token to ~/.fleet/secrets.json)"))
                    return
                ch = self._bridge["channel"]
                user_names = {
                    self._bridge["peer_uid"]: self._bridge["peer_name"],
                    self._bridge["self_uid"]: self._bridge["self_name"],
                }
                req = urllib.request.Request(
                    f"https://slack.com/api/conversations.history"
                    f"?channel={ch}&limit=15",
                    headers={"Authorization": f"Bearer {token}"}
                )
                with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                    data = json.loads(resp.read())
                if not data.get("ok"):
                    raise ValueError(data.get("error", "unknown"))
                msgs = list(reversed(data["messages"]))
                lines = []
                for m in msgs:
                    uid  = m.get("user", "")
                    name = user_names.get(uid, uid)
                    dt   = datetime.fromtimestamp(float(m.get("ts", 0)), tz=self._CDT)
                    text = self._SENT_FOOTER.sub("", m.get("text", "")).strip()
                    for f in m.get("files", []):
                        fname = f.get("name") or f.get("title") or "file"
                        text = (text + "\n" if text else "") + f"[📎 {fname}]"
                    lines.append(f"{name} [{dt.strftime('%H:%M CDT')}]\n{text}\n")
                result = "\n".join(lines)
                self.after(0, lambda r=result: self._on_history(r))
                # Auto-signal detection — check if peer told us to start/stop auto-respond
                sn_lower = self._bridge.get("self_name", "").lower()
                sentinel = f"::{sn_lower}-auto"
                want_on = None
                for m in reversed(msgs):
                    if float(m.get("ts", 0)) < self._opened_at:
                        break
                    txt = m.get("text", "")
                    if sentinel in txt:
                        want_on = "state=on" in txt
                        break
                if want_on is not None:
                    self.after(0, lambda w=want_on: self._handle_auto_signal(w))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_history(f"(refresh error: {err})"))

        threading.Thread(target=fetch, daemon=True).start()

    def _on_history(self, text):
        self._history.configure(state="normal")
        self._history.delete("1.0", "end")
        self._history.insert("end", text)
        self._history.configure(state="disabled")
        self._history.see("end")
        self._loading = False
        self._refresh_btn.configure(state="normal")
        self._set_status("Ready")
        if self._composing and self._last_sender(text) == self._bridge["peer_name"]:
            self._stop_composing()

    def _send(self):
        msg = self._entry.get().strip()
        if not msg:
            return
        self._entry.delete(0, "end")
        self._entry.configure(state="disabled")
        self._send_btn.configure(state="disabled", text="…")
        self._set_status("Sending…")
        files = list(self._pending_files)
        self._pending_files.clear()
        self._rebuild_chips()
        _fleet_log(LOGS_DIR / "bridge.log", "YOU", msg)

        ch = self._bridge["channel"]
        sn = self._bridge["self_name"]

        if self._collab_armed and not self._collab_active:
            self._collab_workdir = self._parse_workdir(msg)
            sentinel = f"::collab-task:: {msg} — {sn}"
            collab_prompt = (
                f'Send this exact message to Slack DM channel {ch}: '
                f'"{sentinel}"\n'
                "Do not add 'Sent using Claude' — it is appended automatically."
            )
            def on_collab_started(_text):
                self._collab_active = True
                self._collab_exchanges = 0
                self._collab_stop.clear()
                self._collab_lbl.configure(text="⚡ COLLAB ACTIVE")
                self._status_bar.configure(fg_color="#0D2B1A")
                self._set_status("Collab session running…")
                self._refresh_history()
                threading.Thread(target=self._collab_loop, daemon=True).start()
            self._claude(collab_prompt, on_collab_started)
            return

        _IMG_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
        _SLACK_FILE_CHARS = 4_000

        text_files = [f for f in files if Path(f).suffix.lower() not in _IMG_EXTS]
        img_files  = [f for f in files if Path(f).suffix.lower() in _IMG_EXTS]

        # Build the full Slack message body — text files inlined as code blocks
        full_msg = f"{msg} — {sn}"
        for f in text_files:
            try:
                content = Path(f).read_text(errors="replace")
                if len(content) > _SLACK_FILE_CHARS:
                    content = content[:_SLACK_FILE_CHARS] + "\n…[truncated]"
                full_msg += f"\n\n```\n# {Path(f).name}\n{content}\n```"
            except Exception:
                full_msg += f"\n\n[{Path(f).name} — unreadable]"

        # Images upload directly to Slack in a background thread — no Claude needed
        if img_files:
            threading.Thread(
                target=self._upload_images_to_dm,
                args=(img_files, ch),
                daemon=True,
            ).start()

        prompt = (
            f"Send this exact message to Slack DM channel {ch}:\n"
            f"{full_msg}\n"
            "Do not add 'Sent using Claude' — it is appended automatically."
        )

        def on_result(_text):
            self._entry.configure(state="normal")
            self._send_btn.configure(state="normal", text="Send")
            self._entry.focus()
            self._start_composing()
            self._refresh_history()

        self._claude(prompt, on_result)

    def _on_auto_toggle(self):
        ch = self._bridge["channel"]
        is_group = self._bridge.get("mode") == "group"
        peers = self._bridge.get("peers", []) if is_group else [
            {"agent": self._bridge["peer_name"]}
        ]
        peer_label = self._bridge["peer_name"]  # already "Group (...)" or single name
        state = "on" if self._auto_var.get() else "off"
        _fleet_log(LOGS_DIR / "bridge.log", "AUTO", state)
        if state == "on":
            self._auto_lbl.configure(text=f"⚡ AUTO — signalling…")
            self._status_bar.configure(fg_color="#3B1010")
            self._send_btn.configure(state="disabled")
            def on_signal_done(out):
                self._send_btn.configure(state="normal")
                if "(timed out)" in out or "(error" in out:
                    self._auto_lbl.configure(text=f"⚠ AUTO signal failed: {out}")
                else:
                    self._auto_lbl.configure(text=f"⚡ AUTO ON — signal sent")
        else:
            self._auto_lbl.configure(text="")
            self._status_bar.configure(fg_color=C_CARD)
            on_signal_done = lambda _: None

        self._write_bridge_state(auto_active=(state == "on"))

        def _post_signals():
            out = "(no output)"
            for peer in peers:
                pn = peer["agent"]
                msg = f"::{pn.lower()}-auto state={state} from={HUMAN_NAME.lower()}::"
                prompt = (
                    f'Send this exact message to Slack channel {ch}: "{msg}"\n'
                    "Do not add 'Sent using Claude' — it is appended automatically."
                )
                try:
                    proc = subprocess.Popen(
                        [self.CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
                         "--allowedTools",
                         "mcp__plugin_slack_slack__slack_send_message"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                        start_new_session=True,
                    )
                    try:
                        stdout, _ = proc.communicate(input=prompt, timeout=30)
                        out = self._clean(stdout) or "(no output)"
                    except subprocess.TimeoutExpired:
                        import os, signal as _sig
                        os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                        proc.communicate()
                        out = "(timed out)"
                except Exception as e:
                    out = f"(error: {e})"
            self.after(0, lambda: on_signal_done(out))
        threading.Thread(target=_post_signals, daemon=True).start()

    @staticmethod
    def _write_bridge_state(armed: bool = None, auto_active: bool = None) -> None:
        try:
            p = Path.home() / ".fleet" / "bridge_state.json"
            state = {}
            if p.exists():
                try:
                    state = json.loads(p.read_text())
                except Exception:
                    pass
            if armed is not None:
                state["armed"] = armed
            if auto_active is not None:
                state["auto_active"] = auto_active
            p.write_text(json.dumps(state))
        except Exception:
            pass

    def _on_collab_toggle(self):
        pn = self._bridge["peer_name"]
        if self._collab_var.get():
            self._collab_armed = True
            self._write_bridge_state(True)
            self._collab_lbl.configure(text="⚡ COLLAB ARMED — type task and send")
            self._status_bar.configure(fg_color="#0D2B1A")
            self._entry.configure(placeholder_text=f"Describe a collab task for {pn}…")
            self._entry.focus()
        else:
            self._stop_collab()

    def _stop_collab(self):
        ch = self._bridge["channel"]
        sn = self._bridge["self_name"]
        was_active = self._collab_active
        self._collab_armed = False
        self._collab_active = False
        self._write_bridge_state(False)
        self._collab_stop.set()
        self._stop_composing()
        self._collab_var.set(False)
        self._collab_lbl.configure(text="")
        self._status_bar.configure(fg_color=C_CARD)
        self._entry.configure(state="normal", placeholder_text=f"Message {self._bridge['peer_name']}…")
        self._send_btn.configure(state="normal", text="Send")
        if was_active:
            close_prompt = (
                f'Send this exact message to Slack DM channel {ch}: '
                f'"::task complete:: — {sn}"\n'
                "Do not add 'Sent using Claude' — it is appended automatically."
            )
            self._claude(close_prompt, lambda _: self._refresh_history())

    def _open_pair(self):
        if hasattr(self, "_pair_win") and self._pair_win.winfo_exists():
            self._pair_win.lift()
            return
        self._pair_win = PairDialog(self, on_pair=self._on_paired)

    def _on_paired(self, bridge: dict):
        self._bridge = bridge
        sn = bridge.get("self_name", AGENT_NAME)
        if bridge.get("mode") == "group":
            peers = bridge.get("peers", [])
            pn = "Group (" + ", ".join(p["agent"] for p in peers) + ")"
            status_msg = f"Group with {pn} — restart daemon to apply"
        else:
            pn = bridge.get("peer_name", "—")
            status_msg = f"Paired with {pn} — restart daemon to apply"
        self.title(f"Bridge — {sn} ↔ {pn}")
        self._peer_lbl.configure(text=f"{sn} ↔ {pn}")
        self._set_status(status_msg)

    def _collab_loop(self):
        while not self._collab_stop.wait(60):
            if self._collab_exchanges >= self._MAX_COLLAB_EXCHANGES:
                self.after(0, self._stop_collab)
                break
            workdir = self._collab_workdir
            prompt = self._make_collab_prompt(workdir)
            try:
                r = subprocess.run(
                    [self.CLAUDE_BIN, "--print", "--allowedTools", self._SLACK_TOOLS_WITH_FS],
                    input=prompt,
                    capture_output=True, text=True, timeout=300,
                    cwd=workdir,
                )
                out = self._clean(r.stdout)
                if out == "TASK_COMPLETE":
                    self.after(0, self._stop_collab)
                    break
                elif out and out != "NO_OP":
                    self._collab_exchanges += 1
                    self.after(0, self._refresh_history)
            except Exception:
                pass

    def _handle_auto_signal(self, want_on: bool):
        if want_on and not self._auto_active:
            self._auto_active = True
            self._auto_var.set(True)
            self._auto_stop.clear()
            _fleet_log(LOGS_DIR / "bridge.log", "AUTO", "on")
            self._auto_lbl.configure(text="⚡ AUTO ON — peer signalled")
            self._status_bar.configure(fg_color="#3B1010")
            threading.Thread(target=self._auto_loop, daemon=True).start()
        elif not want_on and self._auto_active:
            self._auto_active = False
            self._auto_var.set(False)
            self._auto_stop.set()
            _fleet_log(LOGS_DIR / "bridge.log", "AUTO", "off")
            self._auto_lbl.configure(text="")
            self._status_bar.configure(fg_color=C_CARD)

    def _auto_loop(self):
        while not self._auto_stop.wait(30):
            prompt = self._auto_prompt()
            try:
                r = subprocess.run(
                    [self.CLAUDE_BIN, "--print", "--allowedTools", self._SLACK_TOOLS],
                    input=prompt,
                    capture_output=True, text=True, timeout=120,
                )
                out = self._clean(r.stdout)
                if out and out != "NO_OP":
                    self.after(0, self._refresh_history)
            except Exception:
                pass

    def _poll_loop(self):
        while not self._poll_stop.wait(5):
            self.after(0, self._refresh_history)

    def destroy(self):
        self._poll_stop.set()
        self._collab_stop.set()
        self._auto_stop.set()
        super().destroy()


# ── Pair Dialog ───────────────────────────────────────────────────────────────

class PairDialog(ctk.CTkToplevel):
    """Reads #fleet-pairing for active agents and writes bridge config on selection."""
    _PRESENCE_MAX_AGE = 900  # 15 minutes

    def __init__(self, parent, on_pair):
        super().__init__(parent)
        self.withdraw()
        self.title("Pair Agent")
        self.geometry("480x360")
        self.minsize(400, 280)
        self.configure(fg_color=C_BG)
        self._on_pair = on_pair
        self._agents: list[dict] = []
        self._selected_set: set[int] = set()
        self._row_frames: list = []
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        self.grab_set()
        threading.Thread(target=self._scan, daemon=True).start()

    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color=C_HEADER, height=40)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="Pair Agent", font=("SF Pro Display", 14, "bold")).pack(side="left", padx=14, pady=10)
        self._status_lbl = ctk.CTkLabel(hdr, text="Scanning #fleet-pairing…",
                                         font=("SF Pro Mono", 10), text_color=C_MUTED)
        self._status_lbl.pack(side="right", padx=14)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=C_CARD)
        self._scroll.pack(fill="both", expand=True, padx=8, pady=6)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkButton(foot, text="Cancel", width=80, height=32,
                      font=("SF Pro Display", 12),
                      fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
                      command=self.destroy).pack(side="right", padx=(4, 0))

        self._group_btn = ctk.CTkButton(foot, text="Group", width=80, height=32,
                                         font=("SF Pro Display", 12, "bold"),
                                         fg_color="#1E6B3C", hover_color="#155230",
                                         command=self._do_group, state="disabled")
        self._group_btn.pack(side="right", padx=(4, 0))

        self._pair_btn = ctk.CTkButton(foot, text="Pair", width=80, height=32,
                                        font=("SF Pro Display", 12, "bold"),
                                        fg_color=BTN_START[0], hover_color=BTN_START[1],
                                        command=self._do_pair, state="disabled")
        self._pair_btn.pack(side="right")

    def _scan(self):
        token = BridgeWindow._slack_token()
        if not token:
            self.after(0, lambda: self._status_lbl.configure(text="No Slack token"))
            return
        try:
            req = urllib.request.Request(
                f"https://slack.com/api/conversations.history"
                f"?channel={FLEET_PAIRING_CHANNEL}&limit=50",
                headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
            if not data.get("ok") and data.get("error") == "missing_scope":
                # User token lacks channels:history — retry with bot token
                bot = BridgeWindow._bot_token()
                if bot:
                    req = urllib.request.Request(
                        f"https://slack.com/api/conversations.history"
                        f"?channel={FLEET_PAIRING_CHANNEL}&limit=50",
                        headers={"Authorization": f"Bearer {bot}"}
                    )
                    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                        data = json.loads(resp.read())
            if not data.get("ok"):
                raise ValueError(data.get("error", "unknown"))
            now = time.time()
            seen_uids: set = set()
            agents = []
            for m in data.get("messages", []):
                text = m.get("text", "")
                if "::fleet-presence::" not in text:
                    continue
                ts = float(m.get("ts", 0))
                if now - ts > self._PRESENCE_MAX_AGE:
                    continue
                am = re.search(r'agent=(\S+)', text)
                hm = re.search(r'human=(\S+)', text)
                um = re.search(r'uid=(\S+)', text)
                if not (am and hm and um):
                    continue
                uid = um.group(1)
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                agents.append({
                    "agent": am.group(1), "human": hm.group(1),
                    "uid": uid, "ts": ts,
                })
            self.after(0, lambda a=agents: self._populate(a))
        except Exception as e:
            self.after(0, lambda err=str(e): self._status_lbl.configure(text=f"Error: {err}"))

    def _populate(self, agents: list):
        self._agents = agents
        for w in self._scroll.winfo_children():
            w.destroy()
        self._row_frames.clear()
        if not agents:
            self._status_lbl.configure(text="No agents online")
            ctk.CTkLabel(self._scroll,
                         text="No Fleet agents found in #fleet-pairing.\nMake sure the other agent's daemon is running.",
                         font=("SF Pro Display", 11), text_color=C_MUTED,
                         wraplength=400).pack(pady=20)
            return
        self._status_lbl.configure(text=f"{len(agents)} agent(s) — select one to Pair, two or more to Group")
        for i, a in enumerate(agents):
            row = ctk.CTkFrame(self._scroll, fg_color=C_BORDER, corner_radius=6)
            row.pack(fill="x", pady=2)
            self._row_frames.append(row)
            ago = int(time.time() - a["ts"])
            ago_str = f"{ago // 60}m ago" if ago >= 60 else "just now"
            ctk.CTkLabel(row, text=f"{a['agent']}  ({a['human']})",
                         font=("SF Pro Display", 13, "bold"), anchor="w").pack(side="left", padx=10, pady=8)
            ctk.CTkLabel(row, text=ago_str, font=("SF Pro Mono", 10),
                         text_color=C_MUTED, anchor="e").pack(side="right", padx=10)
            row.bind("<Button-1>", lambda _e, idx=i, r=row: self._select(idx, r))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda _e, idx=i, r=row: self._select(idx, r))

    def _select(self, idx: int, row: ctk.CTkFrame):
        if idx in self._selected_set:
            self._selected_set.discard(idx)
            row.configure(fg_color=C_BORDER)
        else:
            self._selected_set.add(idx)
            row.configure(fg_color="#1E3A5F")
        n = len(self._selected_set)
        self._pair_btn.configure(state="normal" if n == 1 else "disabled")
        self._group_btn.configure(state="normal" if n >= 2 else "disabled")

    def _do_pair(self):
        if len(self._selected_set) != 1:
            return
        agent = self._agents[next(iter(self._selected_set))]
        token = BridgeWindow._slack_token()
        if not token:
            return
        self._pair_btn.configure(state="disabled", text="Pairing…")

        def run():
            try:
                # Open DM to get channel ID; fall back to bot token on missing_scope
                def _open_dm(tok, uids):
                    payload = json.dumps({"users": uids}).encode()
                    req = urllib.request.Request(
                        "https://slack.com/api/conversations.open",
                        data=payload,
                        headers={"Authorization": f"Bearer {tok}",
                                 "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                        return json.loads(resp.read())
                dm_data = _open_dm(token, agent["uid"])
                if not dm_data.get("ok") and dm_data.get("error") == "missing_scope":
                    bot = BridgeWindow._bot_token()
                    if bot:
                        dm_data = _open_dm(bot, agent["uid"])
                if not dm_data.get("ok"):
                    raise ValueError(dm_data.get("error", "unknown"))
                channel_id = dm_data["channel"]["id"]

                # Get self UID via auth.test
                req2 = urllib.request.Request(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req2, timeout=10, context=_SSL_CTX) as resp2:
                    auth_data = json.loads(resp2.read())
                self_uid = auth_data.get("user_id", "")

                bridge = {
                    "channel":    channel_id,
                    "self_uid":   self_uid,
                    "self_name":  AGENT_NAME,
                    "self_human": HUMAN_NAME,
                    "peer_uid":   agent["uid"],
                    "peer_name":  agent["agent"],
                    "peer_human": agent["human"],
                }

                # Persist to config.json
                cfg_path = FLEET_DIR / "config.json"
                cfg: dict = {}
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                    except Exception:
                        pass
                cfg["bridge"] = bridge
                cfg_path.write_text(json.dumps(cfg, indent=2))

                # Announce to #fleet-pairing so peer's daemon self-configures
                # Use bot token (same as heartbeat) — user token lacks chat:write on channels
                try:
                    pair_text = (
                        f"::fleet-pair:: "
                        f"initiatorUID={self_uid} initiator={AGENT_NAME} initiatorHuman={HUMAN_NAME} "
                        f"peerUID={agent['uid']} peer={agent['agent']} peerHuman={agent['human']} "
                        f"channel={channel_id}"
                    )
                    pair_payload = json.dumps({"channel": FLEET_PAIRING_CHANNEL, "text": pair_text}).encode()
                    _announce_tok = BridgeWindow._bot_token() or token
                    pair_req = urllib.request.Request(
                        "https://slack.com/api/chat.postMessage",
                        data=pair_payload,
                        headers={"Authorization": f"Bearer {_announce_tok}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(pair_req, timeout=10, context=_SSL_CTX) as _r:
                        _rd = json.loads(_r.read())
                        if not _rd.get("ok"):
                            raise ValueError(_rd.get("error", "unknown"))
                except Exception as _e:
                    print(f"[bridge] pair announce failed: {_e}", flush=True)

                self.after(0, lambda b=bridge: self._on_pair(b))
                self.after(0, self.destroy)
            except Exception as e:
                self.after(0, lambda err=str(e): (
                    self._status_lbl.configure(text=f"Pair failed: {err}"),
                    self._pair_btn.configure(state="normal", text="Pair"),
                ))

        threading.Thread(target=run, daemon=True).start()

    def _do_group(self):
        if len(self._selected_set) < 2:
            return
        agents = [self._agents[i] for i in sorted(self._selected_set)]
        token = BridgeWindow._slack_token()
        if not token:
            return
        self._group_btn.configure(state="disabled", text="Opening…")

        def run():
            try:
                # Get self UID
                req_auth = urllib.request.Request(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req_auth, timeout=10, context=_SSL_CTX) as resp:
                    auth_data = json.loads(resp.read())
                self_uid = auth_data.get("user_id", "")

                # Open group DM; fall back to bot token on missing_scope
                all_uids = ",".join(a["uid"] for a in agents)
                def _open_group_dm(tok, uids):
                    payload = json.dumps({"users": uids}).encode()
                    req = urllib.request.Request(
                        "https://slack.com/api/conversations.open",
                        data=payload,
                        headers={"Authorization": f"Bearer {tok}",
                                 "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                        return json.loads(resp.read())
                dm_data = _open_group_dm(token, all_uids)
                if not dm_data.get("ok") and dm_data.get("error") == "missing_scope":
                    bot = BridgeWindow._bot_token()
                    if bot:
                        dm_data = _open_group_dm(bot, all_uids)
                if not dm_data.get("ok"):
                    raise ValueError(dm_data.get("error", "unknown"))
                channel_id = dm_data["channel"]["id"]

                bridge = {
                    "mode":       "group",
                    "channel":    channel_id,
                    "self_uid":   self_uid,
                    "self_name":  AGENT_NAME,
                    "self_human": HUMAN_NAME,
                    "peers":      [{"uid": a["uid"], "agent": a["agent"], "human": a["human"]} for a in agents],
                    "peer_uid":   "",
                    "peer_name":  "Group",
                    "peer_human": "",
                }

                cfg_path = FLEET_DIR / "config.json"
                cfg: dict = {}
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                    except Exception:
                        pass
                cfg["bridge"] = bridge
                cfg_path.write_text(json.dumps(cfg, indent=2))

                # Announce to #fleet-pairing — one message encoding all peers
                try:
                    peers_field = ",".join(
                        f"{a['uid']}:{a['agent']}:{a['human']}" for a in agents
                    )
                    pair_text = (
                        f"::fleet-pair:: "
                        f"initiatorUID={self_uid} initiator={AGENT_NAME} initiatorHuman={HUMAN_NAME} "
                        f"peers={peers_field} channel={channel_id} mode=group"
                    )
                    pair_payload = json.dumps({"channel": FLEET_PAIRING_CHANNEL, "text": pair_text}).encode()
                    _announce_tok = BridgeWindow._bot_token() or token
                    pair_req = urllib.request.Request(
                        "https://slack.com/api/chat.postMessage",
                        data=pair_payload,
                        headers={"Authorization": f"Bearer {_announce_tok}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(pair_req, timeout=10, context=_SSL_CTX) as _r:
                        _rd = json.loads(_r.read())
                        if not _rd.get("ok"):
                            raise ValueError(_rd.get("error", "unknown"))
                except Exception as _e:
                    print(f"[bridge] group pair announce failed: {_e}", flush=True)

                self.after(0, lambda b=bridge: self._on_pair(b))
                self.after(0, self.destroy)
            except Exception as e:
                self.after(0, lambda err=str(e): (
                    self._status_lbl.configure(text=f"Group failed: {err}"),
                    self._group_btn.configure(state="normal", text="Group"),
                ))

        threading.Thread(target=run, daemon=True).start()


# ── Sessions ──────────────────────────────────────────────────────────────────

class Session:
    _SOFT_THRESHOLD = 3  # T1: warn, keep session alive
    _HARD_THRESHOLD = 6  # T2: reset session UUID, start fresh
    # T3 stub: tab-level restart via SessionsWindow — not implemented;
    # add if T2 proves insufficient in prod

    def __init__(self, sid: int, root_after, resume_uuid: str | None = None):
        self.sid = sid
        self.name = f"Session {sid}"
        self._uuid = resume_uuid if resume_uuid else str(_uuid.uuid4())
        self._started = resume_uuid is not None  # True = start with --resume; False = need --session-id
        self._resumed = resume_uuid is not None
        self._busy = False
        self._dangerous = True   # default: skip permission prompts; GATE button enables gate
        self._root_after = root_after
        self._fail_streak = 0
        self.total_in = 0
        self.total_out = 0
        self.total_cost = 0.0
        self._proc: subprocess.Popen | None = None
        self._auto_named = False
        self.name_changed_cb = None

    def terminate(self):
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._proc = None
        self._busy = False

    def send(self, msg: str, on_chunk, on_done, on_error, cwd: str | None = None, files: list | None = None, on_tool: object = None, on_activity: object = None):
        if self._busy:
            return
        self._busy = True

        work_dir     = os.path.expanduser(cwd.replace(" (home)", "")) if cwd else str(Path.home())
        mcp_cfg_path = FLEET_DIR / f"mcp_cfg_{self._uuid}.json"
        _mem_running = _fleet_memory is not None and _fleet_memory.is_running()
        _mem_cfg = FLEET_DIR / "memory_mcp.json"

        if self._dangerous:
            cmd = [
                "/opt/homebrew/bin/claude", "--print",
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
            ]
            if _mem_running and _mem_cfg.exists():
                cmd += ["--mcp-config", str(_mem_cfg)]
        else:
            # Proxy tools gate Bash/Edit/Write through Fleet UI; disallow built-ins
            mcp_servers = {
                "fleet": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(FLEET_DIR / "fleet_mcp_gate.py"), work_dir],
                },
                "plugin_slack_slack": {
                    "type": "http",
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {
                        "clientId": "1601185624273.8899143856786",
                        "callbackPort": 3118,
                    },
                },
            }
            if _mem_running:
                mcp_servers["fleet_memory"] = {
                    "type": "http",
                    "url": "http://127.0.0.1:{}/mcp".format(_fleet_memory.PORT),
                }
            mcp_cfg_path.write_text(json.dumps({"mcpServers": mcp_servers}))

            _mem_tools = (
                ",mcp__fleet_memory__memory_search"
                ",mcp__fleet_memory__memory_write"
                ",mcp__fleet_memory__memory_delete"
            ) if _mem_running else ""
            cmd = [
                "/opt/homebrew/bin/claude", "--print",
                "--output-format", "stream-json", "--verbose",
                "--disallowedTools", "Bash,Edit,Write,NotebookEdit",
                "--allowedTools", (
                    "mcp__fleet__fleet_bash,mcp__fleet__fleet_edit,mcp__fleet__fleet_write,"
                    "mcp__plugin_slack_slack__slack_send_message,"
                    "mcp__plugin_slack_slack__slack_read_channel"
                    + _mem_tools
                ),
                "--mcp-config", str(mcp_cfg_path),
            ]
        if not self._started:
            cmd += ["--session-id", self._uuid, "--name", self.name]
            self._started = True
        else:
            cmd += ["--resume", self._uuid]

        stdin_data = None
        if files:
            cmd += ["--input-format", "stream-json"]
            blocks = _build_content_blocks(files, msg)
            stdin_data = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": blocks},
            }) + "\n"
        else:
            cmd += ["--", msg]

        def run():
            _dbg = open(FLEET_DIR / "session_debug.log", "a", buffering=1)
            _dbg.write(f"\n--- run() start dangerous={self._dangerous} cmd={cmd}\n")
            accumulated = ""
            success = False
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE if stdin_data else None,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, cwd=work_dir
                )
                self._proc = proc
                if stdin_data:
                    def _write_stdin():
                        try:
                            proc.stdin.write(stdin_data)
                            proc.stdin.close()
                        except Exception:
                            pass
                    threading.Thread(target=_write_stdin, daemon=True).start()
                for raw in proc.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    _dbg.write(f"  event type={t}\n")
                    if t == "assistant":
                        content = obj.get("message", {}).get("content", [])
                        text = "".join(
                            b.get("text", "")
                            for b in content
                            if b.get("type") == "text"
                        )
                        _dbg.write(f"  assistant text len={len(text)}\n")
                        if on_tool:
                            tool_names = [b.get("name") for b in content if b.get("type") == "tool_use" and b.get("name")]
                            if tool_names:
                                self._root_after(0, lambda n=tool_names[0]: on_tool(n))
                        if on_activity:
                            for b in content:
                                if b.get("type") == "tool_use" and b.get("name"):
                                    tname = b["name"]
                                    inp = b.get("input", {})
                                    first_val = str(next(iter(inp.values()), ""))[:60] if inp else ""
                                    label = f"⚙ {tname}({first_val})" if first_val else f"⚙ {tname}"
                                    self._root_after(0, lambda l=label: on_activity(l))
                        if text and len(text) > len(accumulated):
                            delta = text[len(accumulated):]
                            accumulated = text
                            self._root_after(0, lambda d=delta: on_chunk(d))
                            success = True
                    elif t == "result":
                        final = obj.get("result", "")
                        if final and len(final) > len(accumulated):
                            delta = final[len(accumulated):]
                            accumulated = final
                            self._root_after(0, lambda d=delta: on_chunk(d))
                        usage = obj.get("usage", {})
                        self.total_in += usage.get("input_tokens", 0)
                        self.total_out += usage.get("output_tokens", 0)
                        cost = obj.get("cost_usd")
                        if cost:
                            self.total_cost += cost
                        success = True
                        break
                _dbg.write(f"  loop exited success={success} accumulated={bool(accumulated)}\n")
                proc.stdout.close()
                threading.Thread(target=proc.wait, daemon=True).start()
                if not accumulated:
                    err = ANSI_ESCAPE.sub("", proc.stderr.read()).strip()
                    if "too long" in err.lower() or "context" in err.lower():
                        err = "Prompt too long — session context is full. Start a new session (+ button) or use smaller files."
                    self._root_after(0, lambda e=err or "(no response)": on_chunk(e))
            except Exception as e:
                _dbg.write(f"  EXCEPTION: {e}\n")
                self._root_after(0, lambda err=str(e): on_error(err))
            finally:
                self._proc = None
                if not self._dangerous:
                    try:
                        mcp_cfg_path.unlink()
                    except OSError:
                        pass
                if success:
                    self._fail_streak = 0
                else:
                    self._fail_streak += 1
                    if self._fail_streak == self._SOFT_THRESHOLD:
                        self._root_after(0, lambda: on_chunk(
                            f"\n[!] Watchdog T1: {self._SOFT_THRESHOLD} consecutive failures "
                            "— Slack API may be degraded, retrying with current session…\n"
                        ))
                    elif self._fail_streak >= self._HARD_THRESHOLD:
                        self._fail_streak = 0
                        self._uuid = str(_uuid.uuid4())
                        self._started = False
                        self._root_after(0, lambda: on_chunk(
                            f"\n[!!] Watchdog T2: {self._HARD_THRESHOLD} consecutive failures "
                            "— session reset, next send starts fresh.\n"
                        ))
                self._busy = False
                _dbg.write(f"  finally done, _busy=False\n")
                _dbg.close()
                self._root_after(0, on_done)

        threading.Thread(target=run, daemon=True).start()


class ResumeDialog(ctk.CTkToplevel):
    _MAX_SESSIONS = 15

    def __init__(self, parent, on_resume, project_dir: str | None = None):
        super().__init__(parent)
        self.withdraw()
        self.title("Resume Session")
        self.geometry("640x460")
        self.minsize(480, 300)
        self.configure(fg_color=C_BG)
        self._on_resume = on_resume
        self._project_dir = project_dir
        self._selected_uuid: str | None = None
        self._rows: list[ctk.CTkFrame] = []
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        threading.Thread(target=self._scan, daemon=True).start()

    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color=C_HEADER, height=40)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(
            hdr, text="Resume Session", font=("SF Pro Display", 14, "bold")
        ).pack(side="left", padx=14, pady=10)

        self._status_lbl = ctk.CTkLabel(
            hdr, text="Scanning…", font=("SF Pro Mono", 10),
            text_color=C_MUTED
        )
        self._status_lbl.pack(side="right", padx=14, pady=10)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=C_CARD)
        self._scroll.pack(fill="both", expand=True, padx=8, pady=6)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=8, pady=(0, 8))

        self._uuid_entry = ctk.CTkEntry(
            foot, font=("SF Pro Mono", 11), height=32,
            placeholder_text="or paste UUID…"
        )
        self._uuid_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self._resume_btn = ctk.CTkButton(
            foot, text="↺ Resume", width=90, height=32,
            font=("SF Pro Display", 12, "bold"),
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._do_resume, state="disabled"
        )
        self._resume_btn.pack(side="left")

    def _scan(self):
        projects_root = Path.home() / ".claude" / "projects"
        sessions = []
        search_root = Path(self._project_dir) if self._project_dir else projects_root
        if not search_root.exists():
            self.after(0, lambda: self._status_lbl.configure(text="No sessions found"))
            return
        try:
            jsonls = sorted(
                search_root.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )[:50]
        except Exception:
            self.after(0, lambda: self._status_lbl.configure(text="Scan error"))
            return
        for jsonl in jsonls:
            try:
                mtime = jsonl.stat().st_mtime
                uid = jsonl.stem
                if len(uid) != 36:
                    continue
                preview = ""
                with open(jsonl, errors="replace") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "user":
                            content = obj.get("message", {}).get("content", "")
                            if isinstance(content, list):
                                for blk in content:
                                    if blk.get("type") == "text":
                                        preview = blk.get("text", "")[:80]
                                        break
                            elif isinstance(content, str):
                                preview = content[:80]
                            if preview:
                                break
                ago = self._rel_time(mtime)
                proj = jsonl.parent.name
                sessions.append((uid, preview or "(no preview)", ago, proj))
                if len(sessions) >= self._MAX_SESSIONS:
                    break
            except Exception:
                continue
        self.after(0, lambda s=sessions: self._populate(s))

    @staticmethod
    def _rel_time(ts: float) -> str:
        delta = int(time.time() - ts)
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _populate(self, sessions: list):
        for w in self._rows:
            w.destroy()
        self._rows.clear()
        self._selected_uuid = None
        if not sessions:
            self._status_lbl.configure(text="No sessions found")
            return
        self._status_lbl.configure(text=f"{len(sessions)} sessions")
        for uid, preview, ago, proj in sessions:
            row = ctk.CTkFrame(self._scroll, fg_color=C_BORDER, corner_radius=6)
            row.pack(fill="x", pady=2)
            self._rows.append(row)
            top = ctk.CTkFrame(row, fg_color="transparent")
            top.pack(fill="x", padx=8, pady=(6, 2))
            ctk.CTkLabel(
                top, text=uid, font=("SF Pro Mono", 10), text_color=C_MUTED,
                anchor="w"
            ).pack(side="left")
            ctk.CTkLabel(
                top, text=ago, font=("SF Pro Mono", 10), text_color=C_MUTED,
                anchor="e"
            ).pack(side="right")
            ctk.CTkLabel(
                row, text=preview, font=("SF Pro Display", 11),
                anchor="w", wraplength=560
            ).pack(fill="x", padx=8, pady=(0, 6))
            row.bind("<Button-1>", lambda _e, u=uid, r=row: self._select(u, r))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda _e, u=uid, r=row: self._select(u, r))
                for grandchild in child.winfo_children():
                    grandchild.bind("<Button-1>", lambda _e, u=uid, r=row: self._select(u, r))

    def _select(self, uid: str, row: ctk.CTkFrame):
        for r in self._rows:
            r.configure(fg_color=C_BORDER)
        row.configure(fg_color="#3A1D1F")
        self._selected_uuid = uid
        self._resume_btn.configure(state="normal")

    def _do_resume(self):
        uid = self._uuid_entry.get().strip() or self._selected_uuid
        if uid:
            self._on_resume(uid)
            self.destroy()


class SessionPane(_FileAttachMixin, ctk.CTkFrame):
    def __init__(self, parent, session: Session, on_status_change, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.session = session
        self.on_status_change = on_status_change
        date_str = datetime.now().strftime("%Y-%m-%d")
        self._log_path = LOGS_DIR / f"session_{date_str}_{session.sid}.log"
        self._log_buf = []
        self._auto_summarizing = False
        self._tools_this_turn = 0
        self._first_msg: str | None = None
        self._build()
        _fleet_log(self._log_path, "SESSION_START",
                   f"sid={session.sid} uuid={session._uuid} resumed={session._resumed}")
        if session._resumed:
            self._append(f"↺ Resumed from {session._uuid}\n\n")

    _DIR_OPTIONS = [
        "~/.fleet",
        "~ (home)",
        "~/.claude",
    ]

    def _build(self):
        # Directory picker
        dir_row = ctk.CTkFrame(self, fg_color=C_HEADER, height=36)
        dir_row.pack(fill="x", pady=(0, 2))
        dir_row.pack_propagate(False)

        ctk.CTkLabel(
            dir_row, text="Dir:", font=("SF Pro Mono", 11),
            text_color=C_MUTED, width=30
        ).pack(side="left", padx=(10, 4))

        # Gate button packed before dir_seg (side=right before expand=True left)
        # Default: dangerous=True (no gate). Button enables gate when clicked.
        self._gate_btn = ctk.CTkButton(
            dir_row, text="GATE", width=48, height=24,
            font=("SF Pro Mono", 10),
            fg_color=C_CARD, hover_color=C_BORDER,
            command=self._toggle_danger,
        )
        self._gate_btn.pack(side="right", padx=(0, 8), pady=6)

        self._dir_seg = ctk.CTkSegmentedButton(
            dir_row, values=self._DIR_OPTIONS,
            font=("SF Pro Mono", 10), height=24,
            fg_color=C_CARD, selected_color=C_BRAND,
            selected_hover_color="#C41920",
            unselected_color=C_CARD, unselected_hover_color=C_BORDER,
            border_width=1,
            text_color=("#FFFFFF", "#FFFFFF"),
        )
        self._dir_seg.set("~/.fleet")
        self._dir_seg.pack(side="left", padx=(0, 6), pady=6, fill="x", expand=True)

        # Status bar
        status_row = ctk.CTkFrame(self, fg_color=C_CARD, height=30)
        status_row.pack(fill="x", pady=(0, 4))
        status_row.pack_propagate(False)

        self._dot = ctk.CTkLabel(
            status_row, text="●", font=("SF Pro Display", 13),
            text_color=C_GRAY, width=18
        )
        self._dot.pack(side="left", padx=(10, 4))

        self._status_lbl = ctk.CTkLabel(
            status_row, text="Idle", font=("SF Pro Mono", 11),
            text_color=C_MUTED, anchor="w"
        )
        self._status_lbl.pack(side="left")

        self._usage_lbl = ctk.CTkLabel(
            status_row, text="", font=("SF Pro Mono", 10),
            text_color=C_MUTED, anchor="e"
        )
        self._usage_lbl.pack(side="right", padx=10)

        ctk.CTkLabel(
            status_row, text="⚡ workbench",
            font=("SF Pro Mono", 10), text_color="#FF4040"
        ).pack(side="right", padx=(10, 0))

        self._output = ctk.CTkTextbox(
            self, font=("SF Pro Mono", 11), wrap="word",
            fg_color=C_LOG_BG, activate_scrollbars=True
        )
        self._output.pack(fill="both", expand=True, pady=(0, 6))
        self._output.configure(state="disabled")

        self._file_mixin_init()
        self._chips_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._input_row = ctk.CTkFrame(self, fg_color="transparent")
        self._input_row.pack(fill="x")
        self._input_row.columnconfigure(0, weight=1)
        self._input_ref = self._input_row

        self._entry = ctk.CTkEntry(
            self._input_row, font=("SF Pro Display", 13), height=38,
            placeholder_text=f"Describe a task for {AGENT_NAME}…"
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._entry.bind("<Return>", lambda _e: self._send())
        self._entry.bind("<Command-v>", self._on_paste)
        self._register_drop_target(self._entry)

        ctk.CTkButton(
            self._input_row, text="⊕", width=38, height=38,
            font=("SF Pro Display", 16),
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_file_picker
        ).grid(row=0, column=1, padx=(0, 4))

        self._run_btn = ctk.CTkButton(
            self._input_row, text="Run", width=72, height=38,
            font=("SF Pro Display", 13, "bold"),
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._send
        )
        self._run_btn.grid(row=0, column=2)

    def _toggle_danger(self):
        self.session._dangerous = not self.session._dangerous
        if self.session._dangerous:
            # dangerous=True: gate off, button dimmed
            self._gate_btn.configure(
                text="GATE", fg_color=C_CARD, hover_color=C_BORDER
            )
        else:
            # dangerous=False: gate on, button lit red
            self._gate_btn.configure(
                text="GATED", fg_color="#8B0000", hover_color="#A00000"
            )

    _SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _start_spinner(self):
        self._spinning = True
        self._spin_step = 0
        self._output.configure(state="normal")
        self._output.insert("end", self._SPINNER[0])
        self._output.configure(state="disabled")
        self._spin_id = self.after(100, self._tick_spinner)

    def _tick_spinner(self):
        if not getattr(self, "_spinning", False):
            return
        self._spin_step = (self._spin_step + 1) % len(self._SPINNER)
        self._output.configure(state="normal")
        self._output.delete("end-2c", "end-1c")
        self._output.insert("end-1c", self._SPINNER[self._spin_step])
        self._output.configure(state="disabled")
        self._output.see("end")
        self._spin_id = self.after(100, self._tick_spinner)

    def _stop_spinner(self):
        if not getattr(self, "_spinning", False):
            return
        self._spinning = False
        if hasattr(self, "_spin_id"):
            self.after_cancel(self._spin_id)
        self._output.configure(state="normal")
        self._output.delete("end-2c", "end-1c")
        self._output.configure(state="disabled")

    def _append(self, text: str):
        self._stop_spinner()
        self._output.configure(state="normal")
        self._output.insert("end", text)
        self._output.configure(state="disabled")
        self._output.see("end")

    def _append_activity(self, line: str):
        self._output.configure(state="normal")
        self._output._textbox.insert("end", f"  {line}\n", "activity")
        self._output._textbox.tag_config(
            "activity", foreground="#4A4547", font=("SF Pro Mono", 10)
        )
        self._output.see("end")
        self._output.configure(state="disabled")

    def _interrupt(self):
        self.session.terminate()
        self._stop_spinner()
        self._append("\n[interrupted]\n")
        self._dot.configure(text_color=C_GRAY)
        self._status_lbl.configure(text="Interrupted")
        self._run_btn.configure(state="normal", text="Run")
        self._entry.configure(state="normal")
        self._entry.focus()

    def _do_rename(self, new_name: str):
        self.session.name = new_name
        if self.session.name_changed_cb:
            self.session.name_changed_cb(new_name)

    def _send(self):
        if self.session._busy:
            return
        msg = self._entry.get().strip()
        if not msg:
            return
        if msg.startswith("/rename "):
            new_name = msg[8:].strip()
            if new_name:
                self._entry.delete(0, "end")
                self._do_rename(new_name)
            return
        if self._first_msg is None:
            self._first_msg = msg
        self._entry.delete(0, "end")
        self._entry.configure(state="disabled")
        self._run_btn.configure(state="disabled", text="…")
        self._dot.configure(text_color=C_YELLOW)
        self._status_lbl.configure(text="Running…")
        self.on_status_change()
        files = list(self._pending_files)
        self._pending_files.clear()
        self._rebuild_chips()
        suffix = f"  +{len(files)} file(s)" if files else ""
        self._append(f"\nYou:  {msg}{suffix}\n\n{AGENT_NAME}:  ")
        self._start_spinner()
        _fleet_log(self._log_path, "YOU", msg + suffix)
        self._log_buf = []
        self._auto_summarizing = False
        self._tools_this_turn = 0

        def _chunked(text):
            self._log_buf.append(text)
            self._append(text)

        def _on_tool(name):
            self._tools_this_turn += 1
            self._status_lbl.configure(text=f"↻ {name}…")

        def _on_activity(line):
            self._append_activity(line)

        self.session.send(
            msg,
            on_chunk=_chunked,
            on_done=self._on_done,
            on_error=lambda e: (_fleet_log(self._log_path, "ERROR", e),
                                self._append(f"\nError: {e}")),
            on_tool=_on_tool,
            on_activity=_on_activity,
            cwd=self._dir_seg.get(),
            files=files,
        )

    def _on_done(self):
        response = "".join(self._log_buf).strip()
        if response and not self._auto_summarizing:
            _fleet_log(self._log_path, "APOLLO", response)
        self._log_buf = []

        def _try(fn):
            try:
                fn()
            except Exception:
                pass

        if len(response) < _AUTO_SUMMARY_THRESHOLD and not self._auto_summarizing and self._tools_this_turn > 0:
            self._auto_summarizing = True
            _try(self._stop_spinner)
            _try(lambda: self._append("\nrecap:  "))
            _try(self._start_spinner)
            _try(lambda: self._status_lbl.configure(text="Summarizing…"))
            self.session.send(
                _AUTO_SUMMARY_PROMPT,
                on_chunk=lambda t: (self._log_buf.append(t), self._append(t)),
                on_done=self._on_done,
                on_error=lambda e: self._append(f"\nError: {e}"),
                on_tool=None,
                on_activity=None,
                cwd=self._dir_seg.get(),
            )
            return

        self._auto_summarizing = False
        _try(self._stop_spinner)
        _try(lambda: self._append("\n\n" + "─" * 64 + "\n"))
        _try(lambda: self._dot.configure(text_color=C_GREEN))
        _try(lambda: self._status_lbl.configure(text="Done"))
        _try(lambda: self._run_btn.configure(state="normal", text="Run"))
        _try(lambda: self._entry.configure(state="normal"))
        _try(self._entry.focus)
        s = self.session
        if s.total_in or s.total_out:
            parts = [f"in:{s.total_in:,} out:{s.total_out:,}"]
            if s.total_cost:
                parts.append(f"${s.total_cost:.4f}")
            _try(lambda: self._usage_lbl.configure(text="  ".join(parts)))
        _try(self.on_status_change)
        if not s._auto_named and self._first_msg:
            s._auto_named = True
            threading.Thread(target=self._auto_name_bg, args=(self._first_msg,), daemon=True).start()

    def _auto_name_bg(self, first_msg: str):
        try:
            result = subprocess.run(
                ["/opt/homebrew/bin/claude", "--print", "--dangerously-skip-permissions",
                 f"Give this chat a short title: 3-5 words, no quotes, no punctuation, no trailing period. "
                 f"First message: {first_msg[:300]}"],
                capture_output=True, text=True, timeout=20,
            )
            name = result.stdout.strip().splitlines()[0].strip()[:50]
            if name and not name.lower().startswith("i "):
                self.session.name = name
                if self.session.name_changed_cb:
                    self.session._root_after(0, lambda n=name: self.session.name_changed_cb(n))
        except Exception:
            pass

    def focus_input(self):
        self._entry.focus()


class PermissionDialog(ctk.CTkToplevel):
    def __init__(self, parent, req: dict, on_done):
        super().__init__(parent)
        self.withdraw()
        self.title("Permission Request")
        self.geometry("540x340")
        self.resizable(False, False)
        self.configure(fg_color=C_BG)
        self._req = req
        self._on_done = on_done
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        self.grab_set()

    def _build(self):
        tool_name  = self._req.get("tool_name", "?")
        input_data = self._req.get("input", {})
        cwd        = self._req.get("cwd", "")

        hdr = ctk.CTkFrame(self, fg_color=C_HEADER, height=42)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="⚠  Permission Request",
                     font=("SF Pro Display", 14, "bold"),
                     text_color="#FFFFFF").pack(side="left", padx=14, pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=10)

        ctk.CTkLabel(body, text="Tool", font=("SF Pro Mono", 10),
                     text_color=C_MUTED, anchor="w").pack(anchor="w")
        ctk.CTkLabel(body, text=tool_name,
                     font=("SF Pro Display", 16, "bold"),
                     text_color=C_BRAND, anchor="w").pack(anchor="w")

        ctk.CTkLabel(body, text="Input", font=("SF Pro Mono", 10),
                     text_color=C_MUTED, anchor="w").pack(anchor="w", pady=(10, 0))
        input_str = json.dumps(input_data, indent=2)
        if len(input_str) > 400:
            input_str = input_str[:400] + "\n…"
        box = ctk.CTkTextbox(body, font=("SF Pro Mono", 10),
                             fg_color=C_CARD, height=110)
        box.pack(fill="x")
        box.insert("end", input_str)
        box.configure(state="disabled")

        ctk.CTkLabel(body, text=f"cwd: {cwd}", font=("SF Pro Mono", 10),
                     text_color=C_MUTED, anchor="w").pack(anchor="w", pady=(6, 0))

        btn_row = ctk.CTkFrame(self, fg_color=C_HEADER, height=52)
        btn_row.pack(fill="x", side="bottom")
        btn_row.pack_propagate(False)
        ctk.CTkButton(btn_row, text="Deny", width=100, height=36,
                      font=("SF Pro Display", 13, "bold"),
                      fg_color=BTN_STOP[0], hover_color=BTN_STOP[1],
                      command=self._deny).pack(side="right", padx=8, pady=8)
        ctk.CTkButton(btn_row, text="Allow", width=100, height=36,
                      font=("SF Pro Display", 13, "bold"),
                      fg_color=BTN_START[0], hover_color=BTN_START[1],
                      command=self._allow).pack(side="right", padx=(0, 4), pady=8)

    def _allow(self): self._respond("allow", "Approved by user")
    def _deny(self):  self._respond("deny", "Denied by user")

    def _respond(self, behavior: str, message: str):
        pid = self._req.get("pid", 0)
        req_id = self._req.get("request_id", "")
        resp_path = FLEET_DIR / f"permission_response_{pid}.json"
        resp_path.write_text(json.dumps({
            "request_id": req_id,
            "behavior": behavior,
            "message": message,
        }))
        self._on_done()
        self.destroy()


class LogViewer(ctk.CTkToplevel):
    """Generic single-log viewer. get_path() → Path | None, called on each refresh."""
    _REFRESH_MS = 2000

    def __init__(self, parent, title: str, get_path):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.geometry("720x540")
        self.configure(fg_color=C_BG)
        self._get_path = get_path
        self._after_id = None
        self._build()
        self.after(50, lambda: _center_on_parent(self, parent))
        self._refresh()

    def _build(self):
        self._path_lbl = ctk.CTkLabel(
            self, text="", font=("SF Pro Mono", 10),
            text_color=C_MUTED, anchor="w"
        )
        self._path_lbl.pack(fill="x", padx=12, pady=(8, 0))
        self._box = ctk.CTkTextbox(
            self, font=("SF Pro Mono", 11),
            fg_color=C_LOG_BG, wrap="none", activate_scrollbars=True
        )
        self._box.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self._box.configure(state="disabled")

    def _refresh(self):
        if not self.winfo_exists():
            return
        path = self._get_path()
        if path:
            self._path_lbl.configure(text=str(path))
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = "(no log yet)"
        else:
            self._path_lbl.configure(text="(no log)")
            content = "(no active session)"
        self._box.configure(state="normal")
        self._box.delete("1.0", "end")
        self._box.insert("end", content)
        self._box.configure(state="disabled")
        self._box.see("end")
        self._after_id = self.after(self._REFRESH_MS, self._refresh)

    def destroy(self):
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        super().destroy()


class SessionsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title("Sessions")
        self.geometry("780x620")
        self.minsize(560, 420)
        self.configure(fg_color=C_BG)
        self._sessions: list[Session] = []
        self._active_permission_pids: set[str] = set()
        self._panes: dict[int, SessionPane] = {}
        self._tab_frames: dict[int, ctk.CTkFrame] = {}
        self._tab_labels: dict[int, ctk.CTkLabel] = {}
        self._active_sid: int | None = None
        self._next_sid = 1
        self._build_shell()
        self._new_session()
        self.after(50, lambda: _center_on_parent(self, parent))
        self.after(200, self._poll_permissions)
        self.bind("<Escape>", self._interrupt)

    def _interrupt(self, _event=None):
        pane = self._panes.get(self._active_sid)
        if pane and pane.session._busy:
            pane._interrupt()

    def _poll_permissions(self):
        for path in FLEET_DIR.glob("pending_permission_*.json"):
            pid_str = path.stem[len("pending_permission_"):]
            if pid_str in self._active_permission_pids:
                continue
            # Reap stale files from crashed gates (gate timeout is 60s)
            try:
                if time.time() - path.stat().st_mtime > 70:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                req = json.loads(path.read_text())
            except Exception:
                continue
            self._active_permission_pids.add(pid_str)
            PermissionDialog(
                self, req,
                on_done=lambda p=pid_str: self._active_permission_pids.discard(p),
            )
        self.after(150, self._poll_permissions)

    def _build_shell(self):
        self._tab_bar = ctk.CTkFrame(self, fg_color=C_HEADER, height=44)
        self._tab_bar.pack(fill="x")
        self._tab_bar.pack_propagate(False)

        self._tabs_row = ctk.CTkFrame(self._tab_bar, fg_color="transparent")
        self._tabs_row.pack(side="left", fill="y", padx=(6, 0))

        ctk.CTkButton(
            self._tab_bar, text="+", width=32, height=30,
            font=("SF Pro Display", 18), corner_radius=6,
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._new_session
        ).pack(side="left", padx=(6, 2), pady=7)

        ctk.CTkButton(
            self._tab_bar, text="↺", width=32, height=30,
            font=("SF Pro Display", 14), corner_radius=6,
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_resume_dialog
        ).pack(side="left", padx=(0, 4), pady=7)

        ctk.CTkButton(
            self._tab_bar, text="Logs", width=44, height=30,
            font=("SF Pro Mono", 11), corner_radius=6,
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._open_logs_window
        ).pack(side="left", padx=(0, 6), pady=7)

        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=10, pady=(8, 10))

    def _open_resume_dialog(self):
        active_pane = self._panes.get(self._active_sid)
        project_dir = None
        if active_pane:
            raw = active_pane._dir_seg.get().replace(" (home)", "")
            expanded = os.path.expanduser(raw)
            project_key = re.sub(r"[^A-Za-z0-9]", "-", expanded)
            candidate = Path.home() / ".claude" / "projects" / project_key
            if candidate.exists():
                project_dir = str(candidate)
        ResumeDialog(self, on_resume=self._new_session_resumed, project_dir=project_dir)

    def _open_logs_window(self):
        if hasattr(self, "_logs_win") and self._logs_win.winfo_exists():
            self._logs_win.lift()
            return
        def get_path():
            pane = self._panes.get(self._active_sid)
            return pane._log_path if pane else None
        self._logs_win = LogViewer(self, "Session Log", get_path)

    def _new_session_resumed(self, uuid: str):
        self._new_session(resume_uuid=uuid)

    def _new_session(self, resume_uuid: str | None = None):
        sid = self._next_sid
        self._next_sid += 1
        session = Session(sid, self.after, resume_uuid=resume_uuid)
        session.name_changed_cb = lambda name, s=sid: self._on_name_change(s, name)
        self._sessions.append(session)

        pane = SessionPane(
            self._content, session,
            on_status_change=lambda: self._refresh_tab(sid)
        )
        self._panes[sid] = pane

        tab = ctk.CTkFrame(self._tabs_row, fg_color=C_BORDER, corner_radius=6)
        tab.pack(side="left", padx=(0, 3), pady=7)
        self._tab_frames[sid] = tab

        lbl = ctk.CTkLabel(
            tab, text=f"Session {sid}",
            font=("SF Pro Display", 12), width=72, anchor="w"
        )
        lbl.pack(side="left", padx=(8, 2), pady=5)
        lbl.bind("<Button-1>", lambda _e, s=sid: self._switch_tab(s))
        tab.bind("<Button-1>", lambda _e, s=sid: self._switch_tab(s))
        self._tab_labels[sid] = lbl

        ctk.CTkButton(
            tab, text="×", width=22, height=22,
            font=("SF Pro Display", 13), corner_radius=4,
            fg_color="transparent", hover_color="#3D2D2D",
            command=lambda s=sid: self._close_session(s)
        ).pack(side="left", padx=(0, 4), pady=4)

        self._switch_tab(sid)

    def _switch_tab(self, sid: int):
        for pane in self._panes.values():
            pane.pack_forget()
        self._active_sid = sid
        self._refresh_all_tabs()
        self._panes[sid].pack(fill="both", expand=True)
        self._panes[sid].focus_input()

    def _close_session(self, sid: int):
        if sid in self._panes:
            self._panes.pop(sid).destroy()
        if sid in self._tab_frames:
            self._tab_frames.pop(sid).destroy()
        self._tab_labels.pop(sid, None)
        self._sessions = [s for s in self._sessions if s.sid != sid]
        if self._active_sid == sid:
            if self._sessions:
                self._switch_tab(self._sessions[-1].sid)
            else:
                self._new_session()

    def _on_name_change(self, sid: int, name: str):
        lbl = self._tab_labels.get(sid)
        if lbl and lbl.winfo_exists():
            display = name if len(name) <= 14 else name[:13] + "…"
            lbl.configure(text=display)

    def _refresh_tab(self, sid: int):
        self._refresh_all_tabs()

    def _refresh_all_tabs(self):
        for s in self._sessions:
            frame = self._tab_frames.get(s.sid)
            if not frame:
                continue
            is_active = s.sid == self._active_sid
            if s._busy:
                frame.configure(fg_color="#5C1215" if not is_active else "#7A1820")
            else:
                frame.configure(fg_color="#3A1D1F" if is_active else C_BORDER)


# ── Main App ───────────────────────────────────────────────────────────────────

class FleetApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        if _HAS_TKDND:
            try:
                _TkDnD._require(self)
            except Exception:
                pass
        self.title(f"{AGENT_NAME} Fleet")
        self.geometry("660x540")
        self.minsize(500, 380)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.configure(fg_color=C_BG)

        self._config = load_config()
        self._cards: dict[str, AgentCard] = {}
        self._last_lctl: dict[str, dict] = {}
        self._lock = threading.Lock()

        self._build()
        self._refresh()
        self._schedule_auto_refresh()

    def _build(self):
        # Header
        header = ctk.CTkFrame(self, fg_color=C_HEADER, height=50)
        header.pack(fill="x")
        header.pack_propagate(False)

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left", padx=(14, 0), pady=10)
        ctk.CTkLabel(
            title_frame, text="tastytrade",
            font=("SF Pro Display", 18, "bold"), text_color=C_BRAND
        ).pack(side="left")
        ctk.CTkLabel(
            title_frame, text=" Fleet",
            font=("SF Pro Display", 18, "bold"), text_color="#FFFFFF"
        ).pack(side="left")

        self.ts_lbl = ctk.CTkLabel(
            header, text="", font=("SF Pro Mono", 11), text_color=C_MUTED
        )
        self.ts_lbl.pack(side="right", padx=8)

        ctk.CTkButton(
            header, text="↺", width=36, height=32,
            font=("SF Pro Display", 16), corner_radius=6,
            fg_color=BTN_NEUTRAL[0], hover_color=BTN_NEUTRAL[1],
            command=self._reload_config
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="+", width=36, height=32,
            font=("SF Pro Display", 20), corner_radius=6,
            fg_color=BTN_START[0], hover_color=BTN_START[1],
            command=self._open_add_dialog
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="Sessions", width=72, height=32,
            font=("SF Pro Display", 13), corner_radius=6,
            fg_color=C_BRAND, hover_color="#C41920",
            command=self._open_sessions
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="Bridge", width=62, height=32,
            font=("SF Pro Display", 13), corner_radius=6,
            fg_color="#0E7490", hover_color="#0891B2",
            command=self._open_bridge
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="Chat", width=58, height=32,
            font=("SF Pro Display", 13), corner_radius=6,
            fg_color="#4B2D8F", hover_color="#6D3FC0",
            command=self._open_chat
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="Built", width=52, height=32,
            font=("SF Pro Display", 13), corner_radius=6,
            fg_color="#1E3A5F", hover_color="#1D4ED8",
            command=self._open_built
        ).pack(side="right", padx=(4, 0), pady=8)

        ctk.CTkButton(
            header, text="Roster", width=62, height=32,
            font=("SF Pro Display", 13), corner_radius=6,
            fg_color="#5B21B6", hover_color="#6D28D9",
            command=self._open_roster
        ).pack(side="right", padx=(4, 0), pady=8)

        # Summary bar
        self.summary = SummaryBar(self)
        self.summary.pack(fill="x", padx=0, pady=0)

        # Agent cards
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=8, pady=8)

        for agent in self._config.get("agents", []):
            card = AgentCard(self.scroll, agent, on_action=self._handle_action)
            card.pack(fill="x", pady=4)
            self._cards[agent["label"]] = card

        # Log panel (hidden)
        self.log_panel = LogPanel(self)

    def _reload_config(self):
        self.summary.status_lbl.configure(text="Pulling…")
        self.update_idletasks()

        # Step 1 — git pull if we're inside a repo
        app_updated = False
        if (_REPO_DIR / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(_REPO_DIR), "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=15
                )
                changed = result.stdout + result.stderr
                app_updated = "fleet_app.py" in changed and "Already up to date" not in changed
            except Exception:
                pass

        if app_updated:
            self.summary.status_lbl.configure(text="Updated — restarting…")
            self.after(1500, lambda: os.execv(sys.executable, [sys.executable] + sys.argv))
            return

        # Step 2 — reload config + rebuild cards
        self._config = load_config()
        for card in self._cards.values():
            card.destroy()
        self._cards.clear()
        for agent in self._config.get("agents", []):
            card = AgentCard(self.scroll, agent, on_action=self._handle_action)
            card.pack(fill="x", pady=4)
            self._cards[agent["label"]] = card
        self._refresh()
        self.summary.status_lbl.configure(text="Config reloaded")
        self.after(3000, lambda: self.summary.status_lbl.configure(text=""))

    def _refresh(self):
        lctl = lctl_list()
        with self._lock:
            self._last_lctl = lctl

        running = 0
        total = len(self._config.get("agents", []))
        for agent in self._config.get("agents", []):
            label = agent["label"]
            if label not in self._cards:
                continue
            status = derive_status(agent, lctl)
            self._cards[label].update(status)
            if status["state"] in ("running", "stale"):
                running += 1

        now = datetime.now().strftime("%H:%M:%S")
        self.ts_lbl.configure(text=now)
        self.summary.update(running, total)

    def _schedule_auto_refresh(self):
        ms = int(self._config.get("refresh_interval_seconds", 10) * 1000)
        self.after(ms, self._on_auto_refresh)

    def _on_auto_refresh(self):
        self._refresh()
        self._schedule_auto_refresh()

    def _set_action_msg(self, msg: str):
        self.summary.update(0, 0, msg)  # just update text; counts refresh next cycle
        self._refresh()

    def _handle_action(self, action: str, agent: dict):
        label = agent.get("parent_label", agent["label"])
        plist = agent.get("plist")

        if action == "logs":
            content = tail_log(agent.get("log_err"))
            self.log_panel.show(agent, content)
            return

        self.summary.status_lbl.configure(
            text=f"{action}ing {agent['display_name']}…"
        )

        def run():
            if action == "toggle":
                with self._lock:
                    info = self._last_lctl.get(label, {})
                if info.get("pid", 0) > 0:
                    ok, msg = lctl_stop(label, plist)
                else:
                    ok, msg = lctl_start(label, plist)
            elif action == "restart":
                ok, msg = lctl_restart(label)
            else:
                return

            status_text = f"{agent['display_name']}: {msg}"
            self.after(0, lambda: self.summary.status_lbl.configure(text=status_text))
            time.sleep(1.5)
            self.after(0, self._refresh)
            self.after(5000, lambda: self.summary.status_lbl.configure(text=""))

        threading.Thread(target=run, daemon=True).start()

    def _open_chat(self):
        if not hasattr(self, "_chat_win") or not self._chat_win.winfo_exists():
            self._chat_win = ChatWindow(self)
        else:
            self._chat_win.lift()
            self._chat_win.entry.focus()

    def _open_sessions(self):
        if not hasattr(self, "_sessions_win") or not self._sessions_win.winfo_exists():
            self._sessions_win = SessionsWindow(self)
        else:
            self._sessions_win.lift()

    def _open_bridge(self):
        if not hasattr(self, "_bridge_win") or not self._bridge_win.winfo_exists():
            self._bridge_win = BridgeWindow(self)
        else:
            self._bridge_win.lift()

    def _open_built(self):
        if not hasattr(self, "_built_win") or not self._built_win.winfo_exists():
            self._built_win = BuiltWindow(self)
        else:
            self._built_win.lift()

    def _open_roster(self):
        if not hasattr(self, "_roster_win") or not self._roster_win.winfo_exists():
            self._roster_win = RosterWindow(self)
        else:
            self._roster_win.lift()

    def _open_add_dialog(self):
        existing = [a["label"] for a in self._config.get("agents", [])]
        AddAgentDialog(self, on_save=self._add_agent, existing_labels=existing)

    def _add_agent(self, agent: dict):
        # Persist to config.json
        self._config.setdefault("agents", []).append(agent)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self._config, f, indent=2)

        # Add card to scroll frame
        card = AgentCard(self.scroll, agent, on_action=self._handle_action)
        card.pack(fill="x", pady=4)
        self._cards[agent["label"]] = card

        self._refresh()
        self.summary.status_lbl.configure(text=f"Added {agent['display_name']}")
        self.after(4000, lambda: self.summary.status_lbl.configure(text=""))


def _set_dock_icon():
    # Must run after FleetApp() so Tk owns the NSApplication singleton.
    # Calling sharedApplication() before Tk init creates a plain NSApplication;
    # Tk 9 then sends TKApplication-private selectors to it and aborts.
    try:
        import AppKit as _AppKit
        _icns = FLEET_DIR / "ApolloFleet.icns"
        if _icns.exists():
            _img = _AppKit.NSImage.alloc().initWithContentsOfFile_(str(_icns))
            _AppKit.NSApplication.sharedApplication().setApplicationIconImage_(_img)
    except Exception:
        pass


def _write_memory_mcp_cfg() -> None:
    cfg = {
        "mcpServers": {
            "fleet_memory": {
                "type": "http",
                "url": "http://127.0.0.1:{}/mcp".format(_fleet_memory.PORT if _fleet_memory else 54321),
            }
        }
    }
    try:
        (FLEET_DIR / "memory_mcp.json").write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def _memory_server_reachable() -> bool:
    """Return True if fleet_memory daemon is already listening on 127.0.0.1:54321."""
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:54321/health", timeout=1, context=_SSL_CTX
        )
        return True
    except Exception:
        return False


def _ensure_app_bundle() -> None:
    """Create ~/Applications/Fleet.app on first launch if it doesn't exist."""
    import stat, shutil, subprocess
    app = Path.home() / "Applications" / "Fleet.app"
    if app.exists():
        return
    try:
        macos = app / "Contents" / "MacOS"
        res   = app / "Contents" / "Resources"
        macos.mkdir(parents=True, exist_ok=True)
        res.mkdir(parents=True, exist_ok=True)

        (app / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '    <key>CFBundleExecutable</key><string>Fleet</string>\n'
            '    <key>CFBundleIdentifier</key><string>com.fleet.app</string>\n'
            '    <key>CFBundleName</key><string>Fleet</string>\n'
            '    <key>CFBundleDisplayName</key><string>Fleet</string>\n'
            '    <key>CFBundleIconFile</key><string>ApolloFleet</string>\n'
            '    <key>CFBundlePackageType</key><string>APPL</string>\n'
            '    <key>CFBundleVersion</key><string>1.0</string>\n'
            '    <key>CFBundleShortVersionString</key><string>1.0</string>\n'
            '    <key>LSMinimumSystemVersion</key><string>11.0</string>\n'
            '    <key>NSHighResolutionCapable</key><true/>\n'
            '    <key>NSSupportsAutomaticGraphicsSwitching</key><true/>\n'
            '</dict>\n</plist>\n'
        )

        launcher = macos / "Fleet"
        launcher.write_text('#!/bin/zsh\nexec "$HOME/.fleet/launch.sh"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        icns_src = FLEET_DIR / "ApolloFleet.icns"
        if icns_src.exists():
            shutil.copy2(icns_src, res / "ApolloFleet.icns")

        _lsr = Path(
            "/System/Library/Frameworks/CoreServices.framework"
            "/Versions/A/Frameworks/LaunchServices.framework"
            "/Versions/A/Support/lsregister"
        )
        if _lsr.exists():
            subprocess.run([str(_lsr), "-f", str(app)], capture_output=True, timeout=10)
    except Exception:
        pass  # never block startup


def main():
    _ensure_app_bundle()
    _mem_started_by_fleet = False
    if _fleet_memory:
        if _memory_server_reachable():
            # daemon already has the port — skip start_server to avoid conflict
            pass
        else:
            _fleet_memory.start_server()
            _mem_started_by_fleet = True
        _write_memory_mcp_cfg()

    app = FleetApp()
    _set_dock_icon()
    app.mainloop()

    if _fleet_memory and _mem_started_by_fleet:
        _fleet_memory.stop_server()


if __name__ == "__main__":
    main()
