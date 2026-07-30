#!/usr/bin/env python3
"""PreCompact hook — extract and save cross-session memories before the context is wiped."""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

CLAUDE_BIN   = os.environ.get("CLAUDE_BIN", "/opt/homebrew/bin/claude")
MEMORY_TOOLS = "mcp__fleet_memory__memory_search,mcp__fleet_memory__memory_write"
MAX_CHARS    = 16_000  # cap fed to Claude


def _claude_env() -> dict:
    env = os.environ.copy()
    try:
        secrets = json.loads((Path.home() / ".fleet/secrets.json").read_text())
        if "claude_token" in secrets:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets["claude_token"]
    except Exception:
        pass
    return env


def _read_transcript(path: str) -> str:
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                role = obj.get("type") or obj.get("role", "")
                # assistant / user message objects
                content = obj.get("message", obj)
                if isinstance(content, dict):
                    role = content.get("role", role)
                    parts = content.get("content", "")
                    if isinstance(parts, list):
                        text = " ".join(
                            p.get("text", "") for p in parts if isinstance(p, dict)
                        )
                    else:
                        text = str(parts)
                else:
                    text = str(content)
                if text.strip():
                    lines.append(f"{role}: {text.strip()[:500]}")
    except Exception as e:
        return f"(could not read transcript: {e})"
    return "\n".join(lines)


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    # PreCompact provides transcript_path
    transcript_path = data.get("transcript_path") or data.get("transcriptPath")
    if transcript_path:
        conversation = _read_transcript(transcript_path)
    else:
        # fallback: whatever came in on stdin
        conversation = raw

    conversation = conversation[:MAX_CHARS]
    if not conversation.strip():
        return

    prompt = (
        "The conversation below is about to be compacted and the context will be lost. "
        "Your job: call memory_search first to avoid duplicates, then call memory_write "
        "for anything worth preserving across sessions.\n\n"
        "Save liberally — decisions made, bugs found and fixed, project state changes, "
        "patterns learned, one-liners (context matters). Skip only truly ephemeral details "
        "and things clearly already in memory.\n\n"
        "Use descriptive kebab-case names. Type: user | feedback | project | reference.\n\n"
        f"CONVERSATION:\n{conversation}"
    )

    try:
        subprocess.run(
            [CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
             "--allowedTools", MEMORY_TOOLS],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            env=_claude_env(),
        )
    except Exception as e:
        sys.stderr.write(f"[pre-compact-memory] error: {e}\n")


if __name__ == "__main__":
    main()
