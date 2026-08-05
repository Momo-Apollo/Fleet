# Fleet App — Honest Review & Proposed Changes

*Apollo's take, 2026-08-05. Rocky: add your own section at the bottom.*

---

## Overall Verdict

The app punches well above its weight. Bridge/Auto/Collab over Slack is genuinely novel infrastructure, not a toy. GATE mode's MCP proxy pattern is clean. Fleet Memory as a proper MCP server was the right call over flat files. The installer is thoughtful (merge-not-clobber).

But it's at the point where a little structural discipline would compound. Most of the rough edges below aren't bugs — they're the kind of debt that's invisible until you're knee-deep in a refactor at 11pm.

---

## What I'd Change

### 1. Split the single file — highest leverage

`fleet_app.py` is 4,200 lines. It works, but today I had to grep for the right `_append` because there are two of them. Every feature we add makes the next one harder to find.

Proposed split:

```
fleet/
  app.py              # entry point, main window, tab routing
  ui/
    chat.py           # ChatWindow
    sessions.py       # SessionsWindow, SessionPane, Session
    bridge.py         # BridgeWindow
    dashboard.py      # DashboardPane
    roster.py         # RosterDialog
    dialogs.py        # PermissionDialog, ResumeDialog, AddAgentDialog
  core/
    mcp_gate.py       # fleet_mcp_gate.py (already separate, just move)
    memory.py         # fleet_memory.py (already separate)
  utils/
    theme.py          # all C_* constants, BTN_* tuples, _tool_activity_tag, etc.
    attach.py         # _FileAttachMixin, _build_content_blocks
    log.py            # _fleet_log, ANSI_ESCAPE
```

The classes are already well-separated internally — this is mostly `Ctrl+X / Ctrl+V` work, not a redesign. The payoff is immediate: `bridge.py` can change without touching `sessions.py`.

---

### 2. Automate the sync tax

Every session ends with:
```
cp ~/Apollo-Fleet/fleet_app.py ~/Fleet/fleet_app.py
cp ~/Apollo-Fleet/fleet_app.py ~/rocky-apollo/fleet/fleet_app.py
```

This is friction, and it already caused a branch mixup today. Options:

- **Git submodule** — Fleet (agnostic) as a submodule of Apollo-Fleet. Sync is `git submodule update`.
- **Symlink** — `~/Fleet/fleet_app.py` is already symlinked to `~/.fleet/fleet_app.py`. Extend the pattern.
- **post-commit hook** — Apollo-Fleet commits trigger a script that copies + commits to Fleet and rocky-apollo automatically.

The hook approach is lowest friction and doesn't require restructuring repos. Rocky should weigh in on what works on his end.

---

### 3. Flip the GATE default

Sessions defaults to `dangerous=True` (skip all permissions). The GATE button enables gating. This is backwards for a tool designed around the idea that you want control over what runs.

Proposed: `dangerous=False` by default. The button becomes **UNGATED** when you explicitly want to skip prompts. Power users click once to go fast; default behavior is safe.

Counterargument: it would slow down every Session turn until the user discovers the button. Rocky's take welcome here — he might use Sessions differently.

---

### 4. Add a smoke test suite

Nothing elaborate. Three tests that would have caught real bugs we've hit:

```python
# test_fleet.py
def test_gate_confirm_roundtrip():
    # write a pending_permission_{pid}.json, assert _confirm() reads + returns it

def test_tool_activity_tag():
    assert _tool_activity_tag("⚙ Bash(ls)") == "act_bash"
    assert _tool_activity_tag("⚙ Read(/foo)") == "act_read"
    assert _tool_activity_tag("⚙ mcp__fleet__fleet_write") == "act_edit"

def test_session_uuid_reset_on_hard_threshold():
    # simulate N consecutive failures, assert UUID rotates at T2
```

`pytest` + `unittest.mock` for the subprocess bits. No GUI testing needed — the logic worth testing is all in non-UI classes.

---

### 5. Readme + tooltips — overdue

Deferred since 2026-07-22. The app is genuinely hard to onboard without me explaining it. Two things fix this:

**Tooltips** on every non-obvious button:
- Pair — "Discover agents via #fleet-pairing presence heartbeats"
- Auto — "Signal Rocky to enter autonomous response mode"
- Collab — "Start a ::collab-task:: session with Rocky"
- GATE — "Route Bash/Edit/Write through Fleet's approval dialog"

CTk doesn't have native tooltips but there's a standard pattern (bind `<Enter>`/`<Leave>`, show a small `CTkToplevel`). ~50 lines shared, wired once.

**README.md** in the Fleet repo covering: what Fleet is, panel overview, Bridge/Auto/Collab flow, install steps, two-repo setup.

---

### 6. Chat message history — persistence across restarts

Chat uses `--continue` to maintain session state in Claude's JSONL, but the visible history in the UI resets every time you close and reopen the window. The transcript exists on disk; we could replay the last N turns into the textbox on open.

This is a quality-of-life thing, not critical — but it's the most noticeable gap in Chat day-to-day.

---

### 7. Minor: sessions on `dangerous=True` should still show activity lines

When GATE is off (default), `Session.send()` uses `--dangerously-skip-permissions` and disables the MCP proxy entirely. Claude still calls tools — we just don't gate them. But activity lines (`⚙ Bash(...)`) still surface from the `assistant` events. The status label color update from today's session now makes these more readable. No change needed here — just noting it's working correctly.

---

## What's Working Well and Shouldn't Change

- **Bridge signal protocol** — the `bridge_state.json` + `_opened_at` stale-signal guard is solid. Don't touch it.
- **Fleet Memory MCP** — Streamable HTTP, always-on launchd daemon. Right call, right implementation.
- **GATE MCP proxy pattern** — `fleet_mcp_gate.py` is clean. If anything, it should be the model for future gating features.
- **Installer** — merge-not-clobber is the right default. Preserve it.

---

## Priority Order

| # | Change | Effort | Impact |
|---|--------|--------|--------|
| 1 | Tooltips + README | Low | High — unblocks new users |
| 2 | Smoke tests | Low | High — catches regressions |
| 3 | Sync automation (hook) | Low | Medium — saves 3 steps per session |
| 4 | File split | Medium | High — pays off on every future change |
| 5 | Chat history replay | Medium | Medium — quality of life |
| 6 | Flip GATE default | Low | Medium — pending Rocky's take |

---

---

## UI/UX Take

### What needs the most work

**Single-window navigation.** Chat, Sessions, and Bridge all spawn as separate `CTkToplevel` windows. In practice you end up managing 3-4 windows with no spatial consistency — they re-center on the parent every open. Everything should dock inside one window with a sidebar icon rail. This is the single highest-impact UX change; the app would feel twice as polished immediately.

**Empty states are blank.** When Chat first opens it's a dark box with a cursor. No prompt, no context. Even a single dim line — *"New conversation. Type to start."* — removes the "did it load?" moment. Sessions has the same problem on a fresh tab.

**⊕ does no work as a label.** The file attach button is consistent across panes, which is good — but ⊕ is ambiguous between "add" and "attach." A paperclip glyph or tooltip would make it immediately readable to a new user.

**Activity line font is 10pt.** The tool call lines are the most information-dense part of a running session, and they're the smallest text on screen. 11pt or matching the output size would help — the color coding we added today helps but the size undercuts it.

**No keyboard navigation.** `Cmd+1/2/3` for Chat/Sessions/Bridge, `Cmd+N` for new session — none of it exists. Power tool, no power-user shortcuts.

**Chat history blanks on reopen.** Already in the main list (#6 above) but worth calling out from a pure UX angle: the session *continues* but the visual history is gone. The cognitive mismatch is jarring — you type and Apollo remembers context you can't see.

---

### What's actually working well (corrections to prior take)

**Bridge Auto/Collab state is readable.** Initial take was that state was only communicated via color. That was wrong — the toggles are color-coded (Auto = red, Collab = green) *and* the status bar shows explicit text: `⚡ COLLAB ARMED — type task and send` and `⚡ AUTO — signalling…`. State is unambiguous when you're in the window. No change needed here.

---

### Nice-to-haves (not blockers)

- Window position persistence across restarts
- Notification badge on the main window when a background session finishes
- `Cmd+K` palette for quick actions (new session, open chat, toggle bridge)

---

## Rocky's Section

*Barrett / Rocky — add your takes below. Disagree with anything above, flag it.*

---
