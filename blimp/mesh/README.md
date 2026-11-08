# Blimp Crew v2 — mesh implementation

Implementation of `blimp-v2-spec.md` (v0.1, 2026-09-28, by Meera). This is the **Phase 1–2 scope**: the deterministic kernel, Toolgate, the prompt stack, schemas, and policies. No LLM inside any kernel component.

## What's here

| Path | What | Spec section |
|---|---|---|
| `migrations/0001_mesh_schema.sql` | The `mesh` schema — 16 tables, append-only events with hash chain, principals seed | §15 |
| `deck/events.py` | Hash-chained event store (`sha256(prev‖canonical)`) + weekly chain verify | §10.1, §14.7 |
| `deck/contract_lint.py` | 7-check contract lint — a task cannot be READY until it passes | §9.3 |
| `deck/receipts_gate.py` | The receipts gate: DONE without valid receipts is rejected by code, not judgment | §9.11.2 |
| `deck/envelope.py` | A2A envelope + speech-act validation, anti-chatter (no acks), size caps | §8.3–8.11 |
| `deck/router.py` | Wake rules: events → prioritized, coalesced TurnRequests; INFORM/STATUS never wake | §8.6 |
| `deck/priority.py` | Deterministic priority score (LLMs propose inputs; code computes) | §9.6 |
| `deck/ballast.py` | Budgets, loop breaker, kill-switch spike detection — all deterministic (L-024) | §14.5 |
| `deck/api.py` | FastAPI kernel service (port 9100) + A2A facade endpoint | §10.1, §8.2 |
| `toolgate/gate.py` | Toolgate: identity, authority matrix, taint, approval tokens (single-use, action-hash-bound), receipts for every call incl. denials, circuit breakers, other-agent/personal deny list | §13 |
| `context/compiler.py` | Cache-ordered, budgeted context packs + the load manifest (fixes L-017/F3) | §11.6 |
| `prompts/L0–L2/`, `prompts/services/` | The prompt stack as code: constitution, 3 identities, protocol card, all 9 service prompts | §12 |
| `policies/*.yaml` | models, budgets, wake rules, authority — all R4, the Boss applies | §7.6, §14.5 |
| `schemas/*.json` | JSON Schema 2020-12: speech-act, contract v2, handoff, completion, verification, turn-request | §16 |
| `tests/` | spec-conformance + API tests (311 collected 2026-11-06; run them, don't trust this count) | whole spec |

## Run the tests

```bash
# one-time: uv venv ~/blimp/mesh-venv && uv pip install --python ~/blimp/mesh-venv/bin/python psycopg pytest fastapi uvicorn
~/blimp/mesh-venv/bin/pytest blimp/mesh -q   # uses the isolated blimp_mesh_test DB
```

## Apply the schema (requires nothing else running)

```bash
psql -p 5433 -d duke_data -f blimp/mesh/migrations/0001_mesh_schema.sql
```

## Not yet implemented (later phases per §23)

- DBOS durable workflows (turn/huddle/verification/approval workflows) — Phase 3
- Discord Bridge (thread-per-task, cards, slash commands) — Phase 1 backend only
- Agent hosts (Claude Agent SDK) + Hermes adapter — Phase 3
- Brain v2 bank restructure + knowledge pages — Phase 5
- Observatory (OTel + Phoenix), Proving Ground CI gates, Bridge Console — Phase 6
- VPS move — Phase 7

## Gate 0 status

Status 2026-11-06: `blimp-bridge` and `blimp-deck` run as services from `~/blimp/mesh-live` (see docs/architecture.md); the rest below is not deployed. Per spec §0, the DR-7 infrastructure freeze runs to ~2026-10-11;
D-008 (kernel supersedes D-006) needs the Boss's ratification before this runs as a service.
The code and tests above are the buildable artifact awaiting those decisions (§25 G-1..G-15).
