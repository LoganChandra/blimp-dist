---
topic: a2a
audience: the Boss + Duke + Hermes
summary: A2A Broker — agent-to-agent message routing via JSON-RPC 2.0 over HTTP
last_updated: 2026-06-10
---

# A2A Broker

A2A (Agent2Agent Protocol) broker for Blimp. Implements the Linux Foundation
A2A spec v1.0 directly — JSON-RPC 2.0 over HTTP, Agent Cards, task lifecycle.

## Architecture

```
Supervisor (:9092)
    │ POST /hermes/
    ▼
Broker (:9091)
    ├── writes to Postgres (a2a_messages)
    └── notifies agent:
        ├── Duke → tmux send-keys (zero idle tokens)
        └── Hermes → webhook POST (zero idle tokens)
```

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/:agent/.well-known/agent-card.json` | Agent Card (capabilities, skills) |
| POST | `/:agent/` | JSON-RPC `message/send` — create a task |
| POST | `/tasks/:task_id/transition` | Enforced lifecycle transition (P0.6 + P1.5) |
| GET | `/tasks/stale` | Tasks the watchdog must act on (P0.6) |
| GET | `/healthz` | Health check |

## Task lifecycle (P0.6 + P1.5)

States are declared in `postgres/schema.sql`; the broker **enforces** legal
moves between them. The MCP direct-DB write path (`a2a_send` / `a2a_reply`) is
unchanged — `/tasks/:id/transition` is an additional, opt-in enforcement surface
the watchdog and evidence-gated completions use.

```
            ┌──────────────────── canceled ◄──────────────────┐
            │                                                  │
  submitted ──► working ⇄ input-required ──► completed        │
       │           │            │                             │
       └───────────┴────────────┴──────────► failed ──────────┘
                                  ▲
            bounce budget = 2: a completion whose payload fails the
            schema gate is rejected (422) and counts as a bounce; the
            2nd reject auto-transitions the task to `failed` (409) so a
            fabricating counterparty can never livelock.
```

- **Legal transitions** stamp the target state plus a per-transition timestamp
  (`working_at` / `input_required_at` / `completed_at` / `failed_at`).
- **Illegal jumps** (or any move out of a terminal state) → **HTTP 409**.
- **Unknown target state** → **HTTP 400**; **unknown task** → **HTTP 404**.
- **Completion schema gate (P1.5):** `to=completed` requires `result` to match
  `{status, artifacts:[], verification:{cmd, output}}`. Non-conforming →
  rejected (**422**), `bounce_count++`. The `BOUNCE_BUDGET`-th reject (default
  2) auto-fails the task with a `fail_reason` (**409**, `auto_failed: true`).

```bash
# transition a task to working
curl -s localhost:9091/tasks/$TID/transition -d '{"to":"working"}'

# complete with the required evidence envelope
curl -s localhost:9091/tasks/$TID/transition -d '{
  "to":"completed",
  "result":{"status":"done","artifacts":["broker.py"],
            "verification":{"cmd":"python3 -m py_compile broker.py","output":"OK"}}}'
```

### Stale watchdog feed (`GET /tasks/stale`)

Returns root tasks that are `submitted` > 10 min without reaching `working`
(action `renotify`), or `input-required` > 2 h (action `escalate_logan`).
Shaped for a curl+jq one-liner the watchdog polls:

```bash
# task_ids the watchdog should re-fire the Hermes notification for
curl -s localhost:9091/tasks/stale | jq -r '.tasks[] | select(.action=="renotify").task_id'
```

Thresholds and budget are env-tunable: `A2A_STALE_SUBMITTED_SECS` (600),
`A2A_STALE_INPUT_REQ_SECS` (7200), `A2A_BOUNCE_BUDGET` (2).

### Startup migration

The broker runs an **idempotent** migration on boot (`ADD COLUMN IF NOT
EXISTS`) adding the lifecycle bookkeeping columns (`bounce_count`,
`fail_reason`, and the four per-transition timestamps). Safe to re-run on every
restart; existing rows and the live `message/send` flow are untouched.

## Zero-Idle-Token Notifications

The broker never burns agent tokens. When a task arrives for an agent:

- **Duke**: `tmux send-keys -t duke` injects a prompt into his Claude Code session
- **Hermes**: `X-Hub-Signature-256` HMAC-signed POST to Hermes webhook (`:8644/webhooks/a2a-task`)

Agents don't poll. The broker pushes only when there's work.

## Running

```bash
export DUKE_DB_URL="postgresql:///duke_data?host=/var/run/postgresql&port=5433"
export HERMES_WEBHOOK_URL="http://localhost:8644/webhooks/a2a-task"
# BROKER_HERMES_SECRET is injected via EnvironmentFile= from the gitignored
# blimp-infra/secrets/a2a-secret.env (P0.2 — no secret in git). For a one-off
# local run: source that file, e.g. `set -a; . ../secrets/a2a-secret.env; set +a`
cd blimp-infra && ~/blimp/duke-venv/bin/python a2a/broker.py
```

Or via systemd (recommended):
```bash
systemctl --user start a2a-broker
```

## Tests

No live service or DB needed — the broker is exercised via FastAPI `TestClient`
against an in-memory fake of the queries it issues.

```bash
# plain runner (no pytest required)
~/blimp/duke-venv/bin/python a2a/test_broker.py

# or with pytest (vendored in a2a/.pytest-deps)
PYTHONPATH=a2a/.pytest-deps ~/blimp/duke-venv/bin/python -m pytest a2a/test_broker.py -q
```

Coverage: legal/illegal/terminal/unknown transitions, the completion schema
gate, the bounce-budget auto-fail, and the `/tasks/stale` listing.

## Name conflict with a2a-sdk

The `a2a/` directory name conflicts with the `a2a-sdk` PyPI package installed in
duke-venv. Do NOT add `__init__.py` or import as a module — run the file directly:

```bash
python a2a/broker.py          # ✓ works
python -m a2a.broker           # ✗ imports a2a-sdk instead
```