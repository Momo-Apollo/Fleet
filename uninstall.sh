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
print "  This will remove ~/.fleet and undo any CLAUDE.md patch."
REPLY=$(ask "Continue? [y/N]")
[[ "$REPLY" =~ ^[Yy]$ ]] || { print "  Aborted."; exit 0 }

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
IDENTITY_LINE="@~/.fleet/fleet-identity.md"

if [[ -f "$CLAUDE_MD" ]] && grep -qF "$IDENTITY_LINE" "$CLAUDE_MD"; then
    # Remove the line (and any blank line immediately before it)
    sed -i '' '/^@~\/.fleet\/fleet-identity\.md$/d' "$CLAUDE_MD"
    ok "fleet-identity reference removed from CLAUDE.md"
else
    ok "CLAUDE.md clean — nothing to remove"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print "  Done. Run ./install.sh to start fresh."
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
