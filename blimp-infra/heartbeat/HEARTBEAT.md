---
topic: heartbeat-registry
audience: Duke (curates it) + heartbeat.sh (parses it) + the Boss (reviews diffs)
last_updated: 2026-06-10
---

# HEARTBEAT.md — Duke's self-extending watch-item registry

`heartbeat.sh` (same dir) parses the table below every run (every 20 min,
08:00–23:00 KL) and executes each non-expired check. **No LLM is involved** —
a check is a bash exit code, and only a FIRE produces a Discord line + a tmux
nudge. Nothing wrong → total silence.

**Who edits this file:** Duke appends/retires rows as he learns what's worth
watching; the git diff is what the Boss reviews (it surfaces in the morning
digest). This is the agent curating its own attention — the counters keep it
honest.

## Registry format (STRICT — heartbeat.sh parses this)

- Rows live between the `<!-- registry:start -->` and `<!-- registry:end -->`
  markers. Only the first table between the markers is read; the first two
  `|` rows (header + separator) are skipped.
- Exactly **6 columns**: `item | check_cmd | threshold | helped_count | harmed_count | expires`.
- **item** — unique kebab-case name. Duplicates are a parse error.
- **check_cmd** — either `builtin:<name>` (a `check_<name>` function in
  heartbeat.sh, dashes become underscores) or a raw shell command run via
  `timeout 30 bash -c '<cmd>'`. Contract for every check:
  - exit **0** = OK (silent), exit **1** = FIRE, exit **2** = SKIP (no
    data / dependency not deployed — degrade gracefully, never FIRE for
    missing optional infra).
  - On FIRE, the **last stdout line** becomes the alert reason — keep it one
    short line.
  - Must be **read-only**, finish in **<30s**, and make **no LLM calls**.
  - **No `|` characters in the cell** (markdown table limitation — the parser
    splits on pipes). If a check needs a pipe, put it in a small script and
    reference the script path.
- **threshold** — human-readable description of when it fires. Documentation
  only; the command itself encodes the real logic.
- **helped_count / harmed_count** — integers, edited by **Duke** when he
  reviews an alert's outcome: alert led to a real fix → +1 helped; false
  alarm / noise → +1 harmed. heartbeat.sh never edits them — judging
  usefulness is judgment work.
- **expires** — `YYYY-MM-DD` or `never`. Past-dated rows are **not run**;
  they're flagged as retire-candidates in the digest queue. `never` is
  reserved for core-infra checks (brain, backups, disk, pulses).
  Demand-driven or experimental items get a date ~90 days out; renew the
  date if the item earns its keep.

**Retirement rule:** a row is flagged for retirement (one dedup'd line in
`~/blimp/heartbeat/digest-queue.txt`, drained by the morning digest) when it
is expired, **or** when `harmed_count > helped_count` with ≥5 total reviews.
Flagged rows still need a human-visible diff to actually leave the table —
Duke deletes the row, the diff shows up in digest review.

Example of a custom (non-builtin) row Duke might append — real logic wrapped
in a small script under `blimp-infra/heartbeat/checks/` (keeps the cell
pipe-free and the check testable on its own):

```
| broker-fatal-log | bash ~/blimp/repo/blimp-infra/heartbeat/checks/broker-fatal.sh | FATAL lines in broker log within 24h | 0 | 0 | 2026-09-08 |
```

## The registry

<!-- registry:start -->

| item | check_cmd | threshold | helped_count | harmed_count | expires |
|---|---|---|---|---|---|
| hindsight-health | builtin:hindsight-health | GET 127.0.0.1:8888/health answers within 5s (bank duke) | 0 | 0 | never |
| hermes-webhook-port | builtin:hermes-webhook-port | TCP connect to 127.0.0.1:8644 within 3s | 0 | 0 | never |
| backup-freshness | builtin:backup-freshness | duke_data-*.sql.gz AND hindsight-*.tar.gz in ~/blimp/backups newer than 26h | 0 | 0 | never |
| disk-space | builtin:disk-space | use% < 90 on $HOME (ext4 vhdx) and /mnt/c | 0 | 0 | never |
| telemetry-anomaly | builtin:telemetry-anomaly | today's token spend <= 3x mean of last 7 daily totals (SKIP if <3 days of history) | 0 | 0 | 2026-09-30 |
| pulse-stamps | builtin:pulse-stamps | every pulse:% key in project_state updated within 26h (missed-pulse detection, P0.0f) | 0 | 0 | never |

<!-- registry:end -->

## Telemetry contract

`builtin:telemetry-anomaly` reads **one plain-integer file per day**:

```
~/blimp/telemetry/daily/YYYY-MM-DD.tokens     # e.g. "184223\n"
```

Writers add to today's file (read int, add, write back — see
`overnight.sh`, which self-reports its spend there). The P1.7 watchdog
telemetry job is the main writer once it lands; until ≥3 days of history
exist the check SKIPs, it never guesses.

## Pulse-stamp convention

Every scheduled job upserts `project_state` key `pulse:<job>` with value
`{"ts": "<ISO8601>"}` on each successful run (the row's `updated_at` is the
authoritative stamp time). Known stamps: `pulse:heartbeat` (this script),
`pulse:overnight` (overnight.sh); Dreams and backup add theirs as they land.
`builtin:pulse-stamps` fires on any stamp older than 26h — a silently dead
job becomes visible within one heartbeat tick.
