---
topic: task-contract-template
audience: Duke + Hermes + any agent delegating or accepting work
summary: The 5-field verification-first task contract — copy this for every delegated task and every nontrivial self-task. Field 5 is what the verifier subagent runs.
last_updated: 2026-07-09
---

# Task contract — 5 fields, declared BEFORE work starts

Every delegation brief (Hermes A2A tasks included) and every nontrivial
self-task gets this contract **before** any work begins. The point is field 5:
machine-checkable end-state assertions written up front, so "done" is a
command output, not a claim. The `verifier` subagent
(`.claude/agents/verifier.md`) runs field 5 mid-loop and at completion;
no completion without its PASS — see
`blimp-infra/duke/verification-policy.md`.

Rules of thumb:
- If you can't write field 5, you don't understand the task yet. Stop and
  spec it.
- Expected results must be comparable: an exit code, a literal value, a
  count, a substring. "Should work" is not an expected result.
- Every command in field 5 must be safe to run repeatedly (read-only or
  pure-check). Destructive verification is a contract bug.
- Commands run from the AUTHORING AGENT'S OWN repo root (`$HOME/blimp/repo`
  for Duke, `$HOME/nanook/repo` for Frank) unless the contract states an
  explicit cwd — matches `.claude/agents/verifier.md`'s rule. The worked example
  below is Frank-authored (his repo root); a Duke- or Hermes-authored contract
  substitutes its own.

## The template (copy from here)

```markdown
# Contract: <task id> — <one-line title>

## 1. Objective
<What end-state exists in the world when this is done. One paragraph max.
Name the client/prospect/call it serves, per the standing demand test.>

## 2. Output format
<Exactly what comes back: files changed (paths), branch name, summary
length, A2A reply shape. Drafts stay drafts — no sends, no pushes.>

## 3. Tools allowed
<Explicit allowlist: which files/dirs may be written, which commands may
run, which MCP tools. Anything not listed is out of bounds.>

## 4. Boundaries
<What NOT to touch: files, services, schemas, other agents' config.
Reference rails/non-negotiables that apply. Note anything that must wait
for a the Boss terminal session.>

## 5. Verification commands (with expected results)
<The declared checks. Each command + its expected result. The verifier
runs these verbatim; all must pass.>
```

---

## Worked example — Frank's Monday data-refresh integrity

# Contract: data-refresh-2026-07-06 — weekly refresh landed + integrity verified

## 1. Objective

Frank's weekly Monday data refresh (per `data-refresh-ritual`) has landed in
the operational store (`duke_data`): this week's pipeline rows are loaded, the
`data_currency:blimp-pipeline` marker in `project_state` carries today's
`asOf`, and the qualification calibration figure is recomputed. Integrity
means no table silently shed rows (the 2026-06-23 brain-wipe failure mode),
every freshness timestamp is today's, and the calibration figure that will
feed Friday's Boss report (this week: 9.0) matches what the rows actually say.
Serves: the "kept current" promise (C8) and honest Boss-report numbers.

## 2. Output format

- Data landed in `duke_data` (rows upserted, `project_state`
  `data_currency:blimp-pipeline` stamped with today's `asOf`) — NO schema
  changes.
- One changelog line drafted for the venture's `data/changelog.json` — draft
  only, no deploy.
- 3-line completion summary + the verifier's verdict block. The Boss-report
  numbers stay drafts until the verifier's PASS (verification-policy).

## 3. Tools allowed

- Writes to `duke_data` go through the `blimp-brain` single-writer path
  (`mcp__blimp-brain__*`) only — never ad-hoc SQL.
- Bash: read-only `psql` SELECTs against `duke_data` (port 5433, unix socket)
  for verification; `mcp__blimp-brain__remember` for the evidence artifact.
- Read/Edit on the venture's `data/changelog.json` only.

## 4. Boundaries

- Do NOT write to `duke_data` via ad-hoc SQL — the single-writer invariant
  holds; `psql` here is read-only.
- Do NOT bulk-delete or bulk-update any table (the 2026-06-23 brain-wipe
  rail). A refresh upserts; it never truncates.
- Do NOT publish any Boss-report number before the verifier's PASS.
- No schema/DDL changes. The `a2a_messages` / `orchestrator_runs` tables are
  retired — not part of any refresh; leave them untouched.

## 5. Verification commands (with expected results)

```bash
cd $HOME/nanook/repo   # Frank's repo root

# V1 — pipeline row count did not collapse (guards the 2026-06-23 wipe mode)
psql --host=/var/run/postgresql --port=5433 -d duke_data -Atc \
  "SELECT count(*) FROM prospects;"
# expected: an integer >= last week's count (50 as of 2026-06-10). A refresh
# upserts, never sheds rows — a lower count is a FAIL, not a pass.

# V2 — freshness: the refresh stamped this week's as-of marker
psql --host=/var/run/postgresql --port=5433 -d duke_data -Atc \
  "SELECT value->>'asOf' FROM project_state WHERE key='data_currency:blimp-pipeline';"
# expected: prints the refresh date, 2026-07-06. An older date or empty
# result = stale / not-run = FAIL.

# V3 — no active pipeline row left stale (every open row touched this week)
psql --host=/var/run/postgresql --port=5433 -d duke_data -Atc \
  "SELECT count(*) FROM prospects
   WHERE stage NOT IN ('won','lost','parked') AND updated_at::date < DATE '2026-07-06';"
# expected: prints exactly 0

# V4 — calibration: the figure quoted in field 1 / the Boss report matches data
psql --host=/var/run/postgresql --port=5433 -d duke_data -Atc \
  "SELECT round(avg(qualification_score),1) FROM prospects WHERE stage='qualified';"
# expected: prints exactly 9.0 (the calibration figure named in field 1). A
# mismatch means the report number is wrong — FAIL.
```
