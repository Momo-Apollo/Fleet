#!/bin/zsh
# Fleet — uninstall script
# Removes everything the installer wrote. Safe to re-run install.sh after this.
set -e

ok()   { print -P "%F{green}  ✓%f $1" }
warn() { print -P "%F{yellow}  ⚠%f $1" }
info() { print -P "\n%F{cyan}▶%f $1" }
ask()  { print -n "  $1 " >&2; read -r REPLY </dev/tty; print "$REPLY" }

print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print "  Fleet — uninstall"
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print ""
print "  This will remove ~/.fleet, the bridge listener plist,"
print "  undo any CLAUDE.md patch, and optionally remove SOUL.md."
REPLY=$(ask "Continue? [y/N]")
[[ "$REPLY" =~ ^[Yy]$ ]] || { print "  Aborted."; exit 0 }

# ── bridge-collab-listener ────────────────────────────────────────────────────
info "Bridge listener"

BCL_LABEL="com.${USER}.bridge-collab-listener"
BCL_PLIST="$HOME/Library/LaunchAgents/${BCL_LABEL}.plist"

launchctl bootout "gui/$UID/$BCL_LABEL" 2>/dev/null && ok "listener stopped" || true

if [[ -f "$BCL_PLIST" ]]; then
    rm "$BCL_PLIST"
    ok "plist removed ($BCL_PLIST)"
else
    warn "plist not found — skipping"
fi

# ── ~/.fleet ──────────────────────────────────────────────────────────────────
info "Removing ~/.fleet"
if [[ -d "$HOME/.fleet" ]]; then
    rm -rf "$HOME/.fleet"
    ok "~/.fleet removed"
else
    warn "~/.fleet not found — skipping"
fi

# ── CLAUDE.md patch ───────────────────────────────────────────────────────────
info "Checking CLAUDE.md"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
IDENTITY_LINE="@~/.claude/SOUL.md"

if [[ -f "$CLAUDE_MD" ]] && grep -qF "$IDENTITY_LINE" "$CLAUDE_MD"; then
    sed -i '' '/^@~\/.claude\/SOUL\.md$/d' "$CLAUDE_MD"
    ok "SOUL.md reference removed from CLAUDE.md"
else
    ok "CLAUDE.md clean — nothing to remove"
fi

# ── SOUL.md ───────────────────────────────────────────────────────────────────
info "SOUL.md"
SOUL_FILE="$HOME/.claude/SOUL.md"

if [[ -f "$SOUL_FILE" ]]; then
    print ""
    print "  ~/.claude/SOUL.md exists — you may have customized it."
    REPLY=$(ask "Remove it? [y/N]")
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        rm "$SOUL_FILE"
        ok "SOUL.md removed"
    else
        ok "SOUL.md kept"
    fi
else
    ok "SOUL.md not found — skipping"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print "  Done. Run ./install.sh to start fresh."
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
