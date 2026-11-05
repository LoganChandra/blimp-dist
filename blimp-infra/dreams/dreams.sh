#!/usr/bin/env bash
# dreams.sh — Duke Dreams (duke-v2 P1.1): nightly reflective + anticipatory
# pass, demand-gated. Runs at 03:30 Asia/Kuala_Lumpur via systemd user timer
# (blimp-infra/systemd/user/dreams.timer) — NOT cron: CronCreate's 7-day
# expiry kills wakers silently (duke-v2-plan P1.1).
#
# What it does, in order:
#   0. Demand gate   — counts today's conversation turns (Hindsight) +
#                      completed tasks + A2A activity (duke_data). Quiet day →
#                      one log line, pulse stamp, exit 0. No tokens on nothing.
#   1. Reflector     — Hindsight /consolidate then /reflect (server-side LLM,
#                      budgeted), then ONE headless `claude -p` writing
#                      post-mortem case notes for today's failed/stuck A2A
#                      tasks → dreams-out/ + blimp-brain via `duke-mem remember`.
#   2. Curator       — separate `claude -p`: dedupe/contradiction-check the
#                      last 36h of auto-memory writes; PROPOSES a unified diff
#                      under dreams-out/. Never auto-applied (rail #8 — Duke
#                      reviews it in the morning).
#   3. Anticipatory  — separate `claude -p`: tomorrow's calendar (if a source
#                      exists) + open tasks + active prospects → per-item prep
#                      packs under dreams-out/ + a 3-line digest.
#   4. Digest        — posts the 3-liner to #duke via the same `hermes send`
#                      path blimp-brain's send_discord tool shells out to.
#   5. Pulse         — stamps pulse:dreams in project_state (the watchdog
#                      cron-audit checks this gap for missed-pulse detection).
#
# Budgets: DREAMS_TOKEN_BUDGET derives --max-turns per pass; --max-budget-usd
# is a hard dollar stop per pass; measured cost lands in the pulse stamp.
#
# Modes:
#   DRY_RUN=1 bash dreams.sh        # print planned actions, no claude/LLM
#                                   # calls, no writes — safe to run anytime
#   touch ~/blimp/dreams.disabled   # kill switch, no systemd needed
#
# Idempotent. Safe to re-run (re-run overwrites today's dreams-out files).
# set -e deliberately off, matching backup.sh/watchdog.sh: one failed pass
# must not kill the rest of the night — every external call is checked.

set -uo pipefail
export TZ=Asia/Kuala_Lumpur

# ------------------------------------------------------------------
# Config (env-overridable)
# ------------------------------------------------------------------
DREAMS_LOG="${DREAMS_LOG:-$HOME/blimp/logs/dreams.log}"
DREAMS_OUT_ROOT="${DREAMS_OUT_ROOT:-$HOME/blimp/dreams-out}"
DREAMS_MIN_ACTIVITY="${DREAMS_MIN_ACTIVITY:-5}"     # turns+tasks+a2a below this → skip
DREAMS_TOKEN_BUDGET="${DREAMS_TOKEN_BUDGET:-60000}" # whole-night token budget (conservative)
DREAMS_MAX_USD_PER_PASS="${DREAMS_MAX_USD_PER_PASS:-1.50}" # hard $ stop per claude pass
DREAMS_PASS_TIMEOUT="${DREAMS_PASS_TIMEOUT:-600}"   # seconds per claude pass
DREAMS_REFLECT_BUDGET="${DREAMS_REFLECT_BUDGET:-low}" # Hindsight budget: low|mid|high
DREAMS_MODEL="${DREAMS_MODEL:-}"                    # empty = claude CLI default
DREAMS_RETAIN_DAYS="${DREAMS_RETAIN_DAYS:-30}"      # prune dreams-out dirs older than this
DREAMS_CALENDAR_DIR="${DREAMS_CALENDAR_DIR:-$HOME/blimp/calendar}" # P1.4 will populate
DREAMS_DISCORD_CHANNEL="${DREAMS_DISCORD_CHANNEL:-<ID>}" # #duke

HINDSIGHT_URL="${HINDSIGHT_URL:-http://127.0.0.1:8888}"
DUKE_BANK="${DUKE_BANK:-duke}"
BANK_API="$HINDSIGHT_URL/v1/default/banks/$DUKE_BANK"
PG_PORT="${PG_PORT:-5433}"                          # WSL postgres; Hindsight squats 5432

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}" # same path mcp/blimp_brain_mcp.py uses
DUKE_MEM="${DUKE_MEM:-/usr/local/bin/duke-mem}"
DUKE_DATA="${DUKE_DATA:-/usr/local/bin/duke-data}"
AUTO_MEM_DIR="${AUTO_MEM_DIR:-$HOME/.claude/projects/-home-USER-blimp-repo/memory}"

DRY_RUN="${DRY_RUN:-0}"
TODAY="$(date +%Y-%m-%d)"
TOMORROW="$(date -d '+1 day' +%Y-%m-%d)"
OUT_DIR="$DREAMS_OUT_ROOT/$TODAY"
PROMPTS_DIR="$OUT_DIR/prompts"

# --max-turns per pass, derived from the night budget: 3 passes, ~8k tokens
# per turn heuristic, clamped to [2,10]. Passes run tool-less (single-turn in
# practice) so this is a ceiling, not a target.
MAX_TURNS=$(( DREAMS_TOKEN_BUDGET / 3 / 8000 ))
[ "$MAX_TURNS" -lt 2 ]  && MAX_TURNS=2
[ "$MAX_TURNS" -gt 10 ] && MAX_TURNS=10

mkdir -p "$(dirname "$DREAMS_LOG")"

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$DREAMS_LOG"; }
warn() { log "WARN: $*"; }

# ------------------------------------------------------------------
# Kill switch + single-instance lock
# ------------------------------------------------------------------
if [ -f "$HOME/blimp/dreams.disabled" ]; then
  log "disabled via ~/blimp/dreams.disabled — exiting (rm the file to re-enable)"
  exit 0
fi

LOCKFILE="$HOME/blimp/dreams.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "another dreams run holds $LOCKFILE — exiting"
  exit 0
fi

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
pg_count() { # $1 = SQL returning a single count
  local n
  n=$(psql --host=/var/run/postgresql --port="$PG_PORT" duke_data -tAc "$1" 2>/dev/null) || n=""
  n=$(printf '%s' "$n" | tr -dc '0-9')
  printf '%s' "${n:-0}"
}

count_turns_today() { # conversation-tagged Hindsight memories dated today (KL)
  local raw
  raw=$(curl -sf --max-time 30 "$BANK_API/memories/list?limit=500" 2>/dev/null) || raw=""
  if [ -z "$raw" ]; then printf '0'; return; fi
  printf '%s' "$raw" | python3 -c '
import json, sys, datetime
try:
    items = json.load(sys.stdin).get("items", [])
except Exception:
    print(0); raise SystemExit
kl = datetime.timezone(datetime.timedelta(hours=8))
today = datetime.datetime.now(kl).date()
n = 0
for m in items:
    if "conversation" not in (m.get("tags") or []):
        continue
    try:
        d = datetime.datetime.fromisoformat(str(m.get("date"))).astimezone(kl).date()
    except Exception:
        continue
    if d == today:
        n += 1
print(n)' 2>/dev/null || printf '0'
}

LAST_COST="0.0000"
TOTAL_COST="0.0000"

run_claude() { # $1=pass name  $2=prompt file  $3=output file → 0 ok / 1 failed
  local name="$1" prompt="$2" outfile="$3" json rc
  LAST_COST="0.0000"
  local -a args=(-p --tools "" --strict-mcp-config --no-session-persistence
                 --max-turns "$MAX_TURNS" --output-format json)
  [ -n "$DREAMS_MODEL" ] && args+=(--model "$DREAMS_MODEL")
  [ -n "$DREAMS_MAX_USD_PER_PASS" ] && args+=(--max-budget-usd "$DREAMS_MAX_USD_PER_PASS")
  # cd to OUT_DIR so headless runs don't drag the whole repo CLAUDE.md context in
  json=$(cd "$OUT_DIR" && timeout "$DREAMS_PASS_TIMEOUT" "$CLAUDE_BIN" "${args[@]}" \
         < "$prompt" 2>>"$DREAMS_LOG")
  rc=$?
  if [ $rc -ne 0 ] || [ -z "$json" ]; then
    warn "$name: claude -p failed (rc=$rc — timeout, auth, or budget cap)"
    return 1
  fi
  if ! LAST_COST=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
open(sys.argv[1], "w").write(((d.get("result") or "").strip()) + "\n")
print("%.4f" % (d.get("total_cost_usd") or 0))
sys.exit(1 if d.get("is_error") else 0)' "$outfile" 2>/dev/null); then
    warn "$name: claude returned is_error (raw saved to $outfile)"
    LAST_COST="${LAST_COST:-0.0000}"
    return 1
  fi
  TOTAL_COST=$(awk -v a="$TOTAL_COST" -v b="$LAST_COST" 'BEGIN{printf "%.4f", a+b}')
  log "$name: claude pass done (cost \$$LAST_COST, output $(wc -c <"$outfile") bytes)"
  return 0
}

send_discord() { # $1 = message — same hermes-send path as blimp-brain send_discord
  if [ ! -x "$HERMES_BIN" ]; then
    warn "hermes CLI not found at $HERMES_BIN — digest not sent"
    return 1
  fi
  timeout 30 "$HERMES_BIN" send --to "discord:$DREAMS_DISCORD_CHANNEL" --quiet "$1" \
    >>"$DREAMS_LOG" 2>&1
}

stamp_pulse() { # $1=status  $2=digest_sent(true/false)
  local pulse
  pulse=$(printf '{"ts":"%s","status":"%s","activity":%s,"turns":%s,"tasks_done":%s,"a2a":%s,"reflector":"%s","curator":"%s","anticipatory":"%s","digest_sent":%s,"cost_usd":%s,"out_dir":"%s"}' \
    "$(date -Is)" "$1" "$ACTIVITY" "$TURNS" "$TASKS_DONE" "$A2A" \
    "$R_STATUS" "$C_STATUS" "$A_STATUS" "$2" "$TOTAL_COST" "$OUT_DIR")
  "$DUKE_DATA" state-set "pulse:dreams" "$pulse" >/dev/null 2>&1 \
    || warn "pulse stamp failed (duke_data down?)"
}

# ------------------------------------------------------------------
# 0. Demand gate
# ------------------------------------------------------------------
TURNS=$(count_turns_today)
TASKS_DONE=$(pg_count "SELECT count(*) FROM tasks WHERE status='completed'
  AND (completed_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date = (now() AT TIME ZONE 'Asia/Kuala_Lumpur')::date")
A2A=$(pg_count "SELECT count(*) FROM a2a_messages
  WHERE (updated_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date = (now() AT TIME ZONE 'Asia/Kuala_Lumpur')::date")
ACTIVITY=$(( TURNS + TASKS_DONE + A2A ))

R_STATUS="skipped"; C_STATUS="skipped"; A_STATUS="skipped"

# ------------------------------------------------------------------
# DRY_RUN: print the plan with real gate numbers, touch nothing, exit
# ------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  CUR_N=$(find "$AUTO_MEM_DIR" -maxdepth 1 -name '*.md' ! -name MEMORY.md -mmin -2160 2>/dev/null | wc -l)
  OPEN_N=$(pg_count "SELECT count(*) FROM tasks WHERE status IN ('pending','in_progress')")
  PROS_N=$(pg_count "SELECT count(*) FROM prospects")
  CAL="no"; [ -d "$DREAMS_CALENDAR_DIR" ] && ls "$DREAMS_CALENDAR_DIR/$TOMORROW".* >/dev/null 2>&1 && CAL="yes"
  GATE="would RUN"; [ "$ACTIVITY" -lt "$DREAMS_MIN_ACTIVITY" ] && GATE="would SKIP (quiet day)"
  log "DRY RUN — planned actions only, no claude/LLM calls, no writes"
  log "gate: turns=$TURNS tasks_done=$TASKS_DONE a2a=$A2A activity=$ACTIVITY threshold=$DREAMS_MIN_ACTIVITY → $GATE"
  log "budget: DREAMS_TOKEN_BUDGET=$DREAMS_TOKEN_BUDGET → --max-turns $MAX_TURNS/pass, hard cap \$$DREAMS_MAX_USD_PER_PASS/pass, ${DREAMS_PASS_TIMEOUT}s timeout/pass"
  log "[1/3] reflector: would POST $BANK_API/consolidate then $BANK_API/reflect (budget=$DREAMS_REFLECT_BUDGET, max_tokens=2048)"
  log "[1/3] reflector: would query today's failed/stuck a2a_messages + blocked tasks; if any → claude -p → $OUT_DIR/postmortem.md → duke-mem remember (tags dreams,postmortem,incident)"
  log "[2/3] curator: $CUR_N auto-memory file(s) changed in last 36h ($AUTO_MEM_DIR); if any → claude -p → $OUT_DIR/curator-playbook.diff (proposal only, never auto-applied)"
  log "[3/3] anticipatory: calendar=$CAL open_tasks=$OPEN_N prospects=$PROS_N; if any → claude -p → $OUT_DIR/prep-packs.md + 3-line digest"
  log "post: would send digest to #duke (discord:$DREAMS_DISCORD_CHANNEL) via $HERMES_BIN send --quiet"
  log "pulse: would stamp pulse:dreams in project_state; would prune $DREAMS_OUT_ROOT dirs older than ${DREAMS_RETAIN_DAYS}d"
  exit 0
fi

if [ "$ACTIVITY" -lt "$DREAMS_MIN_ACTIVITY" ]; then
  log "skip: activity $ACTIVITY < $DREAMS_MIN_ACTIVITY (turns=$TURNS tasks_done=$TASKS_DONE a2a=$A2A) — quiet day, no dreams"
  stamp_pulse "skipped" "false"
  exit 0
fi

log "dreams start: activity $ACTIVITY (turns=$TURNS tasks_done=$TASKS_DONE a2a=$A2A) ≥ $DREAMS_MIN_ACTIVITY — running all passes (max-turns $MAX_TURNS/pass)"
mkdir -p "$PROMPTS_DIR"

# ------------------------------------------------------------------
# 1. Reflector — consolidate, reflect, post-mortem case notes
# ------------------------------------------------------------------
R_FAILS=0

log "[1/3] reflector: Hindsight consolidate"
CONS=$(curl -sf --max-time 120 -X POST "$BANK_API/consolidate" \
       -H 'Content-Type: application/json' -d '{}' 2>/dev/null) || CONS=""
if [ -n "$CONS" ]; then
  printf '%s\n' "$CONS" > "$OUT_DIR/consolidate.json"
  log "reflector: consolidate accepted (${CONS:0:120})"
else
  warn "reflector: consolidate call failed (Hindsight down?)"
  R_FAILS=$((R_FAILS + 1))
fi

log "[1/3] reflector: Hindsight reflect (budget=$DREAMS_REFLECT_BUDGET)"
REFLECT_BODY=$(printf '{"query":"Nightly review for %s: what failed today, what was decided, what changed in the Blimp business? Surface lessons worth keeping.","budget":"%s","max_tokens":2048}' \
  "$TODAY" "$DREAMS_REFLECT_BUDGET")
if curl -sf --max-time 300 -X POST "$BANK_API/reflect" \
     -H 'Content-Type: application/json' -d "$REFLECT_BODY" \
     -o "$OUT_DIR/reflect.json" 2>/dev/null; then
  log "reflector: reflect saved → $OUT_DIR/reflect.json"
else
  warn "reflector: reflect call failed"
  R_FAILS=$((R_FAILS + 1))
fi

# Today's failed/stuck A2A tasks + blocked tasks → the post-mortem evidence
A2A_FAILS=$(psql --host=/var/run/postgresql --port="$PG_PORT" duke_data -tA -F $'\t' -c "
  SELECT task_id, from_agent, to_agent, state, coalesce(skill,''),
         left(body::text, 400), left(coalesce(result::text,''), 400),
         to_char(updated_at AT TIME ZONE 'Asia/Kuala_Lumpur', 'YYYY-MM-DD HH24:MI')
  FROM a2a_messages
  WHERE ((updated_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date = (now() AT TIME ZONE 'Asia/Kuala_Lumpur')::date
         AND state IN ('failed','rejected','input-required'))
     OR (state = 'submitted' AND updated_at < now() - interval '2 hours')
  ORDER BY updated_at DESC LIMIT 30" 2>/dev/null) || A2A_FAILS=""
BLOCKED=$(psql --host=/var/run/postgresql --port="$PG_PORT" duke_data -tA -F $'\t' -c "
  SELECT id, title, status, coalesce(assigned_to,''),
         to_char(updated_at AT TIME ZONE 'Asia/Kuala_Lumpur', 'YYYY-MM-DD HH24:MI')
  FROM tasks
  WHERE status IN ('failed','blocked')
    AND (updated_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date = (now() AT TIME ZONE 'Asia/Kuala_Lumpur')::date
  ORDER BY updated_at DESC LIMIT 20" 2>/dev/null) || BLOCKED=""

if [ -z "$A2A_FAILS" ] && [ -z "$BLOCKED" ]; then
  log "reflector: no failed/stuck A2A tasks or blocked tasks today — post-mortem claude pass skipped"
else
  printf '%s\n' "$A2A_FAILS" > "$OUT_DIR/a2a-failures.tsv"
  STATS=$("$DUKE_MEM" stats 2>/dev/null | head -20) || STATS="(stats unavailable)"
  {
    cat <<EOF
You are Duke's nightly Reflector (Dreams pass 1 of 3). It is $TODAY, 03:30 in
Kuala Lumpur. You are running headless with NO tools — output plain markdown
text only.

Below are today's failed/stuck A2A tasks and blocked tasks from the Blimp team
brain, plus brain stats. Write a post-mortem case note for EACH distinct
failure:

### Case: <short title>
- What happened (1-2 lines, plain language)
- Root cause (best hypothesis from the evidence; say "unknown" if unknown)
- What Duke should do differently next time (1 actionable line)

Rules: each case under 10 lines; no speculation beyond the evidence; if
several rows describe the same failure, write ONE case. If there are genuinely
no failures worth a case note, output exactly: NO_FAILURES
EOF
    printf '\n=== A2A failed/stuck (TSV: task_id, from, to, state, skill, body, result, updated) ===\n%s\n' "${A2A_FAILS:-"(none)"}"
    printf '\n=== Blocked/failed tasks today (TSV: id, title, status, assigned_to, updated) ===\n%s\n' "${BLOCKED:-"(none)"}"
    printf '\n=== Brain stats ===\n%s\n' "$STATS"
  } > "$PROMPTS_DIR/reflector.txt"

  if run_claude "reflector" "$PROMPTS_DIR/reflector.txt" "$OUT_DIR/postmortem.md"; then
    if [ -s "$OUT_DIR/postmortem.md" ] && [ "$(head -1 "$OUT_DIR/postmortem.md")" != "NO_FAILURES" ]; then
      if "$DUKE_MEM" remember \
           --content "$(head -c 4000 "$OUT_DIR/postmortem.md")" \
           --tags "dreams,postmortem,incident" \
           --source "dreams/$TODAY" \
           --context "nightly Dreams reflector post-mortem" >>"$DREAMS_LOG" 2>&1; then
        log "reflector: post-mortem remembered in blimp-brain (full text: $OUT_DIR/postmortem.md)"
      else
        warn "reflector: duke-mem remember failed — post-mortem only on disk"
        R_FAILS=$((R_FAILS + 1))
      fi
    else
      log "reflector: claude found no failures worth a case note"
    fi
  else
    R_FAILS=$((R_FAILS + 1))
  fi
fi

if   [ "$R_FAILS" -eq 0 ]; then R_STATUS="ok"
elif [ "$R_FAILS" -ge 3 ]; then R_STATUS="failed"
else                            R_STATUS="degraded"; fi

# ------------------------------------------------------------------
# 2. Curator — dedupe/contradiction-check recent auto-memory, propose diff
# ------------------------------------------------------------------
mapfile -t MEM_FILES < <(find "$AUTO_MEM_DIR" -maxdepth 1 -name '*.md' ! -name MEMORY.md -mmin -2160 2>/dev/null | sort | head -15)

if [ "${#MEM_FILES[@]}" -eq 0 ]; then
  log "[2/3] curator: no auto-memory writes in the last 36h — skipped"
else
  log "[2/3] curator: checking ${#MEM_FILES[@]} recent auto-memory file(s)"
  {
    cat <<EOF
You are Duke's nightly Curator (Dreams pass 2 of 3). Headless, NO tools —
output text only.

Below are Duke's auto-memory files modified in the last 36 hours, plus the
memory index (MEMORY.md). Check ONLY for:
1. duplicates — two files/sections recording the same fact,
2. contradictions — claims that cannot both be true,
3. index drift — MEMORY.md entries that no longer match their file.

Propose fixes as ONE unified diff (\`diff -u\` style, using the file paths
exactly as given). Do not rewrite style; touch only what fails the three
checks. Duke reviews and applies this by hand in the morning — it is never
auto-applied.

If everything passes, output exactly: NO_CHANGES
EOF
    if [ -f "$AUTO_MEM_DIR/MEMORY.md" ]; then
      printf '\n--- INDEX: %s ---\n%s\n' "$AUTO_MEM_DIR/MEMORY.md" "$(head -c 4000 "$AUTO_MEM_DIR/MEMORY.md")"
    fi
    for f in "${MEM_FILES[@]}"; do
      printf '\n--- FILE: %s ---\n%s\n' "$f" "$(head -c 6000 "$f")"
    done
  } > "$PROMPTS_DIR/curator.txt"

  if run_claude "curator" "$PROMPTS_DIR/curator.txt" "$OUT_DIR/curator-playbook.diff"; then
    if [ "$(head -1 "$OUT_DIR/curator-playbook.diff")" = "NO_CHANGES" ]; then
      log "curator: no changes proposed"
    else
      log "curator: playbook deltas proposed → $OUT_DIR/curator-playbook.diff (review before applying)"
    fi
    C_STATUS="ok"
  else
    C_STATUS="failed"
  fi
fi

# ------------------------------------------------------------------
# 3. Anticipatory — prep packs for tomorrow + 3-line digest
# ------------------------------------------------------------------
CAL_CONTENT=""
for f in "$DREAMS_CALENDAR_DIR/$TOMORROW.md" "$DREAMS_CALENDAR_DIR/$TOMORROW.txt" "$DREAMS_CALENDAR_DIR/calendar.md"; do
  if [ -f "$f" ]; then CAL_CONTENT=$(head -c 4000 "$f"); break; fi
done
OPEN_TASKS=$({ "$DUKE_DATA" tasks --status pending --limit 20;
               "$DUKE_DATA" tasks --status in_progress --limit 20; } 2>/dev/null) || OPEN_TASKS=""
PROSPECTS=$("$DUKE_DATA" prospects --limit 20 2>/dev/null) || PROSPECTS=""

DIGEST=""
if [ -z "$CAL_CONTENT" ] && [ -z "$OPEN_TASKS" ] && [ -z "$PROSPECTS" ]; then
  log "[3/3] anticipatory: no calendar, no open tasks, no prospects — skipped"
else
  log "[3/3] anticipatory: building prep packs for $TOMORROW"
  {
    cat <<EOF
You are Duke's nightly Anticipator (Dreams pass 3 of 3). Headless, NO tools —
output markdown only. It is the night before $TOMORROW.

Below: tomorrow's calendar (if any), open tasks, and active prospects from the
Blimp CRM. For each item that benefits from preparation, write a prep pack:

## Prep: <item>
- Why it matters tomorrow (1 line)
- Context Duke should have loaded (2-3 bullets)
- Suggested next action (1 line, concrete)

Skip items needing no prep. Order by tomorrow-urgency.

End with exactly this block — it gets posted to Discord, so plain language, no
jargon (the Boss reads it on his phone), each line under 90 characters:
DIGEST:
<line 1: most important thing about tomorrow>
<line 2: second thing, or pipeline status>
<line 3: what Duke prepared overnight>
EOF
    printf '\n=== Calendar for %s ===\n%s\n' "$TOMORROW" "${CAL_CONTENT:-"(no calendar source available — Gmail/Calendar MCP lands in P1.4)"}"
    printf '\n=== Open tasks ===\n%s\n' "${OPEN_TASKS:-"(none)"}"
    printf '\n=== Active prospects ===\n%s\n' "${PROSPECTS:-"(none — CRM empty)"}"
  } > "$PROMPTS_DIR/anticipatory.txt"

  if run_claude "anticipatory" "$PROMPTS_DIR/anticipatory.txt" "$OUT_DIR/prep-packs.md"; then
    DIGEST=$(awk '/^DIGEST:/{f=1; next} f && NF' "$OUT_DIR/prep-packs.md" | head -3)
    A_STATUS="ok"
    log "anticipatory: prep packs → $OUT_DIR/prep-packs.md"
  else
    A_STATUS="failed"
  fi
fi

# ------------------------------------------------------------------
# 4. Digest → #duke
# ------------------------------------------------------------------
if [ -z "$DIGEST" ]; then
  DIGEST="Quiet night: reflector $R_STATUS, curator $C_STATUS, nothing to prep for $TOMORROW."
fi
DIGEST_MSG="Dreams $TODAY
$DIGEST"
DIGEST_SENT="false"
if send_discord "$DIGEST_MSG"; then
  DIGEST_SENT="true"
  log "digest posted to #duke"
else
  warn "digest post to #duke failed — digest saved in $OUT_DIR and this log"
  printf '%s\n' "$DIGEST_MSG" > "$OUT_DIR/digest.txt"
fi

# ------------------------------------------------------------------
# 5. Prune + pulse stamp
# ------------------------------------------------------------------
find "$DREAMS_OUT_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' \
  -mtime "+$DREAMS_RETAIN_DAYS" -exec rm -rf {} + 2>/dev/null

FAILED_PASSES=0
[ "$R_STATUS" = "failed" ] && FAILED_PASSES=$((FAILED_PASSES + 1))
[ "$C_STATUS" = "failed" ] && FAILED_PASSES=$((FAILED_PASSES + 1))
[ "$A_STATUS" = "failed" ] && FAILED_PASSES=$((FAILED_PASSES + 1))

STATUS="completed"
[ "$FAILED_PASSES" -gt 0 ] && STATUS="degraded"
stamp_pulse "$STATUS" "$DIGEST_SENT"
log "dreams done: $STATUS (reflector=$R_STATUS curator=$C_STATUS anticipatory=$A_STATUS, cost \$$TOTAL_COST, out: $OUT_DIR)"

# Exit 1 only if every claude-bearing pass failed — systemd then shows the
# unit as failed and the P1.7 cron-audit picks it up.
if [ "$FAILED_PASSES" -ge 3 ]; then
  exit 1
fi
exit 0
