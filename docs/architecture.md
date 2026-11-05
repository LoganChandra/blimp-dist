---
topic: architecture
audience: the Boss + Duke + Hermes + Frank + any future agent runtime
summary: The full current system architecture of the Blimp estate — the 3-agent team, the shared brain, the A2A mesh, the autonomous loops, the safety guards, backup/DR, the voice pipeline, and the Foundry doctrine that governs it all.
last_updated: 2026-07-06
---

# Blimp — System Architecture

> **What this is.** The single source of truth for how Blimp actually runs today:
> the AI cofounders, the brain they share, how they talk to each other, the
> background loops that keep them honest and self-maintaining, and the doctrine
> that governs every venture. Companion doc: [`docs/projects.md`](projects.md)
> (the portfolio — every venture/client/tool + the sharing policy).
>
> **Honesty note (C8).** This doc records *reality as of 2026-07-06*, warts
> included. When the ground truth changes,
> update this doc; don't let it become aspirational.

Blimp is two things at once:
1. **An AI-automation agency** for SE Asia SMBs (the strategy/sales/build docs), and
2. **An AI venture studio** — a small team of AI cofounders that research, build,
   ship, and operate software ventures under a shared operating doctrine (the
   "Foundry").

Both are run by the same three-agent team on the same shared brain.

---

## 1. The team — three AI cofounders

| Agent | Role | Runtime | Owns | Surface |
|---|---|---|---|---|
| **Duke** (he/him) | Implementation Lead | Claude Code (this session), tmux `duke` | Delivery, shipping code, running tools, commits, coordination | Discord `#duke` (id `…`) |
| **Hermes** | Technical Lead | Hermes Agent + Qwen3-max via OpenRouter, tmux `hermes` | Architecture, design, tech direction | Discord `#hermes` (id `…`) |
| **Frank** (he/him) | The Operator | Claude Code (workspace `~/nanook/repo`), tmux `nanook` | Steady-state: brain/data/ops, rituals, monitoring, backups, escalations, weekly Boss report | Discord `#nanook`→Frank (id `…`) |

Notes that bite:
- **Frank was renamed from "Nanook" (2026-06-22)** but the *code, tmux session,
  Hermes profile dir (`~/.hermes/profiles/nanook/`), and agent-key are all still
  `nanook`.* Persona = Frank; wiring = `nanook`. Don't "fix" the key — a lot
  keys off it.
- **the Boss** owns product + strategy and is the only human. He reads **Discord
  only** (not the terminal) — every substantive reply, question, or report goes
  through the Discord reply tool. This is rule #0 (see `AGENTS.md`).
- **other-agent** (<your-product>'s agent, `~/other-agent/repo`) is **not** part of Blimp — different
  bot, repo, memory, tmux session. It never shares state with this team.

All three are **peers**, not a hierarchy. Standing rule: Duke does *not* assign
tasks to Hermes/Frank — peer updates and A2A handoffs, not commands.

---

## 2. The shared brain — one memory, three agents

Everything the team knows lives in **one place**, reached through a single MCP
server that all three agents register identically.

```
   Duke (Claude Code)    Hermes (gateway)    Frank (nanook)     LiveKit voice agent
         │                     │                   │                    │
         └──────────┬──────────┴─────────┬─────────┴─────────┬──────────┘
                    │  each spawns its own copy of…          │
              ┌─────▼───────────────────────────────────────▼─────┐
              │        blimp-brain  MCP server  (stdio)            │
              │        blimp-infra/mcp/blimp_brain_mcp.py          │
              │        Python FastMCP · ~27 typed tools            │
              └─────┬───────────────────────────────┬─────────────┘
                    │                               │
        ┌───────────▼──────────┐        ┌───────────▼───────────────┐
        │  Hindsight (Docker)  │        │  WSL Postgres-16 + pgvector│
        │  container :8888     │        │  db `duke_data`  :5433     │
        │  bank `duke`         │        │  6 tables + a view         │
        │  UNSTRUCTURED:       │        │  STRUCTURED:               │
        │  facts, decisions,   │        │  tasks, prospects, quotes, │
        │  doc chunks, chat    │        │  a2a_messages, state, runs │
        └──────────────────────┘        └────────────────────────────┘
```

**blimp-brain MCP** (`blimp-infra/mcp/`) exposes ~27 tools in groups:
- **Memory** — `recall`, `remember`, `log_message`, `recent`, `memory_stats`
- **Beliefs** — `who_believes`, `disagreements`, `agent_beliefs` (layered on Hindsight)
- **Tasks** — `tasks`, `task_add`, `task_update`
- **CRM** — `prospects`, `prospect_add`, `prospect_update`
- **Quotes** — `quote_log`, `quotes`
- **KV state** — `state_get`, `state_set`
- **Activity** — `activity`
- **A2A** — `a2a_send`, `a2a_inbox`, `a2a_reply`, `a2a_complete`, `a2a_pushback`
- **Orchestrator** — `delegate_to_orchestrator`, `orchestrator_status`
- **Discord** — `send_discord` (posts via the `hermes send` CLI so webhook/A2A
  sessions with no terminal can still reach Discord)

**Two backing stores:**
- **Hindsight** — self-hosted MIT-licensed Docker container, bank `duke` (a relic
  name — it's the whole *team's* brain, Hermes + Frank have full read/write).
  4-strategy retrieval (semantic + BM25 + graph + temporal). Holds the
  unstructured stuff. Data volume: `~/blimp/hindsight-data/`.
- **Postgres `duke_data`** — WSL Postgres-16 + pgvector holding structured
  business data. Six tables: `tasks`, `prospects`, `pricing_log`,
  `project_state` (JSONB KV), `a2a_messages`, `orchestrator_runs`, plus a
  `recent_activity` view.

**Ports:** Hindsight `:8888`, Postgres `duke_data` `:5433` (5432 is Hindsight's
own *internal* PG). ⚠️ Older docs (`postgres/README.md`, the bootstrap script,
dated 2026-05-27) still say `:8765` — treat **`:8888` / `:5433` as current**.

**For humans/scripts:** the `duke-mem` and `duke-data` CLIs in `/usr/local/bin/`
hit the same data. **MemPalace is retired** — do not use it.

The MCP layer is the *only* thing that changes if storage migrates to cloud
(Supabase for Postgres, any Docker host for Hindsight); the tool contracts stay
identical.

---

## 3. Inter-agent comms — the A2A mesh

Agents task each other over a **JSON-RPC A2A layer** (Linux Foundation
Agent2Agent v1.0), all backed by the same `duke_data` Postgres.

- **A2A broker** (`blimp-infra/a2a/broker.py`, `127.0.0.1:9091`) — FastAPI +
  JSON-RPC 2.0. Serves Agent Cards, accepts `message/send`, persists to
  `a2a_messages`, enforces the task lifecycle
  (`submitted → working ↔ input-required → completed/failed/canceled`). Illegal
  jumps → 409. A **completion schema gate** requires `{status, artifacts[],
  verification:{cmd,output}}` or returns 422 and bumps `bounce_count`;
  `BOUNCE_BUDGET=2` then auto-fails — so a fabricating agent can't livelock.
  `/tasks/stale` feeds the watchdog.
- **Waking agents (presence-gated "Mailroom", zero idle tokens):** Duke + Frank
  are woken by `tmux send-keys` nudges; Hermes by an **HMAC-signed webhook** POST
  to `:8644`. A doorbell only rings if the recipient is deliverable (presence not
  busy/offline **and** a live tmux pane-busy veto passes, fails closed);
  otherwise a `relay_loop` retries with backoff.
- **Orchestrator** (`blimp-infra/orchestrator/supervisor.py`, `127.0.0.1:9092`) —
  a pure-Python **generator-verifier** supervisor. Loop: Hermes designs → Duke
  verifies → `APPROVED` / `NEEDS_REVISION` (loop) / `BLOCKED_NEEDS_LOGAN`. Hard
  caps: `max_turns=6`, `usd_budget=$1.00`. Callable via `delegate_to_orchestrator`.
- **Discord mirror** (`blimp-infra/a2a/discord_mirror.py`) — read-only visibility:
  posts each A2A state transition into the *recipient's* Discord channel, deduped
  via an `a2a_discord_posts` outbox, storing the returned message id as
  proof-of-delivery. Defaults to **dry-run** unless `MIRROR_LIVE=1`.

All services bind loopback by default (never `0.0.0.0` — rail #5). Ports: broker
**9091**, orchestrator **9092**, Hermes webhook **8644**.

---

## 4. Autonomous background loops

Six unattended loops keep the estate self-monitoring, self-reflecting, and
self-backing-up. Five run as **systemd *user* timers** (deliberately not cron —
CronCreate's 7-day expiry silently kills wakers), all on `Asia/Kuala_Lumpur`.

| Loop | Schedule (KL) | What it does |
|---|---|---|
| **Heartbeat** | every 20 min, 08:00–23:00 | Zero-token bash pulse; ~8 read-only health checks (broker, Hindsight, backup freshness, disk, token-spend, stale tasks). `exit 1` = one Discord line + nudge Duke. A missed slot is itself the alarm. |
| **Overnight** | daily 00:30 | Demand-gated **draft-only** headless shift (`claude -p`, Read/Glob/Grep/Write/Edit only — no Bash/MCP, so sends/pushes/deploys are impossible). Caps: 500k tokens hard, 5 tasks. Drafts → `~/blimp/overnight-out/`. |
| **Dreams** | daily 03:30 | 3-pass reflection: Hindsight consolidate/reflect + failure post-mortem; a curator that *proposes* memory de-dupe diffs (never auto-applies — rail #8); anticipatory prep + a 3-line digest to `#duke`. |
| **Backup** | daily 03:00 | Tier-1 local backup (see §6). Persistent catch-up. |
| **Evals** | Sun 04:00 | Behavioral regression suite (see §7). Read-only. |
| **Forgetting-GC** | *hand-run only* | Report-only decay-candidate lister (never deletes — added after the 2026-06-23 brain-wipe incident). Not on any timer. |

Cross-cutting: every scheduled job upserts a `pulse:<job>` stamp into
`project_state`; the cron-audit + heartbeat turn a silently-dead job into a
visible alert within 26h.

---

## 5. Safety & ops guards

A deterministic guardrail layer that turns `CLAUDE.md` prose rules into
machine-enforced checks.

- **Watchdog** (`blimp-infra/watchdog/lib.sh`) — JSONL-transcript analytics on
  Duke's live session: runaway repeat-loop detection (the "$150-class" detector)
  and token burn. If it sees *other* claude sessions in the same repo dir, the
  mapping is ambiguous, so it **logs but must not kill** (see the
  never-broad-pkill discipline). Incidents → `project_state` + brain mirror.
- **Telemetry** (`watchdog/telemetry.sh`) — per-session/per-model token + est-cost
  JSONL under `~/blimp/telemetry/`. Tokens are ground truth; cost is directional.
- **Cron-audit** (`watchdog/cron-audit.sh`) — "did the scheduled jobs run?" Reads
  all `pulse:*` stamps, flags any expected job missing/stale (>26h). Surfaces the
  gap; deliberately does **not** self-heal (re-firing shared units is
  rail/classifier territory).
- **Claude Code hooks** (`blimp-infra/hooks/`, dormant unless
  `DUKE_CHANNEL=discord`, every parse-failure fails *open*):
  - `stop-log-guard.sh` — blocks turn-end if the turn had no `log_message` call.
  - `pretooluse-prod-guard.sh` — blocks `git push` to main, `git push --force`,
    `vercel --prod`, `supabase db push` (hands the Boss the command instead).
  - `promptsubmit-context.sh` — injects open-task count + recall-first reminder.
  - ⚠️ Installing hooks into `~/.claude/settings.json` is **rail #8** — the Boss
    types the snippet himself.
- **Verification policy** (`blimp-infra/duke/verification-policy.md`) — hard rule:
  **no "done" report to the Boss and no A2A task flipped to completed without a PASS
  verdict from the independent read-only verifier subagent**, stored in the brain
  tagged `evidence,task:<id>`. Exists because Hermes's cheap webhook model has
  fabricated tool calls. "I ran the tests" is a claim; the verifier's captured
  output is evidence.
- **Task contracts** (`blimp-infra/duke/task-contract-template.md`) — every
  delegation gets a 5-field contract *before* work starts (Objective, Output,
  Tools, Boundaries, **Verification commands**). No field 5 → no start.

---

## 6. Backup & disaster recovery

A two-tier **3-2-1** system covering both Hindsight brains, `duke_data`, agent
homes, secrets, Discord state, and Claude memory. *Source code is NOT backed up —
git is the source of truth.*

- **Tier-1 local** (`blimp-infra/backup.sh`, systemd `backup.timer` @ 03:00 KL,
  Persistent) — `pg_dump duke_data` + tar of the Hindsight volume + Discord state
  + Claude memory → `~/blimp/backups/`, 30-day prune, mirrored to `/mnt/c`.
- **Tier-2 offsite / SOTA** (`blimp-infra/blimp-sota-backup.sh`, crontab @ 03:30
  KL) — **restic** encrypted+deduped incremental snapshot of the whole estate →
  an offsite repo. Retention 7 daily / 4 weekly / 6 monthly.
- **Auto-discovery** — brains enumerated from `docker ps | grep hindsight`, agent
  homes globbed by their `CLAUDE.md`, so future agents are backed up with no
  script change.
- **Restore/recover** — `blimp-restore.sh` (restic restore → re-`pg_restore` each
  brain + `duke_data`) and `blimp-recover.sh` (one-command rebuild of a blank
  WSL2 box in 3 lines, with a `--force` guard that refuses to overwrite a live
  brain holding >10 memories). The restic passphrase is the one secret kept out
  of the backup, in the Boss's password manager.

---

## 7. Behavioral eval harness

`evals/` is an **incident-derived regression suite**: 25 YAML cases, each tied to
a real logged incident, replayed weekly against a headless `claude -p` Duke and
grep/jq-scored. Mutating tools are hard-denied so a scheduled run can't touch the
brain/Discord/git/disk.

The cases encode the estate's hard rules — currency-by-market, no
prod-deploy/push-from-Discord, verify-Hermes's-claims,
never-broad-pkill, recall-first, other-agent isolation, no-fabricated-facts/PII.

**Separate brain-quality checks** (`blimp-infra/brain-eval/`, hand-run, not on a
timer): `brain_baseline.py` (deterministic 20-question gate) and `judged_eval.py`
(OpenRouter Gemini LLM-judge).

---

## 8. The voice pipeline

`blimp-infra/voice/` — a **LiveKit Agents** cascade (STT→LLM→TTS) hosting one
"scrum room" where the Boss talks to **both Duke and Hermes** in one session, with a
per-turn `PersonaRouter` that swaps persona prompt *and* Cartesia voice on the fly.

- STT Deepgram nova-3 · LLM Claude Haiku 4.5 · TTS Cartesia sonic-2 (per-persona
  voice ids) · VAD Silero · blimp-brain MCP for recall/remember.
- Token server + mic web UI on `127.0.0.1:8790`.
- **Working prototype, verified (2026-05-30) but not currently running** (last
  real session 2026-06-05). Runs locally in WSL against LiveKit Cloud; a
  self-hosted DigitalOcean SGP1 droplet path is scaffolded (the 3 `livekit-*`
  systemd units) but never deployed. The whole `voice/` tree is untracked in git.

---

## 9. The Foundry doctrine

The venture-building OS (merged from the `an earlier venture` estate 2026-06-16;
the Boss's "bible"). Encoded as Claude Code skills (`.claude/skills/foundry*`) so it
survives model upgrades. **It is stricter than baseline, never looser — it does
not replace Duke's safety rails.**

- **Constitution C1–C8** (outranks every playbook/skill/enthusiasm): C1 no entity
  (Merchant-of-Record rails only); C2 ≥70% gross-margin floor (fat-case modeled);
  C3 distribution-first; C4 solo-buildable *and* solo-operable; C5 the Boss's
  words are verbatim + append-only; C6 self-improvement is structural; C7 UX bar
  (satisfying · clean · straightforward — straightforward wins ties); C8 honesty
  is the brand.
- **Prime rule: "Access ≠ authorization."** Full sudo does not authorize a
  Boss-only action. Precedence: Constitution > registry constraints > venture
  constitutions > playbooks > skills > convenience.
- **5 mechanisms:** (1) lifecycle state machine `SPARK→RESEARCH→VERDICT→FOUNDING→
  BUILD→PROBE→OPERATE/PARK`; (2) drive loop; (3) verification doctrine; (4) estate
  registry; (5) compounding lessons (`L-NNN` ledger).
- **9 granular skills:** critic, deterministic-core, distribution-probe,
  drive-loop, idea-vetting, premium-ux, problem-solving, self-improvement,
  verification.
- **Prime directive for any live venture:** *Correct → current → converting →
  cash-flowing*, in that order. The **deterministic-core** rule is the single
  most important product decision: the LLM never decides anything numerical,
  legal, or binding — it extracts/classifies/explains; authoritative outputs come
  from deterministic code over versioned data. One hallucinated number shipped to
  a user is an extinction-level trust event for an indie product.

---
