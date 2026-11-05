---
topic: blimp-infra
audience: the Boss + Duke + Hermes + Frank + any future agent runtime
summary: Three-agent infrastructure — Duke (Implementation Lead, Claude Code) + Hermes (Technical Lead, Hermes Agent + Qwen3-max) + Frank (The Operator). Shared brain via blimp-brain MCP. Full system architecture in ../docs/architecture.md.
last_updated: 2026-07-06
---

# blimp-infra — Duke + Hermes + Frank deployment scripts

Infrastructure for Blimp's three AI cofounders (Duke, Hermes, Frank), running
side-by-side in WSL2. All serve Discord. All share one brain.

> **The complete, current system architecture — brain, A2A mesh, autonomous
> loops, safety guards, backup/DR, voice, and the Foundry doctrine, plus a dated
> health snapshot — lives in [`../docs/architecture.md`](../docs/architecture.md).**
> This README is the deployment/ops reference; that doc is the map.

## Architecture

```
                   ┌───────────────────┐
                   │  Discord (server) │
                   └─────────┬─────────┘
                             │
        ┌────────────────────┴────────────────────┐
        │                                         │
   ┌────▼──────┐                            ┌─────▼─────┐
   │   Duke    │   Implementation Lead      │  Hermes   │   Technical Lead
   │ (Claude   │   ◄── coordination ──►     │ (Hermes   │   ◄── architecture ──►
   │  Code)    │       shipping             │  Agent)   │       tech direction
   │  tmux:    │                            │  tmux:    │
   │  duke     │                            │  hermes   │
   └────┬──────┘                            └─────┬─────┘
        │                                         │
        └──────────────┬──────────────────────────┘
                       │  MCP (stdio)
              ┌────────▼──────────┐
              │  blimp-brain MCP  │
              │  server           │
              │  (blimp-infra/mcp/) │
              └────────┬──────────┘
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
┌──────────────┐              ┌─────────────────┐
│  Hindsight   │              │ WSL Postgres-16 │
│  container   │              │ (port 5433 —    │
│  port 8888   │              │  Hindsight      │
│  bank: duke  │              │  squats 5432)   │
│              │              │ db: duke_data   │
└──────────────┘              └─────────────────┘
   semantic memory               structured business data
   facts/decisions/              tasks/prospects/quotes/
   docs/conversations            project_state
```

Both agents call the same `blimp-brain` MCP tools. Whatever Hermes writes,
Duke can recall, and vice versa. Naming is a relic (everything is called
"duke" because the stack was built before Hermes joined the team) — the
brain is the team's, not Duke's alone.

## Bootstrap order (fresh WSL2 Ubuntu box)

```bash
# 1. Memory stack first (Hindsight + Postgres + duke-mem/duke-data CLIs + blimp-brain MCP)
bash blimp-infra/postgres/bootstrap-duke-memory.sh

# 2. Duke himself (Claude Code wired into Discord)
bash blimp-infra/boot-text-blimp.sh

# 3. Hermes second (must be after step 1 — Hermes needs blimp-brain to exist)
bash blimp-infra/bootstrap-hermes.sh

# 4. A2A infrastructure (broker + orchestrator + webhook)
bash blimp-infra/bootstrap-a2a.sh
```

Each is idempotent. Re-running any of them is safe.

You'll also need (one-time, in the Boss's browser, not scriptable):
- Two Discord applications in https://discord.com/developers/applications:
  one named "Duke", one named "Hermes". Each needs Message Content +
  Server Members intents enabled, and is invited to your Blimp server
  with scopes `bot` + `applications.commands`.

## Files in this directory

| File | Purpose |
|---|---|
| `duke-system-context.md` | Duke's persona, role (Implementation Lead), commitments. Symlinked into Duke's runtime. |
| `hermes-system-context.md` | Hermes's persona, role (Technical Lead), commitments. Symlinked to `~/.hermes/SOUL.md` by bootstrap. |
| `boot-text-blimp.sh` | Starts Duke (Claude Code) in tmux session `duke`. |
| `bootstrap-hermes.sh` | End-to-end Hermes install: OpenRouter, SOUL, MCP, adapter patch, gateway. |
| `boot-blimp.bat` | Windows entry — launches WSL → boot-text-blimp.sh |
| `setup-autostart-blimp.bat` | Registers Task Scheduler entry `DukeAutostart` |
| `restart-blimp.bat` | Kills + restarts Duke's tmux session |
| `inspect-blimp.bat` | Dump tmux pane + process list for debugging |
| `access-update-blimp.sh` | Updates Duke's Discord access allowlist |
| `install-blimp.sh` | (Legacy) original Duke installer, pre-Hermes |
| `migrate-blimp.sh` | (Legacy) tarball the install for transport |
| `.env.example` | Template for `~/blimp/.env` (Discord bot token, paths) |
| `postgres/` | Memory stack — Hindsight + Postgres + duke-mem/duke-data CLIs |
| `mcp/` | blimp-brain MCP server (the canonical tool surface) |
| `patches/` | Workarounds for upstream bugs (currently: Hermes Discord adapter) |
| `a2a/` | A2A broker — JSON-RPC 2.0 message routing + agent notifications (tmux/webhook) |\n| `orchestrator/` | Generator-Verifier supervisor loop — Hermes designs, Duke verifies |\n| `systemd/` | Systemd units for all services (a2a-broker, orchestrator, hermes-gateway) |\n| `bootstrap-a2a.sh` | One-shot A2A infrastructure bring-up — webhook, broker, orchestrator |\n| `secrets/` | API keys (gitignored, mode 600) |

## Tmux session map

| Session | Process | Project | Discord bot |
|---|---|---|---|
| `duke` | Claude Code with `--channels` | Blimp | Duke |
| `hermes` | Hermes Agent gateway | Blimp | Hermes |
| `nanook` | Hermes Agent profile `nanook` (persona **Frank**, The Operator) | Blimp | Frank |
| `other-agent` | Claude Code (<your-product> project) | <your-product> (~/other-agent/) | other-agent (different Discord) |

Each runs in isolation — different Discord state dirs, different memory
stores, different bot tokens. Touching one never affects the others.

## Memory operations (humans)

```bash
# Long-term semantic memory (via Hindsight)
duke-mem recall "pricing for SG med spas"
duke-mem remember --content "..." --tags decision,pricing
duke-mem log --user logan --channel hermes --speaker logan --message "..."
duke-mem recent --channel hermes --limit 10
duke-mem ingest-repo                # re-ingest after meaningful repo changes
duke-mem stats

# Structured business data (via Postgres)
duke-data tasks --status in_progress
duke-data task-add --title "..." --priority P1
duke-data prospects --stage discovery
duke-data prospect-add --company-name "..." --industry "..." --country SG
duke-data quote-log --prospect-id 3 --amount 8000 --currency SGD --inputs '{}'
duke-data state-get current_focus
duke-data activity                  # cross-table recent activity feed
```

Agents use the MCP tools (`mcp__blimp-brain__recall`, etc.) — same data, typed interface.

## Discord channels (Blimp server)

| Channel | Purpose |
|---|---|
| `#general` | Banter, status, off-topic |
| `#duke` | Duke's channel — talk to him directly without @mention |
| `#hermes` | Hermes's channel — talk to him directly without @mention |
| `#prospects` | Lead intake, cost analyses |
| `#builds` | Client build help, n8n debugging |
| `#ops` | Admin, scheduling, client maintenance |

Channel routing config:
- **Hermes**: `DISCORD_ALLOWED_CHANNELS` + `DISCORD_FREE_RESPONSE_CHANNELS` in `~/.hermes/.env`
- **Duke**: Channels plugin pairing flow — `!pair` in the channel, then `/discord:access pair <CODE>` in Duke's CLI

## Stopping / restarting

```bash
tmux ls                          # see all sessions
tmux attach -t duke              # watch Duke (Ctrl-b d to detach)
tmux attach -t hermes            # watch Hermes (Ctrl-b d to detach)

# Restart Duke
tmux kill-session -t duke; bash blimp-infra/boot-text-blimp.sh

# Restart Hermes
tmux kill-session -t hermes
tmux new -d -s hermes 'export PATH="$HOME/.local/bin:$PATH"; set -a; source ~/.hermes/.env; set +a; hermes gateway 2>&1 | tee -a ~/.hermes/logs/gateway-blimp.log'

# Or just re-run the Hermes bootstrap (idempotent)
bash blimp-infra/bootstrap-hermes.sh
```

From Windows: `restart-blimp.bat` for Duke. (No Windows shortcut for Hermes yet — file an issue if you want one.)

## After upstream updates

When you run `hermes update`, the Discord adapter patch in
`patches/patch-hermes-discord-adapter.sh` gets wiped. **Re-apply** it:

```bash
bash blimp-infra/patches/patch-hermes-discord-adapter.sh
```

See `patches/README.md` for the full story on the bug we're working around.

## Coexistence with <your-product> (other-agent)

<your-product>'s other-agent is **not** part of Blimp:
- Different repo (`~/other-agent/repo/`)
- Different tmux session (`other-agent`)
- Different Discord state dir (`~/.claude/channels/discord/` vs `~/blimp/discord-state/`)
- Different memory store (<your-product>'s own MemPalace wing vs Blimp's Hindsight bank)
- Different Discord bot

Each Task Scheduler entry boots its own session. They don't interfere.

## Inter-agent comms: A2A protocol

Duke and Hermes communicate via **A2A** (Agent2Agent Protocol, Linux Foundation,
JSON-RPC 2.0 over HTTP). A broker (`:9091`) routes messages; an orchestrator
(`:9092`) runs a Generator-Verifier loop (Hermes designs, Duke verifies).

- **Broker** — writes to Postgres, then pushes notifications to agents (zero idle tokens)
- **Orchestrator** — supervisor loop with hard caps (turn count, USD budget)
- **Webhook** — Hermes wakes on signed POST (`:8644/webhooks/a2a-task`)
- **Duke** — woken via `tmux send-keys`

See `a2a/README.md` and `orchestrator/README.md` for the full spec.
