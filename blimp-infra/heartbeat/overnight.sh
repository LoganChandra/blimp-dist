#!/usr/bin/env bash
# overnight.sh — Duke's overnight shift runner. Demand-gated, draft-only.
#
# Fired at 00:30 KL by duke-overnight.timer. Reads the work queue from
# project_state key 'overnight-ok' (a JSON list Duke/the Boss populate during
# the day). Empty or missing queue -> exit silently: no tokens, no noise.
#
# Per iteration:
#   1. re-read the deny-list (project_state 'overnight-deny', JSON list of
#      case-insensitive substrings) — a task matching any entry is skipped
#   2. honor STOP: project_state 'overnight-stop' truthy, or a STOP file at
#      ~/blimp/overnight-out/STOP. NOTE: there is no bash-level Discord
#      *read* path (the `hermes send` CLI is send-only, fetch_messages is
#      MCP-only), so "the Boss types STOP in #duke" is honored indirectly —
#      Duke's live session sets 'overnight-stop' when it sees the message.
#   3. take ONE task, run headless `claude -p` with a hard draft-only
#      constraint + read/write-only tool whitelist (no Bash, no MCP — so no
#      sends, no pushes, no deploys are even possible)
#   4. checkpoint progress to project_state 'overnight-progress', pop the
#      task off 'overnight-ok'
#   5. enforce token budgets: SOFT cap -> no new tasks; HARD cap -> kill the
#      loop + one alert line to #duke
#
# Outputs land in ~/blimp/overnight-out/YYYY-MM-DD/NN-<slug>/ as DRAFTS.
# A summary.md is written for the morning digest (state key
# 'overnight-summary' + a line in ~/blimp/heartbeat/digest-queue.txt) —
# this script does NOT ping the Boss at 3am; the digest job delivers it at
# wake time. The only exception is the hard-cap alert.
#
# Usage:
#   bash overnight.sh              # normal run (the timer calls this)
#   bash overnight.sh --dry-run    # show queue, deny matches, and the claude
#                                  # command that WOULD run; writes nothing
#   DRY_RUN=1 bash overnight.sh    # same
#
# Env overrides:
#   OVERNIGHT_SOFT_CAP_TOKENS   stop taking new tasks past this   (default 300000)
#   OVERNIGHT_HARD_CAP_TOKENS   kill loop + alert past this       (default 500000)
#   OVERNIGHT_MAX_TASKS         max tasks per shift               (default 5)
#   OVERNIGHT_TASK_TIMEOUT_SECS wall-clock cap per task           (default 1800)
#   OVERNIGHT_MAX_TURNS         claude -p --max-turns per task    (default 30)
#   CLAUDE_BIN / HERMES_BIN / DUKE_CHANNEL_ID / OVERNIGHT_OUT

set -euo pipefail

export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

OUT_ROOT="${OVERNIGHT_OUT:-$HOME/blimp/overnight-out}"
DIGEST_QUEUE="$HOME/blimp/heartbeat/digest-queue.txt"
TELEMETRY_DIR="$HOME/blimp/telemetry/daily"
LOCKFILE="$HOME/blimp/heartbeat/overnight.lock"

CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.npm-global/bin/claude}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
DUKE_CHANNEL_ID="${DUKE_CHANNEL_ID:-<ID>}"   # #duke

SOFT_CAP="${OVERNIGHT_SOFT_CAP_TOKENS:-300000}"
HARD_CAP="${OVERNIGHT_HARD_CAP_TOKENS:-500000}"
MAX_TASKS="${OVERNIGHT_MAX_TASKS:-5}"
TASK_TIMEOUT="${OVERNIGHT_TASK_TIMEOUT_SECS:-1800}"
MAX_TURNS="${OVERNIGHT_MAX_TURNS:-30}"
DRY_RUN="${DRY_RUN:-0}"
case "${1:-}" in --dry-run) DRY_RUN=1 ;; esac

REPO="$HOME/blimp/repo"
RUN_DATE="$(date +%F)"
RUN_DIR="$OUT_ROOT/$RUN_DATE"

PSQL=(psql --host=/var/run/postgresql --port=5433 duke_data)

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ------------------------------------------------------------------
# project_state helpers (keys below are our own literals, never user input)
# ------------------------------------------------------------------
state_get() {
  "${PSQL[@]}" -tAc "SELECT value FROM project_state WHERE key = '$1'" 2>/dev/null || true
}

state_set_json() {  # state_set_json <key> <json> — :'v' quoting keeps it injection-safe
  # FIXED 2026-07-08 (see docs/foundry-registry/lessons.md L-020): psql's
  # `:'var'` interpolation silently fails with "syntax error at or near ':'"
  # when the SQL is passed via `-c "..."` — confirmed live on psql 16.14; the
  # exact same query works when fed via stdin instead. This was NOT a quoting
  # style bug — heartbeat.sh's "known-working" version had the identical
  # latent bug, just never caught because heartbeat.timer has been disabled
  # since 2026-06-13. Fix: pipe the SQL via stdin, keep the -v binding.
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  "${PSQL[@]}" -qv v="$2" <<SQL >/dev/null
INSERT INTO project_state (key, value) VALUES ('$1', :'v'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
SQL
}

stamp_pulse() {
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  state_set_json "pulse:overnight" "$(jq -cn --arg ts "$(date -Is)" '{ts: $ts}')" \
    || log "WARN could not stamp pulse:overnight"
}

send_discord() {
  local msg="$1"
  if [ "$DRY_RUN" = 1 ]; then log "DRY_RUN would send to #duke: $msg"; return 0; fi
  "$HERMES_BIN" send --to "discord:$DUKE_CHANNEL_ID" --quiet "$msg" 2>/dev/null \
    || log "WARN discord send failed: $msg"
}

queue_digest_line() {
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  mkdir -p "$(dirname "$DIGEST_QUEUE")"
  grep -qxF -- "$1" "$DIGEST_QUEUE" 2>/dev/null || printf '%s\n' "$1" >> "$DIGEST_QUEUE"
}

add_telemetry() {  # add_telemetry <tokens> — self-report spend (HEARTBEAT.md contract)
  local toks="$1" f cur=0
  if [ "$DRY_RUN" = 1 ] || ! [[ "$toks" =~ ^[0-9]+$ ]] || [ "$toks" -eq 0 ]; then return 0; fi
  mkdir -p "$TELEMETRY_DIR"
  f="$TELEMETRY_DIR/$(date +%F).tokens"
  if [ -s "$f" ]; then cur="$(tr -dc '0-9' < "$f")"; cur="${cur:-0}"; fi
  printf '%s\n' "$((cur + toks))" > "$f"
}

# ------------------------------------------------------------------
# Queue / deny / stop
# ------------------------------------------------------------------
read_queue() {  # prints one task per line (strings, or .task of objects)
  local q
  q="$(state_get overnight-ok)"
  [ -n "$q" ] || return 0
  printf '%s' "$q" | jq -r '
    if type == "array" then
      .[] | (if type == "string" then . else (.task // empty) end)
    else empty end' 2>/dev/null || true
}

is_denied() {  # is_denied <task> — 0 if task matches any deny-list substring
  local task="$1" deny
  deny="$(state_get overnight-deny)"
  [ -n "$deny" ] || return 1
  printf '%s' "$deny" | jq -e --arg t "$task" '
    (type == "array") and any(.[]?;
      (if type == "string" then . else (.match // "") end) as $d
      | ($d != "") and (($t | ascii_downcase) | contains($d | ascii_downcase))
    )' >/dev/null 2>&1
}

stop_requested() {
  local v
  v="$(state_get overnight-stop)"
  case "${v,,}" in true|1|'"true"'|'"stop"'|'"pause"') return 0 ;; esac
  if [ -e "$OUT_ROOT/STOP" ]; then return 0; fi
  return 1
}

pop_task() {  # pop_task <task> — remove first matching entry from overnight-ok
  local q new
  q="$(state_get overnight-ok)"
  [ -n "$q" ] || return 0
  new="$(printf '%s' "$q" | jq -c --arg t "$1" '
    if type == "array" then
      (map((if type == "string" then . else (.task // "") end) == $t) | index(true)) as $i
      | if $i == null then . else del(.[$i]) end
    else . end' 2>/dev/null)" || return 0
  [ -n "$new" ] || return 0
  state_set_json overnight-ok "$new"
}

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
    | sed 's/^-//; s/-$//' | cut -c1-40
}

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
mkdir -p "$(dirname "$LOCKFILE")"
exec 200>"$LOCKFILE"
flock -n 200 || { log "another overnight shift holds the lock — exiting"; exit 0; }

mapfile -t QUEUE < <(read_queue)
if [ "${#QUEUE[@]}" -eq 0 ]; then
  log "overnight-ok queue empty — nothing to do (demand gate)"
  stamp_pulse
  exit 0
fi

log "overnight shift starting: ${#QUEUE[@]} queued task(s), soft=$SOFT_CAP hard=$HARD_CAP max_tasks=$MAX_TASKS dry_run=$DRY_RUN"
stamp_pulse

TOTAL_TOKENS=0
TOTAL_COST="0"
DONE=()          # "slug<TAB>status<TAB>tokens" per attempted task
ATTEMPTED=()     # task texts already taken this shift (never re-take)
STOP_REASON="queue drained"
N=0

while [ "$N" -lt "$MAX_TASKS" ]; do
  # budget gates (between tasks — per-task runtime is bounded by timeout/turns)
  if [ "$TOTAL_TOKENS" -ge "$HARD_CAP" ]; then
    STOP_REASON="HARD token cap ($TOTAL_TOKENS >= $HARD_CAP)"
    send_discord "[overnight] hard token cap hit ($TOTAL_TOKENS tokens) — loop killed; drafts in $RUN_DIR"
    break
  fi
  if [ "$TOTAL_TOKENS" -ge "$SOFT_CAP" ]; then
    STOP_REASON="soft token cap ($TOTAL_TOKENS >= $SOFT_CAP) — no new tasks"
    break
  fi
  if stop_requested; then
    STOP_REASON="STOP requested (overnight-stop / STOP file)"
    break
  fi

  # take ONE task: first queue entry that isn't denied and wasn't attempted.
  # Queue and deny-list are re-read EVERY iteration — the Boss/Duke can edit
  # them mid-shift.
  mapfile -t QUEUE < <(read_queue)
  TASK=""
  for t in "${QUEUE[@]+"${QUEUE[@]}"}"; do
    skip=0
    for a in "${ATTEMPTED[@]+"${ATTEMPTED[@]}"}"; do
      if [ "$a" = "$t" ]; then skip=1; break; fi
    done
    if [ "$skip" = 1 ]; then continue; fi
    if is_denied "$t"; then
      log "skip (deny-list): $t"
      queue_digest_line "overnight $RUN_DATE: skipped by deny-list: ${t:0:80}"
      ATTEMPTED+=("$t")
      continue
    fi
    TASK="$t"
    break
  done
  if [ -z "$TASK" ]; then
    break  # queue drained (or everything denied/attempted)
  fi

  ATTEMPTED+=("$TASK")
  N=$((N + 1))
  SLUG="$(printf '%02d' "$N")-$(slugify "$TASK")"
  TASK_DIR="$RUN_DIR/$SLUG"

  PROMPT="OVERNIGHT SHIFT TASK (draft-only). Task: $TASK

HARD CONSTRAINTS — you are running unattended at night:
- DRAFTS ONLY: no sends of any kind, no git push, no deploys, no service restarts, no settings/hook/access changes. You have no Bash and no MCP tools; do not try to work around that.
- Write every output file under $TASK_DIR/ (notes, drafts, computed quotes, code as .patch/.diff files). You may read anything in the repo.
- Finish with a short DONE summary listing the files you wrote and what needs the Boss's review."

  if [ "$DRY_RUN" = 1 ]; then
    log "DRY_RUN would run task $N: $TASK"
    log "DRY_RUN   cd $REPO && timeout $TASK_TIMEOUT $CLAUDE_BIN -p '<brief>' --output-format json --max-turns $MAX_TURNS --allowedTools 'Read,Glob,Grep,Write,Edit' > $TASK_DIR/result.json"
    DONE+=("$SLUG	dry-run	0")
    continue
  fi

  mkdir -p "$TASK_DIR"
  printf '%s\n' "$PROMPT" > "$TASK_DIR/brief.md"
  log "task $N starting: $SLUG"

  STATUS="ok"
  rc=0
  ( cd "$REPO" && timeout "$TASK_TIMEOUT" "$CLAUDE_BIN" -p "$PROMPT" \
      --output-format json --max-turns "$MAX_TURNS" \
      --allowedTools "Read,Glob,Grep,Write,Edit" \
      > "$TASK_DIR/result.json" 2> "$TASK_DIR/stderr.log" ) || rc=$?
  if [ "$rc" = 124 ]; then STATUS="timeout"
  elif [ "$rc" != 0 ]; then STATUS="error(rc=$rc)"
  elif jq -e '.is_error == true' "$TASK_DIR/result.json" >/dev/null 2>&1; then STATUS="agent-error"
  fi

  # extract the human-readable result + usage
  jq -r '.result // empty' "$TASK_DIR/result.json" > "$TASK_DIR/result.md" 2>/dev/null || true
  TOKS="$(jq -r '((.usage.input_tokens // 0) + (.usage.output_tokens // 0) + (.usage.cache_creation_input_tokens // 0))' \
          "$TASK_DIR/result.json" 2>/dev/null)" || TOKS=0
  [[ "$TOKS" =~ ^[0-9]+$ ]] || TOKS=0
  COST="$(jq -r '.total_cost_usd // 0' "$TASK_DIR/result.json" 2>/dev/null)" || COST=0
  TOTAL_TOKENS=$((TOTAL_TOKENS + TOKS))
  TOTAL_COST="$(awk -v a="$TOTAL_COST" -v b="$COST" 'BEGIN {printf "%.4f", a + b}')"
  add_telemetry "$TOKS"
  DONE+=("$SLUG	$STATUS	$TOKS")
  log "task $N done: $SLUG status=$STATUS tokens=$TOKS shift_total=$TOTAL_TOKENS (\$$TOTAL_COST)"

  # checkpoint, then pop the task off the queue (attempted = consumed; the
  # output is a draft either way and the brief is preserved in brief.md)
  CKPT="$(jq -cn --arg date "$RUN_DATE" --arg last "$SLUG" --arg status "$STATUS" \
              --arg ts "$(date -Is)" --argjson tokens "$TOTAL_TOKENS" --argjson n "$N" \
              '{date: $date, last_task: $last, last_status: $status, tasks_done: $n, tokens: $tokens, ts: $ts}')"
  state_set_json overnight-progress "$CKPT" || log "WARN checkpoint write failed"
  pop_task "$TASK" || log "WARN could not pop task from overnight-ok"
done

if [ "$N" -ge "$MAX_TASKS" ]; then STOP_REASON="max tasks ($MAX_TASKS) reached"; fi

# ------------------------------------------------------------------
# Morning summary (delivered by the digest job at wake time, not at 3am)
# ------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  log "DRY_RUN summary: would attempt ${#DONE[@]} task(s); stop reason: $STOP_REASON"
  exit 0
fi

mkdir -p "$RUN_DIR"
{
  echo "# Overnight shift — $RUN_DATE"
  echo
  echo "- tasks attempted: $N"
  echo "- tokens: $TOTAL_TOKENS (cost \$$TOTAL_COST)"
  echo "- stop reason: $STOP_REASON"
  echo
  echo "| task | status | tokens |"
  echo "|---|---|---|"
  for d in "${DONE[@]+"${DONE[@]}"}"; do
    IFS=$'\t' read -r s st tk <<< "$d"
    echo "| $s | $st | $tk |"
  done
  echo
  echo "Drafts under: $RUN_DIR/ — everything needs the Boss's review before it goes anywhere."
} > "$RUN_DIR/summary.md"

SUMMARY_JSON="$(jq -cn --arg date "$RUN_DATE" --arg path "$RUN_DIR/summary.md" \
                  --arg reason "$STOP_REASON" --argjson n "$N" --argjson tokens "$TOTAL_TOKENS" \
                  '{date: $date, summary: $path, tasks: $n, tokens: $tokens, stop_reason: $reason}')"
state_set_json overnight-summary "$SUMMARY_JSON" || log "WARN overnight-summary write failed"
queue_digest_line "overnight $RUN_DATE: $N task(s), $TOTAL_TOKENS tokens — drafts in $RUN_DIR (see summary.md)"
stamp_pulse
log "overnight shift finished: $N task(s), $TOTAL_TOKENS tokens, stop: $STOP_REASON"
