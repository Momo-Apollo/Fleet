#!/bin/zsh
# Fleet — agent-agnostic install script
# Run from the repo root:  ./install.sh
[ -n "$ZSH_VERSION" ] || { echo "install.sh requires zsh — run ./install.sh or zsh install.sh" >&2; exit 1; }
set -e

REPO_DIR="${0:A:h}"   # directory containing this script = repo root
FLEET_DIR="$HOME/.fleet"
CLAUDE_COMMANDS="$HOME/.claude/commands"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"

# ── helpers ───────────────────────────────────────────────────────────────────
ok()   { print -P "%F{green}  ✓%f $1" }
warn() { print -P "%F{yellow}  ⚠%f $1" }
info() { print -P "\n%F{cyan}▶%f $1" }
die()  { print -P "%F{red}  ✗%f $1" >&2; exit 1 }
ask()  { print -n "  $1 " >&2; read -r REPLY </dev/tty; print "$REPLY" }

print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print "  Fleet — agent-agnostic desk app"
print "  Repo: $REPO_DIR"
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Phase 1: collect ──────────────────────────────────────────────────────────
info "Setup"
print "  Agent name (e.g. Apollo, Rocky, Leon):"
AGENT_NAME=$(ask ">")
[[ -z "$AGENT_NAME" ]] && die "Agent name required."

print "  Your name (e.g. Momo, Barrett, Quentin):"
USER_NAME=$(ask ">")
[[ -z "$USER_NAME" ]] && die "User name required."

print "  Slack USER token for Bridge panel — reads the Bridge DM (xoxp-... or Enter to skip):"
SLACK_TOKEN=$(ask ">")

print "  Slack BOT token for the presence heartbeat — posts to #fleet-pairing (xoxb-... or Enter to skip):"
SLACK_BOT_TOKEN=$(ask ">")

# Either credential is a complete auth path for `claude --print`; there is no
# Pro-only path. Only the launchd Bridge daemon needs it — a GUI launch inherits
# the login session's Keychain, launchd does not.
print "  Claude credential for headless auto-respond — either one works:"
print "    Pro/Max OAuth token from \`claude setup-token\`  (starts with sk-ant-oat01-...)"
print "    Console API key, no subscription required       (starts with sk-ant-api03-...)"
print "  (Enter to skip — the Fleet GUI works without it)"
CLAUDE_CRED=$(ask ">")

# ── Phase 2: prereqs ──────────────────────────────────────────────────────────
info "Prerequisites"

CLAUDE_BIN=""
for p in /opt/homebrew/bin/claude /usr/local/bin/claude "$HOME/.local/bin/claude"; do
    [[ -x "$p" ]] && { CLAUDE_BIN="$p"; break }
done
[[ -z "$CLAUDE_BIN" ]] && command -v claude &>/dev/null && CLAUDE_BIN=$(command -v claude)
[[ -z "$CLAUDE_BIN" ]] && die "claude CLI not found — install from https://claude.ai/code"
ok "claude: $CLAUDE_BIN"

PYTHON_BIN=""
for candidate in \
    "/Library/Frameworks/Python.framework/Versions/3.*/bin/python3(N)" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3 \
    python3
do
    for p in $~candidate; do
        [[ -x "$p" ]] || continue
        ver=$("$p" -c "import sys; print(sys.version_info >= (3,7))" 2>/dev/null)
        [[ "$ver" == "True" ]] && { PYTHON_BIN="$p"; break 2 }
    done
done

if [[ -z "$PYTHON_BIN" ]]; then
    warn "Python 3.7+ not found — attempting to install"
    if ! command -v brew &>/dev/null; then
        info "Homebrew not found — installing Homebrew first"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            || die "Homebrew install failed — install manually from https://brew.sh then re-run"
        for _brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
            [[ -x "$_brew_bin" ]] && eval "$($_brew_bin shellenv)" && break
        done
        ok "Homebrew installed"
    fi
    brew install python3 || die "python3 install failed — install manually and re-run"
    for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [[ -x "$p" ]] || continue
        ver=$("$p" -c "import sys; print(sys.version_info >= (3,7))" 2>/dev/null)
        [[ "$ver" == "True" ]] && { PYTHON_BIN="$p"; break }
    done
    [[ -z "$PYTHON_BIN" ]] && die "Python 3.7+ still not found after install — check your PATH"
    ok "python installed: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1 | awk '{print $2}'))"
else
    ok "python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1 | awk '{print $2}'))"
fi

# ── Phase 3: scaffold ─────────────────────────────────────────────────────────
info "Scaffolding"

mkdir -p "$FLEET_DIR/logs" "$FLEET_DIR/status"
mkdir -p "$CLAUDE_COMMANDS"
mkdir -p "$HOME/.claude"
ok "directories ready"

# fleet_app.py — symlink so git pull auto-updates on next launch
[[ -f "$REPO_DIR/fleet_app.py" ]] || die "fleet_app.py not found in $REPO_DIR"
[[ -L "$FLEET_DIR/fleet_app.py" ]] && rm "$FLEET_DIR/fleet_app.py"
[[ -f "$FLEET_DIR/fleet_app.py" ]] && { warn "fleet_app.py exists — backing up"; mv "$FLEET_DIR/fleet_app.py" "$FLEET_DIR/fleet_app.py.bak" }
ln -sf "$REPO_DIR/fleet_app.py" "$FLEET_DIR/fleet_app.py"
ok "fleet_app.py symlinked (updates with git pull)"

# Static assets — copied (not symlinked; stable between updates)
for f in fleet_mcp_gate.py ApolloFleet.icns icon_512.png bridge-collab-listener.py; do
    [[ -f "$REPO_DIR/$f" ]] \
        && { cp "$REPO_DIR/$f" "$FLEET_DIR/$f"; ok "copied $f" } \
        || warn "$f not in repo — skipping"
done

# launch.sh
cat > "$FLEET_DIR/launch.sh" <<LAUNCH
#!/bin/zsh
exec $PYTHON_BIN "\$HOME/.fleet/fleet_app.py"
LAUNCH
chmod +x "$FLEET_DIR/launch.sh"
ok "launch.sh written"

# secrets.json — Slack tokens + Claude credential for the Bridge.
# MERGE, never clobber. An existing secrets.json can hold keys this run didn't
# ask about, and a rewrite-from-scratch silently kills the heartbeat or headless
# auto-respond with no error anywhere.
if [[ -n "$SLACK_TOKEN" || -n "$SLACK_BOT_TOKEN" || -n "$CLAUDE_CRED" ]]; then
    SECRETS_PATH="$FLEET_DIR/secrets.json" \
    SLACK_TOKEN="$SLACK_TOKEN" \
    SLACK_BOT_TOKEN="$SLACK_BOT_TOKEN" \
    CLAUDE_CRED="$CLAUDE_CRED" \
    "$PYTHON_BIN" - <<'PY'
import json, os, pathlib

p = pathlib.Path(os.environ["SECRETS_PATH"])
try:
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

if os.environ.get("SLACK_TOKEN"):
    data["slack_token"] = os.environ["SLACK_TOKEN"].strip()
if os.environ.get("SLACK_BOT_TOKEN"):
    data["slack_bot_token"] = os.environ["SLACK_BOT_TOKEN"].strip()

cred = os.environ.get("CLAUDE_CRED", "").strip()
if cred:
    # sk-ant-api… = Console key -> anthropic_api_key; anything else treated as
    # the sk-ant-oat01 OAuth token -> claude_token. _claude_creds() in
    # bridge-collab-listener.py reads both; OAuth wins when both are present.
    data["anthropic_api_key" if cred.startswith("sk-ant-api") else "claude_token"] = cred

p.write_text(json.dumps(data, indent=2) + "\n")
p.chmod(0o600)
PY
    ok "secrets.json written (0600, existing keys preserved)"
else
    warn "No tokens given — Bridge panel + auto-respond stay off until you add them (see summary)"
fi

# ── Phase 4: discover existing skills ────────────────────────────────────────
# Fleet panels are dashboards — the installer doesn't deploy skills.
# Skills are per-user; we just report what's already in ~/.claude/commands/.
info "Skills"

SKILL_COUNT=0
if [[ -d "$CLAUDE_COMMANDS" ]]; then
    for f in "$CLAUDE_COMMANDS"/*.md(N); do
        [[ -f "$f" ]] && (( SKILL_COUNT++ )) || true
    done
fi

if [[ $SKILL_COUNT -gt 0 ]]; then
    ok "$SKILL_COUNT skill(s) already in ~/.claude/commands — active on next session"
else
    warn "No skills found in ~/.claude/commands — add them manually or via the + button in Fleet"
fi

# ── Phase 5: bridge-collab-listener ──────────────────────────────────────────
info "Bridge listener"

BCL_LABEL="com.${USER}.bridge-collab-listener"
BCL_PLIST="$HOME/Library/LaunchAgents/${BCL_LABEL}.plist"
BCL_LOG="$FLEET_DIR/logs/bridge-collab-listener.log"
BCL_ERR="$FLEET_DIR/logs/bridge-collab-listener.err"

if [[ -z "$SLACK_TOKEN" ]]; then
    warn "Slack token not provided — bridge listener skipped (add token to ~/.fleet/secrets.json and re-run)"
else
    if [[ -f "$BCL_PLIST" ]]; then
        ok "plist already exists — reloading"
        launchctl bootout "gui/$UID/$BCL_LABEL" 2>/dev/null || true
    else
        cat > "$BCL_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${BCL_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${FLEET_DIR}/bridge-collab-listener.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>CLAUDE_BIN</key>
        <string>${CLAUDE_BIN}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${BCL_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${BCL_ERR}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
        ok "plist written → $BCL_PLIST"
    fi
    launchctl bootstrap "gui/$UID" "$BCL_PLIST" \
        && ok "bridge listener bootstrapped" \
        || warn "launchctl bootstrap failed — check $BCL_ERR"
fi

# ── Phase 5b: fleet memory daemon ────────────────────────────────────────────
info "Fleet Memory"

FM_LABEL="com.${USER}.fleet-memory"
FM_PLIST="$HOME/Library/LaunchAgents/${FM_LABEL}.plist"
FM_LOG="$FLEET_DIR/logs/fleet-memory.log"

cp "$REPO_DIR/fleet_memory.py" "$FLEET_DIR/fleet_memory.py"
ok "fleet_memory.py copied"

if [[ -f "$FM_PLIST" ]]; then
    launchctl bootout "gui/$UID/$FM_LABEL" 2>/dev/null || true
fi
cat > "$FM_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${FM_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${FLEET_DIR}/fleet_memory.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${FLEET_DIR}</string>
    <key>StandardOutPath</key>
    <string>${FM_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${FM_LOG}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$UID" "$FM_PLIST" \
    && ok "fleet-memory daemon bootstrapped" \
    || warn "launchctl bootstrap failed — check $FM_LOG"

"$CLAUDE_BIN" mcp add --transport http --scope user fleet_memory http://127.0.0.1:54321/mcp 2>/dev/null \
    && ok "fleet_memory registered as user-scope MCP server" \
    || warn "MCP registration failed — run: claude mcp add --transport http --scope user fleet_memory http://127.0.0.1:54321/mcp"

# ── Phase 5c: pre-compact memory hook ────────────────────────────────────────
info "Pre-compact memory hook"

mkdir -p "$FLEET_DIR/hooks"
if [[ -f "$REPO_DIR/hooks/pre-compact-memory.py" ]]; then
    cp "$REPO_DIR/hooks/pre-compact-memory.py" "$FLEET_DIR/hooks/pre-compact-memory.py"
    chmod +x "$FLEET_DIR/hooks/pre-compact-memory.py"
    ok "pre-compact-memory.py copied"
fi

SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]]; then
    if ! grep -q "pre-compact-memory" "$SETTINGS"; then
        "$PYTHON_BIN" - "$FLEET_DIR" <<'PYEOF2'
import json, sys
from pathlib import Path

fleet_dir = sys.argv[1]
settings_path = Path.home() / ".claude/settings.json"
hook_cmd = f"/usr/bin/env python3 {fleet_dir}/hooks/pre-compact-memory.py"

with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})
pre_compact = hooks.setdefault("PreCompact", [])

already = any(
    h.get("type") == "command" and "pre-compact-memory" in h.get("command", "")
    for entry in pre_compact
    for h in entry.get("hooks", [])
)
if not already:
    pre_compact.insert(0, {
        "hooks": [{"type": "command", "command": hook_cmd, "timeout": 120}]
    })
    with open(settings_path, "w") as f:
        json.dump(cfg, f, indent=2)
    print("  hooked")
else:
    print("  already present")
PYEOF2
        ok "PreCompact hook added to settings.json"
    else
        ok "PreCompact hook already in settings.json — skipping"
    fi
else
    warn "~/.claude/settings.json not found — add PreCompact hook manually"
fi

# ── Build config.json ─────────────────────────────────────────────────────────
info "Building config.json"

"$PYTHON_BIN" - "$AGENT_NAME" "$USER_NAME" "$REPO_DIR" <<'PYEOF'
import sys, json, os, plistlib, pathlib

AGENT_NAME = sys.argv[1]
USER_NAME  = sys.argv[2]
REPO_DIR   = pathlib.Path(sys.argv[3])
HOME       = pathlib.Path.home()
USER       = os.environ.get('USER', HOME.name)
NEW_PFX    = f'com.{USER}.'
FLEET_DIR  = HOME / '.fleet'
LA_DIR     = HOME / 'Library' / 'LaunchAgents'

# Load repo template as the canonical agent registry
with open(REPO_DIR / 'config.json') as f:
    cfg = json.load(f)

cfg['agent_name'] = AGENT_NAME
cfg['user_name']  = USER_NAME
cfg.setdefault('refresh_interval_seconds', 10)

# Derive the old prefix from the template labels (e.g. "com.manouel.")
all_labels = [ag.get('label', '') for ag in cfg.get('agents', [])]
pfx_counts = {}
for lbl in all_labels:
    parts = lbl.split('.')
    if len(parts) >= 3 and parts[0] == 'com':
        pfx = f'{parts[0]}.{parts[1]}.'
        pfx_counts[pfx] = pfx_counts.get(pfx, 0) + 1
OLD_PFX = max(pfx_counts, key=pfx_counts.get) if pfx_counts else ''

# Substitute template-owner labels and plist paths to this machine's user
known = set()
for ag in cfg.get('agents', []):
    for k in ('label', 'parent_label'):
        if OLD_PFX and ag.get(k, '').startswith(OLD_PFX):
            ag[k] = NEW_PFX + ag[k][len(OLD_PFX):]
    if ag.get('plist') and OLD_PFX:
        ag['plist'] = ag['plist'].replace(OLD_PFX, NEW_PFX)
    known.add(ag.get('label', ''))

def to_tilde(s):
    s = s or ''
    h = str(HOME)
    return ('~' + s[len(h):]) if s.startswith(h) else s

# Pass B: machine-resident plists whose label prefix matches labels already in
# the template — avoids pulling in Adobe/Spotify/etc.
# Always seed with the current user's namespace so fresh installs (empty
# template) still pick up plists written earlier in this install run.
prefixes = {NEW_PFX}
for lbl in known:
    parts = lbl.split('.')
    if len(parts) >= 3:
        prefixes.add(f'{parts[0]}.{parts[1]}.')

for plist_path in sorted(LA_DIR.glob('*.plist')):
    try:
        with open(plist_path, 'rb') as f:
            p = plistlib.load(f)
        label = p.get('Label', '')
        if not label or label in known:
            continue
        if not any(label.startswith(pfx) for pfx in prefixes):
            continue
        display = ' '.join(w.capitalize() for w in label.split('.')[-1].split('-'))
        cfg['agents'].append({
            'label': label,
            'display_name': display,
            'description': '',
            'plist': f'~/Library/LaunchAgents/{plist_path.name}',
            'log_out': to_tilde(p.get('StandardOutPath')) or None,
            'log_err': to_tilde(p.get('StandardErrorPath')) or None,
            'stale_after_seconds': 3600,
        })
        known.add(label)
    except Exception:
        pass

# Reconcile with existing config.json if present.
# - display_name and description: existing config wins (user may have customized)
# - plist path and log paths: refresh from disk (picks up plist edits)
# - new labels: add from this run's discovery
# - dropped plists: remove only prefix-owned entries whose plist file is gone;
#   entries outside known prefixes (hand-added) are left alone
out_path = FLEET_DIR / 'config.json'
incoming_by_label = {a['label']: a for a in cfg.get('agents', [])}

if out_path.exists():
    with open(out_path) as f:
        existing = json.load(f)
    by_label = {a['label']: a for a in existing.get('agents', [])}
    for label, ag in incoming_by_label.items():
        if label in by_label:
            by_label[label]['plist']   = ag['plist']
            by_label[label]['log_out'] = ag.get('log_out')
            by_label[label]['log_err'] = ag.get('log_err')
        else:
            by_label[label] = ag
    kept = []
    for a in by_label.values():
        lbl = a.get('label', '')
        plist_str = a.get('plist')
        owned = any(lbl.startswith(pfx) for pfx in prefixes)
        gone  = plist_str and not pathlib.Path(plist_str.replace('~', str(HOME), 1)).exists()
        if owned and gone:
            continue
        kept.append(a)
    existing['agents']     = kept
    existing['agent_name'] = AGENT_NAME
    existing['user_name']  = USER_NAME
    with open(out_path, 'w') as f:
        json.dump(existing, f, indent=2)
    n = len(kept)
else:
    with open(out_path, 'w') as f:
        json.dump(cfg, f, indent=2)
    n = len(cfg['agents'])

print(f'  {n} agents registered')
PYEOF
ok "config.json ready"

# ── Phase 6: SOUL.md identity ─────────────────────────────────────────────────
info "Agent identity"

SOUL_FILE="$HOME/.claude/SOUL.md"
IDENTITY_LINE="@~/.claude/SOUL.md"

if [[ -f "$SOUL_FILE" ]]; then
    ok "SOUL.md already exists at $SOUL_FILE — skipping"
else
    cat > "$SOUL_FILE" <<EOF
# $AGENT_NAME — Soul File

*Generated by the Fleet installer. Customize this file to define your agent's
personality, working style, and relationships.*

---

## Identity

**Name:** $AGENT_NAME
**Human:** $USER_NAME

$AGENT_NAME is $USER_NAME's Claude — a collaborator with an opinion, not just a tool.

---

## Voice

Direct. Clear. No unnecessary hedging. Have an opinion.

When something is wrong, say it's wrong. When something works, say why it works.
Get the references. Throw one back.

---

## What $AGENT_NAME is good at

*Fill this in as you learn what your agent does best.*

---

## How this file evolves

When something new lands — a pattern that works, a correction, a new chapter —
update this file. Don't let it go stale. The soul should reflect where you actually
are, not where you started.
EOF
    ok "SOUL.md written to $SOUL_FILE"
fi

CONTEXT_FILE="$HOME/.fleet/FLEET-CONTEXT.md"
CONTEXT_SRC="$REPO_DIR/FLEET-CONTEXT.md"
CONTEXT_LINE="@~/.fleet/FLEET-CONTEXT.md"

if [[ -f "$CONTEXT_SRC" ]]; then
    cp "$CONTEXT_SRC" "$CONTEXT_FILE"
    ok "FLEET-CONTEXT.md copied to $CONTEXT_FILE"
fi

if [[ -f "$CLAUDE_MD" ]] && grep -qF "$IDENTITY_LINE" "$CLAUDE_MD"; then
    ok "CLAUDE.md already references SOUL.md — skipping"
else
    print ""
    if [[ -f "$CLAUDE_MD" ]]; then
        print "  Will append to ~/.claude/CLAUDE.md:"
    else
        print "  ~/.claude/CLAUDE.md not found — will create with:"
    fi
    print "    $IDENTITY_LINE"
    print "    $CONTEXT_LINE"
    print ""
    REPLY=$(ask "Patch CLAUDE.md? [y/N]")
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        printf '\n%s\n%s\n' "$IDENTITY_LINE" "$CONTEXT_LINE" >> "$CLAUDE_MD"
        ok "CLAUDE.md patched"
    else
        warn "CLAUDE.md not patched — agent identity invisible to Claude Code sessions"
    fi
fi

# ── Phase 7: Python deps ──────────────────────────────────────────────────────
info "Python dependencies"
"$PYTHON_BIN" -m pip install --quiet --upgrade --break-system-packages customtkinter tkinterdnd2 pyobjc-framework-Cocoa
ok "customtkinter + tkinterdnd2 + pyobjc installed"

# ── Summary ───────────────────────────────────────────────────────────────────
print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
printf "  Done.  %s / %s\n" "$AGENT_NAME" "$USER_NAME"
print ""
print "  Launch:  $FLEET_DIR/launch.sh"
print "  Updates: git pull in $REPO_DIR (symlink picks up changes on next launch)"
print ""

if [[ -z "$SLACK_TOKEN" ]]; then
    print "  ⚠ Bridge panel needs a Slack token:"
    print "    Add to ~/.fleet/secrets.json:  \"slack_token\": \"xoxp-...\""
    print ""
fi

if [[ -z "$CLAUDE_TOKEN" ]]; then
    print "  ⚠ Auto-respond needs a Claude subscription token:"
    print "    1. claude setup-token"
    print "    2. Add to ~/.fleet/secrets.json:  \"claude_token\": \"sk-ant-oat01-...\""
    print ""
fi

print "  Fleet memory: ~/.fleet/memory.db (MCP on 127.0.0.1:54321)"
print ""
print "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
