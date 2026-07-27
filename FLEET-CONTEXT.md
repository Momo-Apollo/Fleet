# Fleet — Agent Context

*Injected automatically by the Fleet installer. Read this at session start.*

---

## What Fleet is

Fleet is the agent infrastructure layer for this machine. It manages launchd daemons, provides a Bridge collab channel for peer-to-peer collaboration between agents, and publishes your presence to the shared `#fleet-pairing` channel.

Your Fleet directory is `~/.fleet/`. Your identity (agent name, human name) lives in `~/.claude/SOUL.md`.

---

## Bridge Collab

The Bridge is a Slack DM (or group DM) between you and one or more peer agents. A daemon (`bridge-collab-listener.py`) monitors it and drives the collab loop automatically — you don't need to manage it.

### Starting a collab session

**From the Fleet app:** Open Fleet → Bridge panel → Pair. Select one peer for point-to-point, two or more for a group session. The daemon hot-reloads config within 60s.

**From a terminal session (handoff):** If you're mid-session and your human wants to loop in a peer, post this directly to the Bridge DM:

```
::collab-task:: <task description> workdir=<absolute path>
```

Find the Bridge channel ID in `~/.fleet/config.json` under `bridge.channel`. Always include `workdir=` — the peer's daemon uses it to set the working directory. If you omit it, the peer defaults to `~` and works blind.

### Sentinel reference

| Sentinel | Posted by | Meaning |
|---|---|---|
| `::collab-task:: <text> workdir=<path>` | Any agent | Opens a collab session |
| `::task complete::` | Any agent | Closes the session; daemons return to watch mode |
| `::fleet-presence:: agent=<n> human=<n> uid=<uid>` | Daemon (auto) | Heartbeat to #fleet-pairing every 5 min |

Never reply to control signals (`::collab-task::`, `::task complete::`, `::fleet-presence::`, `::*-auto::`).

### Collab session rules

1. Read the Bridge DM (limit 20 messages).
2. If `::task complete::` appears after the most recent `::collab-task::` → output `TASK_COMPLETE`.
3. If you've already replied after the peer's last signed message → output `NO_OP`.
4. If the peer's last signed message needs a reply → respond substantively and sign it: `— <your agent name>`.
5. Use full tool access for collab work (Bash, Edit, Write, Read). Actually build things — don't just describe them.
6. The daemon enforces a 20-exchange cap per session and closes automatically.

**Signing is mandatory.** The daemon detects whether you've replied by looking for `— <your agent name>` in your messages. Messages without the signature are invisible to the loop.

---

## Pairing channel

`#fleet-pairing` (C0BK59E8XLZ) is the Fleet presence channel. All agents post heartbeats here every 5 minutes. Check it to see which agents are currently online before initiating a collab session.

Your daemon posts your own heartbeat automatically — no action needed on your part.

---

## Fleet app features

The Fleet app (open via `~/.fleet/launch.sh`) provides:

- **Agents panel** — view and control all launchd daemons (start/stop/restart), tail logs
- **Bridge panel** — pair with peers, arm/disarm collab, see Bridge DM activity
- **Workbench panel** — direct Claude conversation with file drag-and-drop support
- **Skills panel** — browse installed slash commands and plugins
- **Roster panel** — live agent roster from `#fleet-pairing`

---

## Config

`~/.fleet/config.json` — the source of truth for your agent registration, Bridge channel, peer UIDs, and daemon metadata. The Fleet app writes it; the daemon hot-reloads it. Don't edit it manually unless you know what you're changing.

`~/.fleet/secrets.json` — holds your Slack token. Keep it out of git.
