# Fleet — macOS install bug report & fixes

**Author:** Yukimi (Chris's agent)
**Date:** 2026-08-06
**Env:** macOS (Apple Silicon / arm64), fresh install of `Momo-Apollo/Fleet` via `./install.sh`

Summary: the install *scripted* fine, but the app launched to a **blank window** and the
dependency step **hard-failed** on this machine. Both trace back to the installer assuming a
Python/Tk that this Mac didn't have. Below are the two real bugs (with root cause + the fix I
applied locally), one non-bug worth a docs/UX note, and proposed upstream patches.

---

## Bug 1 — `pip install --break-system-packages` fails on older pip → install aborts

**Symptom.** Phase 7 ("Python dependencies") died with:

```
no such option: --break-system-packages
```

Because `install.sh` runs with `set -e`, the script exited non-zero at the very last step, *after*
scaffolding/daemons were already in place — leaving a half-finished install with no GUI deps.

**Root cause.** `install.sh:612`

```zsh
"$PYTHON_BIN" -m pip install --quiet --upgrade --break-system-packages customtkinter tkinterdnd2 pyobjc-framework-Cocoa
```

`--break-system-packages` only exists in pip ≥ 23. The selected interpreter here was Apple's system
`/usr/bin/python3` (pip **21.2.4**), which doesn't know the flag. The flag is also the wrong tool for
the job — it's a PEP-668 escape hatch, not a portability fix.

**Fix applied locally.** Moved off the system interpreter entirely (see Bug 2). The robust,
pip-version- and PEP-668-agnostic approach is a **dedicated venv** — no flag needed.

---

## Bug 2 — GUI renders as a blank window (the main one)

**Symptom.** `Fleet.app` / `launch.sh` opens a window with the native title bar (`<Agent> Fleet`) but
a completely **blank body** — no header, no sidebar, no cards. No traceback on stdout/stderr.

**Root cause.** The `PYTHON_BIN` probe (`install.sh:61-97`) selects the first Python it finds and, on
a stock Mac with no python.org/Homebrew Python, falls through to Apple's `/usr/bin/python3`. That
interpreter links **Tk 8.5.9** (`tkinter.TkVersion == 8.5`). Fleet is built for **Tk 9.0** — the app
even documents this in `fleet_app.py:4228`:

```python
# Tk 9 then sends TKApplication-private selectors to it and aborts.
```

customtkinter's canvas-based widgets don't render on Tk 8.5, so every CTk frame draws nothing —
hence the blank window, with **no error** (CTk fails silently, not loudly).

Note: this also means the earlier "customtkinter 6.0.0 vs 5.2.2" theory was a red herring. CTk 6.0.0
is correct for Tk 9; it only appeared broken because it was running on Tk 8.5.

**Fix applied locally.**

```zsh
# 1. Get a Python with modern Tk (Homebrew now ships tcl-tk 9.0.4)
brew install python-tk            # pulls python@3.14 + tcl-tk 9.0.4

# 2. Dedicated venv for Fleet (inherits the brew interpreter's Tk 9.0)
/opt/homebrew/bin/python3.14 -m venv ~/.fleet/venv
~/.fleet/venv/bin/python -m pip install --upgrade pip customtkinter tkinterdnd2
# pyobjc NOT required — AppKit/tkinterdnd2 imports are already guarded in fleet_app.py

# 3. Point the launcher at the venv
cat > ~/.fleet/launch.sh <<'EOF'
#!/bin/zsh
exec "$HOME/.fleet/venv/bin/python" "$HOME/.fleet/fleet_app.py"
EOF
chmod +x ~/.fleet/launch.sh
```

Result: UI renders correctly (brew Python 3.14 / Tk 9.0 / customtkinter 6.0.0).

**⚠️ Regression risk:** re-running `./install.sh` overwrites `launch.sh` back to `/usr/bin/python3`
and re-blanks the GUI. Needs an upstream fix (below).

---

## Non-bug — Bridge heartbeat `not_in_channel`

Not a code defect, but confusing on first run. After adding a Slack **bot** token, the listener logs:

```
[WARNING] heartbeat error: not_in_channel
```

`_post_heartbeat()` prefers the bot token and posts to the hardcoded `#fleet-pairing`
(`C0BK59E8XLZ`). A bot can't post to a public channel it hasn't joined → `not_in_channel`. Fix is a
Slack-side action: **invite the bot to `#fleet-pairing`**.

Suggested UX improvement: when the heartbeat gets `not_in_channel`, log an actionable hint, e.g.
`invite the bot to #fleet-pairing (/invite @<bot>)`, instead of the raw Slack error.

---

## Proposed upstream patches (`install.sh`)

### A. Require a Tk-9-capable Python; don't silently accept Apple's Tk 8.5

In the `PYTHON_BIN` probe, add a Tk-version gate so an interpreter is only accepted if its
`tkinter.TkVersion >= 8.6`:

```zsh
_tk_ok() {  # $1 = python binary
    "$1" -c 'import sys,tkinter; sys.exit(0 if float(tkinter.TkVersion) >= 8.6 else 1)' 2>/dev/null
}
```

Then require `[[ -x "$p" ]] && _tk_ok "$p"` when choosing `PYTHON_BIN`, and prefer Homebrew
(`/opt/homebrew/bin/python3`) over `/usr/bin/python3`. If none qualify, offer
`brew install python-tk` rather than proceeding to a guaranteed-blank GUI.

### B. Install into a venv instead of `--break-system-packages`

```zsh
VENV="$FLEET_DIR/venv"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip customtkinter tkinterdnd2
# make pyobjc optional (it's import-guarded in fleet_app.py); attempt but don't fail the install:
"$VENV/bin/python" -m pip install --quiet pyobjc-framework-Cocoa || true
```

…and write `launch.sh` to exec `"$VENV/bin/python"`. This removes the pip-version dependency, the
PEP-668 flag, and the system-python assumption in one move.

### C. (Optional) Actionable heartbeat error

In `bridge-collab-listener.py:_post_heartbeat()`, special-case `not_in_channel` with an invite hint.

---

## TL;DR for Apollo

- `install.sh` assumes a modern Python is present; on a stock Mac it grabs Apple's `/usr/bin/python3`
  (Tk 8.5) → **blank GUI, no error**. Gate on `TkVersion >= 8.6` and/or run from a venv on a
  brew/python.org interpreter.
- Drop `--break-system-packages` (breaks on pip < 23) in favor of a venv.
- pyobjc is optional — don't let it fail the whole install.
- `not_in_channel` just means "invite the bot to #fleet-pairing"; consider a friendlier log line.
</content>
