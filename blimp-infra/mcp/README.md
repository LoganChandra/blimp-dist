---
topic: blimp-mcp
audience: Duke + Hermes + any future agent runtime
last_updated: 2026-05-27
---

# Blimp Team Brain — MCP Server

A single MCP server (`blimp-brain`) exposing the team's memory + business
data as typed tools. Both Duke (Claude Code) and Hermes (Hermes Agent)
register it; both speak the same tool contracts.

This is the canonical way agents touch the team brain. Humans can still
use `duke-mem` / `duke-data` CLIs directly from a shell.

## Tools exposed

**Long-term memory (Hindsight bank `duke`):**
- `recall(query, limit, tags, budget)` — semantic search
- `remember(content, tags, source, context)` — save a fact / decision
- `log_message(channel, speaker, message, user)` — log a chat turn
- `recent(channel, limit)` — recent conversation log entries
- `memory_stats()` — health + counts

**Structured business data (Postgres `duke_data`):**
- `tasks(status, assigned_to, limit)` / `task_add(...)` / `task_update(...)`
- `prospects(stage, limit)` / `prospect_add(...)` / `prospect_update(...)`
- `quote_log(prospect_id, amount, currency, inputs_json, ...)` / `quotes(...)`
- `state_get(key)` / `state_set(key, value_json)`
- `activity(limit)` — cross-table recent activity feed

**Inter-agent comms (A2A, Postgres-backed):**
- `a2a_send(to, message, skill, parent_task_id)` / `a2a_inbox(state, limit)`
- `a2a_reply(task_id, message, state)` / `a2a_complete(task_id, result)` / `a2a_pushback(task_id, reason, alternative)`
- `a2a_contract(to, objective, output, boundaries, stop_condition, check_command)`: a hand-off with the 5-field contract; a missing field is refused
- `idempotency_key` (optional) on `a2a_send`, `a2a_contract`, `send_discord`, `board_post`: a repeat call with the same key within 24 h returns the first result instead of acting twice
- `reflect(query, budget)`: the brain reasons over what it knows and writes a sourced answer. Slow (30-60 s), costs one cheap model call; on demand only, never in a loop
- `send_discord(channel, message)` — post to a Discord channel via the
  `hermes send` CLI. Works in restricted (webhook/A2A) sessions that have
  **no terminal**, so it's the agents' proactive Discord-send path. Channel
  accepts an alias (`duke`, `hermes`, `general`, `ops` = #frank), a raw channel ID, or `#channel-name`. Returns JSON
  `{"ok": true|false, ...}` — the real subprocess result, so a failed send
  is reported honestly rather than silently swallowed.

## Registering

### Hermes Agent

```bash
hermes mcp add blimp-brain \
  --command $HOME/blimp/duke-venv/bin/python \
  --args $HOME/blimp/repo/blimp-infra/mcp/blimp_brain_mcp.py
hermes mcp list
hermes mcp test blimp-brain
```

> **CRITICAL — set `A2A_AGENT_NAME` per agent.** The A2A tools key the inbox
> off `SELF_AGENT = os.environ.get("A2A_AGENT_NAME", "duke")`. Hermes's
> `blimp-brain` block in `~/.hermes/config.yaml` **must** set this, or Hermes
> reads Duke's inbox and sends tasks tagged `duke`:
> ```yaml
>   blimp-brain:
>     command: $HOME/blimp/duke-venv/bin/python
>     args:
>     - $HOME/blimp/repo/blimp-infra/mcp/blimp_brain_mcp.py
>     enabled: true
>     env:
>       A2A_AGENT_NAME: hermes
> ```
> Restart his gateway (`systemctl --user restart hermes-gateway`) after changes.
> Duke's side correctly defaults to `duke`.

### Claude Code (Duke)

Create `.mcp.json` at the repo root or merge into `~/.claude.json`:

```json
{
  "mcpServers": {
    "blimp-brain": {
      "command": "$HOME/blimp/duke-venv/bin/python",
      "args": ["$HOME/blimp/repo/blimp-infra/mcp/blimp_brain_mcp.py"]
    }
  }
}
```

Restart Claude Code to pick up the new MCP server.

## Standalone test

```bash
$HOME/blimp/duke-venv/bin/python \
  $HOME/blimp/repo/blimp-infra/mcp/blimp_brain_mcp.py
```

(Reads JSON-RPC on stdin, writes on stdout — same protocol Hermes and
Claude Code use to call it. Ctrl-C to exit.)

## Architecture

```
            ┌─────────────────────────┐
   Duke ───▶│   blimp-brain MCP       │◀─── Hermes
            │   (this directory)      │
            └─────────────────────────┘
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
  ┌────────────────┐    ┌──────────────────────┐
  │ hindsight_     │    │ duke_memory.py       │
  │ client.py      │    │ (Postgres business   │
  │ (Hindsight     │    │  data: tasks /       │
  │  HTTP API)     │    │  prospects / quotes / │
  │                │    │  state)              │
  └────────────────┘    └──────────────────────┘
            │                     │
            ▼                     ▼
  ┌────────────────┐    ┌──────────────────────┐
  │ Hindsight      │    │ WSL Postgres-16      │
  │ container      │    │ port 5433 (5432 is   │
  │ port 8888      │    │  Hindsight's own pg) │
  └────────────────┘    └──────────────────────┘
```

The MCP server is the only thing that needs to change when the underlying
storage changes (cloud migration, schema evolution). Both agents keep
calling the same MCP tools.

## Dependencies

- Python venv at `$HOME/blimp/duke-venv`
- `mcp` Python SDK (FastMCP) — see `requirements.txt`
- `hindsight_client.py` and `duke_memory.py` in `../postgres/`
