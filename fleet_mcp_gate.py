#!/usr/bin/env python3
"""Fleet MCP permission gate + proxy tools.

Spawned by Claude CLI via --mcp-config when GATE mode is enabled.
Exposes fleet_bash, fleet_edit, fleet_write as gated proxies for the
built-in Bash/Edit/Write tools (which are disallowed in GATE mode).
Each call blocks until the Fleet UI approves or denies it.
"""
from __future__ import annotations
import sys
import json
import os
import time
import uuid
import subprocess
from pathlib import Path

FLEET_DIR = Path.home() / ".fleet"
AUDIT_LOG = FLEET_DIR / "console-audit.log"
DEBUG_LOG = FLEET_DIR / "gate_debug.log"
MY_PID    = os.getpid()
FLEET_CWD = sys.argv[1] if len(sys.argv) > 1 else str(Path.home())


def _dlog(msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={MY_PID} {msg}\n")
    except Exception:
        pass

PENDING  = FLEET_DIR / f"pending_permission_{MY_PID}.json"
RESPONSE = FLEET_DIR / f"permission_response_{MY_PID}.json"

TOOLS = [
    {
        "name": "fleet_bash",
        "description": (
            "Run a shell command. Use this instead of Bash — "
            "the user will be prompted to approve each command before it executes."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["command"],
            "properties": {
                "command":     {"type": "string", "description": "Shell command to run"},
                "timeout":     {"type": "number", "description": "Timeout in seconds (default 120)"},
                "description": {"type": "string", "description": "Brief description of what this command does"},
            },
        },
    },
    {
        "name": "fleet_edit",
        "description": (
            "Replace a string in a file (str_replace). Use this instead of Edit — "
            "the user will be prompted to approve each change before it's applied."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["file_path", "old_string", "new_string"],
            "properties": {
                "file_path":  {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
        },
    },
    {
        "name": "fleet_write",
        "description": (
            "Write content to a file. Use this instead of Write — "
            "the user will be prompted to approve before the file is written."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["file_path", "content"],
            "properties": {
                "file_path": {"type": "string"},
                "content":   {"type": "string"},
            },
        },
    },
]


def _audit(action: str, tool: str, detail: str = "") -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{ts}\t{action}\t{tool}\t{detail}\n")
    except Exception:
        pass


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _confirm(tool_name: str, input_data: dict) -> dict:
    """Show Fleet permission dialog and wait for allow/deny. Returns {"behavior": ..., "message": ...}."""
    # Strip fleet_ prefix for display (fleet_bash → Bash)
    display_name = tool_name[len("fleet_"):].title() if tool_name.startswith("fleet_") else tool_name

    req_id = str(uuid.uuid4())
    try:
        RESPONSE.unlink()
    except OSError:
        pass
    _dlog(f"writing pending for {display_name}")
    PENDING.write_text(json.dumps({
        "request_id": req_id,
        "tool_name":  display_name,
        "input":      input_data,
        "cwd":        FLEET_CWD,
        "pid":        MY_PID,
    }))
    _dlog(f"pending written, waiting for response")

    deadline = time.time() + 60
    while time.time() < deadline:
        if RESPONSE.exists():
            try:
                resp = json.loads(RESPONSE.read_text())
                try:
                    RESPONSE.unlink()
                except OSError:
                    pass
                if resp.get("request_id") == req_id:
                    try:
                        PENDING.unlink()
                    except OSError:
                        pass
                    behavior = resp.get("behavior", "deny")
                    _audit(behavior.upper(), display_name, f"cwd={FLEET_CWD}")
                    return {"behavior": behavior, "message": resp.get("message", "")}
                # stale response — discard, keep polling
            except Exception:
                pass
        time.sleep(0.1)

    try:
        PENDING.unlink()
    except OSError:
        pass
    _audit("DENY", display_name, f"timeout cwd={FLEET_CWD}")
    return {"behavior": "deny", "message": "Timed out — denied"}


def _exec_bash(args: dict) -> str:
    command = args.get("command", "")
    timeout = args.get("timeout", 120)
    try:
        result = subprocess.run(
            command, shell=True, cwd=FLEET_CWD,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        parts.append(f"[exit {result.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"[error] Command timed out after {timeout}s"
    except Exception as e:
        return f"[error] {e}"


def _exec_edit(args: dict) -> str:
    file_path = args.get("file_path", "")
    old_str   = args.get("old_string", "")
    new_str   = args.get("new_string", "")
    try:
        path = Path(file_path) if os.path.isabs(file_path) else Path(FLEET_CWD) / file_path
        content = path.read_text(encoding="utf-8")
        if old_str not in content:
            return f"[error] old_string not found in {file_path}"
        path.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return f"Edited {file_path}"
    except Exception as e:
        return f"[error] {e}"


def _exec_write(args: dict) -> str:
    file_path = args.get("file_path", "")
    content   = args.get("content", "")
    try:
        path = Path(file_path) if os.path.isabs(file_path) else Path(FLEET_CWD) / file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Written {file_path}"
    except Exception as e:
        return f"[error] {e}"


PROXY_EXEC = {
    "fleet_bash":  _exec_bash,
    "fleet_edit":  _exec_edit,
    "fleet_write": _exec_write,
}


def main() -> None:
    _dlog(f"gate started cwd={FLEET_CWD}")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        rid    = req.get("id")

        if method == "initialize":
            client_ver = req.get("params", {}).get("protocolVersion", "2024-11-05")
            _send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": client_ver,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fleet-gate", "version": "2.0.0"},
                },
            })

        elif method in ("notifications/initialized", "initialized"):
            pass

        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            params = req.get("params", {})
            name   = params.get("name", "")
            args   = params.get("arguments", {})

            if name in PROXY_EXEC:
                decision = _confirm(name, args)
                if decision["behavior"] == "allow":
                    output   = PROXY_EXEC[name](args)
                    is_error = output.startswith("[error]")
                else:
                    output   = f"[denied] {decision.get('message', 'Denied by user')}"
                    is_error = True
                _send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {
                        "content": [{"type": "text", "text": output}],
                        "isError": is_error,
                    },
                })
            else:
                _send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32601, "message": f"Unknown tool: {name}"}})

        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    main()
