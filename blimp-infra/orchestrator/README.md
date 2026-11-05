---
topic: orchestrator
audience: the Boss + Duke + Hermes
summary: Generator-Verifier supervisor — orchestrates Hermes (design) and Duke (verify) via A2A
last_updated: 2026-05-28
---

# Orchestrator

Generator-Verifier supervisor loop. Orchestrates Blimp's two AI cofounders:
Hermes designs, Duke verifies, loop until APPROVED or budget/time exhausted.

## Why not LangGraph SDK

Chose pure Python over LangGraph SDK. Reasons:
- LangGraph SDK API churn on Python 3.14
- The Gen-Verifier pattern is ~40 lines of explicit logic — simpler than a graph
- LangGraph would add checkpoint overhead without value at this scale

## Flow

```
the Boss task
    ↓
Supervisor sends to Hermes (design)
    ↓ polls Postgres for reply (600s timeout)
Hermes responds with design
    ↓
Supervisor sends to Duke (verify)
    ↓ polls Postgres for reply
Duke responds: APPROVED / NEEDS_REVISION / BLOCKED_NEEDS_LOGAN
    ↓
APPROVED → END (result saved to orchestrator_runs)
NEEDS_REVISION → loop back to Hermes with feedback
BLOCKED_NEEDS_LOGAN → END (escalation)
```

## Hard caps

| Cap | Default | Env override |
|-----|---------|--------------|
| max_turns | 6 | `ORCH_MAX_TURNS` |
| usd_budget | $1.00 | `ORCH_USD_BUDGET` |
| poll_timeout | 600s | `ORCH_POLL_TIMEOUT` |

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/orchestrate` | Start a new orchestration |
| GET | `/orchestrate/{thread_id}` | Poll status/result |
| GET | `/healthz` | Health check |

## API

```bash
# Start a task
curl -X POST http://localhost:9092/orchestrate \
  -H "Content-Type: application/json" \
  -d '{"task":"Design a webhook receiver","max_turns":2,"usd_budget":0.50}'

# Poll result
curl http://localhost:9092/orchestrate/<thread_id>
```

Or via MCP (from Duke or Hermes):
```python
thread_id = mcp__blimp-brain__delegate_to_orchestrator(
    task="Design a webhook receiver for client X",
    max_turns=2,
    usd_budget=0.50
)
result = mcp__blimp-brain__orchestrator_status(thread_id)
```