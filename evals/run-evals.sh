#!/usr/bin/env bash
# run-evals.sh — headless eval runner for the estate's incident-derived
# behavioral regression suite (P1.8a). Grades Duke AND Frank (--agent).
#
# For each case in cases/*.yaml: run `claude -p` FROM THE TARGET AGENT'S OWN
# REPO so its real CLAUDE.md (+ @-imports: system-context, AGENTS.md) loads
# exactly like a production session — this is what actually gets graded, not
# a hand-maintained restatement of the rules. preamble.md is eval-mode
# framing only (offline, read-only, evals/ out of bounds), appended on top.
# Scores the four regex assertion lists + an optional LLM `judge_rubric` tier
# against the FINAL RESULT ONLY (not every assistant text block along the
# way), writes a results JSONL + markdown summary under results/<run-id>/.
#
# Usage:
#   ./run-evals.sh                       # run every Duke case, serial
#   ./run-evals.sh --agent frank         # run Frank's suite instead
#   ./run-evals.sh --budget 2            # run with concurrency 2 (the hard cap)
#   ./run-evals.sh --case <id>           # run a single case
#   ./run-evals.sh --dry-run             # validate + list all cases, run nothing
#
# Env knobs:
#   CLAUDE_BIN      path to the claude CLI (auto-detected)
#   EVAL_MODEL      model override passed to --model (default: CLI default)
#   CASE_TIMEOUT    seconds per case before kill (default 300)
#   MAX_TURNS       agent turn cap per case (default 10)
#   MAX_RUN_COST    USD warning threshold in the summary (default 15)
#   OPENROUTER_API_KEY / HINDSIGHT_LLM_MODEL  used by judge_case.py for the
#                   judge_rubric tier (read from ~/blimp/.env if unset)
#
# Safety by construction (read-only harness):
#   - never passes --dangerously-skip-permissions; forces --permission-mode default
#   - mutating tools (Bash/Edit/Write/sends/brain-writes/discord-sends) are
#     hard-disallowed, so no eval run can touch the brain, Discord, git, or disk
#   - only read-only tools are pre-allowed (Read/Grep/Glob + brain reads)
#   Consequence: must_call_tool assertions only work for read-only tools;
#   must_not_call_tool entries on mutating tools are belt-and-suspenders.
#
# Exit codes: 0 = all cases pass, 1 = >=1 case failed (or <20 cases on a real run),
#             2 = usage / environment error.
#
# Uses set -uo pipefail (not -e): case failures are data, not script aborts.

set -uo pipefail

log()  { printf '\n\033[1;36m[evals]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m  err\033[0m %s\n' "$*"; }

EVALS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$EVALS_DIR/.." && pwd)"
CASES_DIR="$EVALS_DIR/cases"
RESULTS_DIR="$EVALS_DIR/results"
PREAMBLE_FILE="$EVALS_DIR/preamble.md"

CASE_TIMEOUT="${CASE_TIMEOUT:-300}"
MAX_TURNS="${MAX_TURNS:-10}"
MIN_CASES=20

# Read-only tools the eval agent may actually use.
ALLOWED_TOOLS="Read,Grep,Glob,mcp__blimp-brain__recall,mcp__blimp-brain__recent,mcp__blimp-brain__tasks,mcp__blimp-brain__prospects,mcp__blimp-brain__quotes,mcp__blimp-brain__memory_stats,mcp__blimp-brain__state_get,mcp__blimp-brain__a2a_inbox,mcp__blimp-brain__messages,mcp__plugin_discord_discord__fetch_messages"

# Mutating tools: hard-denied (deny beats any allow rule, incl. Bash(*) in
# settings.local.json) so a scheduled eval can never write the brain, post to
# Discord, run shell, or edit files.
DISALLOWED_TOOLS="Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch,mcp__blimp-brain__remember,mcp__blimp-brain__log_message,mcp__blimp-brain__task_add,mcp__blimp-brain__task_update,mcp__blimp-brain__prospect_add,mcp__blimp-brain__prospect_update,mcp__blimp-brain__quote_log,mcp__blimp-brain__state_set,mcp__blimp-brain__a2a_send,mcp__blimp-brain__a2a_reply,mcp__blimp-brain__a2a_complete,mcp__blimp-brain__a2a_pushback,mcp__blimp-brain__send_discord,mcp__plugin_discord_discord__reply,mcp__plugin_discord_discord__edit_message,mcp__plugin_discord_discord__react,mcp__plugin_discord_discord__download_attachment"

usage() {
  sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
}

# ------------------------------------------------------------------
# arg parsing
# ------------------------------------------------------------------
CASE_FILTER=""
DRY_RUN=0
BUDGET=1
AGENT="duke"
while [ $# -gt 0 ]; do
  case "$1" in
    --case)    CASE_FILTER="${2:?--case needs a case id}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --budget)  BUDGET="${2:?--budget needs a number}"; shift 2 ;;
    --agent)   AGENT="${2:?--agent needs duke or frank}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

case "$AGENT" in
  duke|frank) ;;
  *) err "--agent must be 'duke' or 'frank' (got: $AGENT)"; exit 2 ;;
esac

# Each agent runs from its OWN repo cwd so its real CLAUDE.md (+ @-imports)
# auto-loads exactly like a production session — the eval tests the actual
# deployed prompt stack, not a hand-maintained restatement of it.
if [ "$AGENT" = "frank" ]; then
  AGENT_REPO_ROOT="${FRANK_REPO_ROOT:-$HOME/nanook/repo}"
  AGENT_SPEAKER="nanook"
else
  AGENT_REPO_ROOT="$REPO_ROOT"
  AGENT_SPEAKER="duke"
fi
[ -d "$AGENT_REPO_ROOT" ] || { err "agent repo root not found: $AGENT_REPO_ROOT"; exit 2; }

case "$BUDGET" in
  ''|*[!0-9]*) err "--budget must be a positive integer"; exit 2 ;;
esac
[ "$BUDGET" -ge 1 ] || BUDGET=1
if [ "$BUDGET" -gt 2 ]; then
  warn "budget capped at 2 concurrent cases (P1 token-budget rule)"
  BUDGET=2
fi

# ------------------------------------------------------------------
# environment checks
# ------------------------------------------------------------------
PY=""
# The judge needs httpx as well as yaml. A python without httpx crashed judge_case.py silently and every
# judge_rubric case failed with an empty reason (found 2026-11-06), so require both.
for cand in "$HOME/blimp/mesh-venv/bin/python" "$HOME/blimp/duke-venv/bin/python" /usr/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml, httpx' >/dev/null 2>&1; then
    PY="$(command -v "$cand")"
    break
  fi
done
if [ -z "$PY" ]; then
  err "no python3 with PyYAML + httpx found (apt: python3-yaml python3-httpx, or ~/blimp/mesh-venv)"
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  err "jq not found"
  exit 2
fi

[ -f "$PREAMBLE_FILE" ] || { err "missing $PREAMBLE_FILE"; exit 2; }
[ -d "$CASES_DIR" ]     || { err "missing $CASES_DIR"; exit 2; }

yaml2json() {
  "$PY" - "$1" <<'PYEOF'
import json, sys, yaml
with open(sys.argv[1]) as f:
    print(json.dumps(yaml.safe_load(f)))
PYEOF
}

# ------------------------------------------------------------------
# collect + validate cases (filenames starting with _ are templates)
# ------------------------------------------------------------------
CASE_FILES=()
if [ -n "$CASE_FILTER" ]; then
  f="$CASES_DIR/$CASE_FILTER.yaml"
  [ -f "$f" ] || { err "no such case: $CASE_FILTER (expected $f)"; exit 2; }
  CASE_FILES+=("$f")
else
  while IFS= read -r f; do
    CASE_FILES+=("$f")
  done < <(find "$CASES_DIR" -maxdepth 1 -name '[a-z0-9]*.yaml' | sort)
fi

if [ "${#CASE_FILES[@]}" -eq 0 ]; then
  err "no case files found in $CASES_DIR"
  exit 2
fi

validate_case() { # $1 = file; echoes nothing, returns 0/1
  local f="$1" base cj
  base="$(basename "$f")"
  cj="$(yaml2json "$f")" || { err "$base: invalid YAML"; return 1; }
  if ! jq -e --arg fname "$base" '
      (.id | type == "string" and test("^[a-z0-9][a-z0-9-]*$")) and
      (.id + ".yaml" == $fname) and
      (.source_incident | type == "string" and length > 0) and
      (.prompt | type == "string" and length > 0) and
      (.severity as $s | ["critical","high","medium"] | index($s) != null) and
      (.expected_behavior | type == "object") and
      (((.expected_behavior.must_include // []) +
        (.expected_behavior.must_not_include // []) +
        (.expected_behavior.must_call_tool // []) +
        (.expected_behavior.must_not_call_tool // [])) | length > 0)
    ' >/dev/null <<<"$cj"; then
    err "$base: schema check failed (id==filename, source_incident, prompt, severity in critical|high|medium, >=1 assertion)"
    return 1
  fi
  return 0
}

# Validate BEFORE the agents-filter below, on purpose (2026-07-09 fix — see
# docs/foundry-registry/lessons.md): the agents-filter's jq query used to run
# first and silently dropped any file that failed to parse (bad YAML) instead
# of surfacing it as invalid, defeating the "must fail loudly" MIN_CASES
# guarantee for full-suite runs — a malformed case could vanish from every
# run forever with zero signal. Validating first means a broken file is
# always caught here, never silently filtered out downstream.
log "validating ${#CASE_FILES[@]} case file(s)"
INVALID=0
for f in "${CASE_FILES[@]}"; do
  validate_case "$f" || INVALID=$((INVALID + 1))
done
if [ "$INVALID" -gt 0 ]; then
  err "$INVALID invalid case file(s) — fix before running"
  exit 1
fi
ok "all case files valid"

# Filter by the case's optional `agents:` field, now that every file is known
# to be valid YAML. Omitted = applies to both (true of almost every existing
# incident-derived rail/safety case); a case can restrict itself with
# `agents: [frank]` or `agents: [duke]` when the scenario is genuinely
# agent-specific (e.g. Frank's brain-upkeep duties).
if [ -z "$CASE_FILTER" ]; then
  FILTERED_FILES=()
  for f in "${CASE_FILES[@]}"; do
    if yaml2json "$f" | jq -e --arg a "$AGENT" \
        '((.agents // ["duke","frank"]) | index($a)) != null' >/dev/null 2>&1; then
      FILTERED_FILES+=("$f")
    fi
  done
  CASE_FILES=("${FILTERED_FILES[@]}")
fi

# MIN_CASES floor applies to every whole-suite run, not just --dry-run — a
# case-file deletion or a glob typo must fail loudly, not silently run 3
# cases and call it a green week. (--dry-run has its own listing+check below
# so its nicer "list then fail" UX is preserved.)
if [ "$DRY_RUN" -eq 0 ] && [ -z "$CASE_FILTER" ] && [ "${#CASE_FILES[@]}" -lt "$MIN_CASES" ]; then
  err "fewer than $MIN_CASES cases — suite below the P1.8 floor"
  exit 1
fi

# ------------------------------------------------------------------
# dry run: list + count, no claude calls
# ------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  log "dry run — cases that would execute"
  printf '%-34s %-9s %-11s %s\n' "ID" "SEVERITY" "ASSERTIONS" "SOURCE INCIDENT"
  for f in "${CASE_FILES[@]}"; do
    yaml2json "$f" | jq -r '
      [ .id,
        .severity,
        ( ((.expected_behavior.must_include // []) | length | tostring) + "+" +
          ((.expected_behavior.must_not_include // []) | length | tostring) + "+" +
          ((.expected_behavior.must_call_tool // []) | length | tostring) + "+" +
          ((.expected_behavior.must_not_call_tool // []) | length | tostring) ),
        (.source_incident | gsub("\n"; " ") | .[0:60]) ] | @tsv' |
      awk -F'\t' '{ printf "%-34s %-9s %-11s %s\n", $1, $2, $3, $4 }'
  done
  printf '\nTOTAL: %s cases\n' "${#CASE_FILES[@]}"
  if [ -z "$CASE_FILTER" ] && [ "${#CASE_FILES[@]}" -lt "$MIN_CASES" ]; then
    err "fewer than $MIN_CASES cases — suite below the P1.8 floor"
    exit 1
  fi
  ok "dry run complete"
  exit 0
fi

# ------------------------------------------------------------------
# real run setup
# ------------------------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
  for cand in claude "$HOME/.npm-global/bin/claude" "$HOME/.local/bin/claude"; do
    if command -v "$cand" >/dev/null 2>&1; then
      CLAUDE_BIN="$(command -v "$cand")"
      break
    fi
  done
fi
if [ -z "$CLAUDE_BIN" ]; then
  err "claude CLI not found (set CLAUDE_BIN=/path/to/claude)"
  exit 2
fi

RUN_ID="$(date +%Y%m%d-%H%M%S)"
[ "$AGENT" = "frank" ] && RUN_ID="frank-$RUN_ID"
RUN_DIR="$RESULTS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"
PREAMBLE="$(cat "$PREAMBLE_FILE")"

log "run $RUN_ID — agent $AGENT, ${#CASE_FILES[@]} case(s), budget $BUDGET, model ${EVAL_MODEL:-default}"

run_one_case() { # $1 = case file
  local f="$1" cj id severity tfile efile rfile started ended rc status
  cj="$(yaml2json "$f")"
  id="$(jq -r '.id' <<<"$cj")"
  severity="$(jq -r '.severity' <<<"$cj")"
  tfile="$RUN_DIR/$id.transcript.jsonl"
  efile="$RUN_DIR/$id.stderr.log"
  rfile="$RUN_DIR/$id.result.json"

  local cmd=( "$CLAUDE_BIN" -p "$(jq -r '.prompt' <<<"$cj")"
              --append-system-prompt "$PREAMBLE"
              --output-format stream-json --verbose
              --max-turns "$MAX_TURNS"
              --permission-mode default
              --allowedTools "$ALLOWED_TOOLS"
              --disallowedTools "$DISALLOWED_TOOLS" )
  if [ -n "${EVAL_MODEL:-}" ]; then
    cmd+=( --model "$EVAL_MODEL" )
  fi

  started="$(date +%s)"
  ( cd "$AGENT_REPO_ROOT" && timeout -k 10 "$CASE_TIMEOUT" "${cmd[@]}" ) >"$tfile" 2>"$efile"
  rc=$?
  ended="$(date +%s)"
  status="ok"
  [ "$rc" -eq 124 ] && status="timeout"
  [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && status="error"

  # Grade the FINAL RESULT ONLY, not every assistant text block along the
  # way — grading concatenated investigation notes ("here's a warning about
  # pkill" while diagnosing) as if it were the final answer is exactly the
  # mention-vs-use false-positive class that decayed the pass rate 17→15
  # across four runs (see docs/foundry-registry/lessons.md L-019). Fall back
  # to concatenated assistant text only if the run produced no result event
  # at all (crash/timeout), so a genuine failure still has something to grade.
  local text tools
  text="$(jq -r 'select(.type == "result") | .result // empty' "$tfile" 2>/dev/null | tr '\n' ' ')"
  if [ -z "${text// /}" ]; then
    text="$(jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "text") | .text' "$tfile" 2>/dev/null | tr '\n' ' ')"
  fi
  tools="$(jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | .name' "$tfile" 2>/dev/null | sort -u)"

  local failures=()
  if [ -z "${text// /}" ]; then
    failures+=("no output captured (claude rc=$rc, status=$status)")
  fi
  local pat
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    grep -qiE -- "$pat" <<<"$text" || failures+=("must_include miss: $pat")
  done < <(jq -r '.expected_behavior.must_include[]?' <<<"$cj")
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    if grep -qiE -- "$pat" <<<"$text"; then
      failures+=("must_not_include hit: $pat")
    fi
  done < <(jq -r '.expected_behavior.must_not_include[]?' <<<"$cj")
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    grep -qiE -- "$pat" <<<"$tools" || failures+=("must_call_tool miss: $pat")
  done < <(jq -r '.expected_behavior.must_call_tool[]?' <<<"$cj")
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    if grep -qiE -- "$pat" <<<"$tools"; then
      failures+=("must_not_call_tool hit: $pat")
    fi
  done < <(jq -r '.expected_behavior.must_not_call_tool[]?' <<<"$cj")

  # Second-tier LLM judge — for cases whose expected behavior is too nuanced
  # for regex (a case carries an optional `judge_rubric` field). Authoritative
  # alongside the regex checks, not just a tie-breaker: a case with
  # judge_rubric is testing exactly the judgment call regex can't grade.
  local judge_rubric
  judge_rubric="$(jq -r '.judge_rubric // empty' <<<"$cj")"
  if [ -n "$judge_rubric" ] && [ -n "${text// /}" ]; then
    local rfile_tmp tfile_tmp judge_out judge_pass judge_reason
    rfile_tmp="$(mktemp)"; tfile_tmp="$(mktemp)"
    printf '%s' "$judge_rubric" > "$rfile_tmp"
    printf '%s' "$text" > "$tfile_tmp"
    judge_out="$("$PY" "$EVALS_DIR/judge_case.py" "$rfile_tmp" "$tfile_tmp" 2>/dev/null)"
    rm -f "$rfile_tmp" "$tfile_tmp"
    judge_pass="$(jq -r '.pass // false' <<<"$judge_out" 2>/dev/null)"
    judge_reason="$(jq -r '.reason // "no reason given"' <<<"$judge_out" 2>/dev/null)"
    [ -n "$judge_out" ] || judge_reason="judge crashed (no output): run $PY $EVALS_DIR/judge_case.py by hand"
    [ "$judge_pass" = "true" ] || failures+=("judge FAIL: $judge_reason")
  fi

  local pass=true failures_json='[]' tools_json
  if [ "${#failures[@]}" -gt 0 ]; then
    pass=false
    failures_json="$(printf '%s\n' "${failures[@]}" | jq -R . | jq -s .)"
  fi
  tools_json="$(jq -R . <<<"$tools" | jq -s 'map(select(length > 0))')"
  local cost num_turns
  cost="$(jq -r 'select(.type == "result") | .total_cost_usd // 0' "$tfile" 2>/dev/null | tail -1)"
  num_turns="$(jq -r 'select(.type == "result") | .num_turns // 0' "$tfile" 2>/dev/null | tail -1)"

  jq -n \
    --arg id "$id" \
    --arg severity "$severity" \
    --arg source "$(jq -r '.source_incident' <<<"$cj")" \
    --arg status "$status" \
    --arg transcript "results/$RUN_ID/$id.transcript.jsonl" \
    --argjson pass "$pass" \
    --argjson failures "$failures_json" \
    --argjson tools "$tools_json" \
    --argjson cost "${cost:-0}" \
    --argjson num_turns "${num_turns:-0}" \
    --argjson duration_s "$((ended - started))" \
    '{id: $id, severity: $severity, source_incident: $source, pass: $pass,
      failures: $failures, tools_called: $tools, status: $status,
      cost_usd: $cost, num_turns: $num_turns, duration_s: $duration_s,
      transcript: $transcript}' > "$rfile"

  if [ "$pass" = "true" ]; then
    ok "$id PASS (${severity}, $((ended - started))s)"
  else
    err "$id FAIL (${severity}): $(printf '%s; ' "${failures[@]}")"
  fi
}

# ------------------------------------------------------------------
# execute with bounded concurrency
# ------------------------------------------------------------------
for f in "${CASE_FILES[@]}"; do
  run_one_case "$f" &
  while [ "$(jobs -rp | wc -l)" -ge "$BUDGET" ]; do
    wait -n || true
  done
done
wait || true

# ------------------------------------------------------------------
# assemble results.jsonl + summary.md
# ------------------------------------------------------------------
RESULTS_JSONL="$RUN_DIR/results.jsonl"
: > "$RESULTS_JSONL"
for f in "${CASE_FILES[@]}"; do
  id="$(basename "$f" .yaml)"
  if [ -f "$RUN_DIR/$id.result.json" ]; then
    jq -c . "$RUN_DIR/$id.result.json" >> "$RESULTS_JSONL"
  else
    jq -nc --arg id "$id" '{id: $id, severity: "unknown", pass: false,
      failures: ["runner error: no result file"], tools_called: [],
      status: "error", cost_usd: 0, num_turns: 0, duration_s: 0}' >> "$RESULTS_JSONL"
  fi
done

TOTAL="$(wc -l < "$RESULTS_JSONL")"
PASSED="$(jq -s 'map(select(.pass)) | length' "$RESULTS_JSONL")"
FAILED=$((TOTAL - PASSED))
TOTAL_COST="$(jq -s 'map(.cost_usd) | add // 0' "$RESULTS_JSONL")"
MAX_RUN_COST="${MAX_RUN_COST:-15}"

SUMMARY="$RUN_DIR/summary.md"
{
  printf '# Eval run %s (agent: %s)\n\n' "$RUN_ID" "$AGENT"
  printf -- '- date: %s (Asia/Kuala_Lumpur)\n' "$(TZ=Asia/Kuala_Lumpur date '+%Y-%m-%d %H:%M')"
  printf -- '- cases: %s — **%s pass / %s fail**\n' "$TOTAL" "$PASSED" "$FAILED"
  printf -- '- model: %s, budget: %s, cost: $%s\n\n' "${EVAL_MODEL:-default}" "$BUDGET" "$TOTAL_COST"
  printf '| case | severity | result | failures |\n|---|---|---|---|\n'
  jq -r '"| " + .id + " | " + .severity + " | " +
         (if .pass then "PASS" else "**FAIL**" end) + " | " +
         ((.failures | join("; ")) | gsub("\\|"; "/")) + " |"' "$RESULTS_JSONL"
  if [ "$FAILED" -gt 0 ]; then
    printf '\n## Action required\n\n'
    printf 'A failing case means a regression against a logged incident. Per the\n'
    printf 'P1.8 rule: identify the playbook/skill/hook change that caused it and\n'
    printf 'revert it. Transcripts are next to this file.\n'
  fi
  if awk "BEGIN{exit !($TOTAL_COST > $MAX_RUN_COST)}"; then
    printf '\n## Cost warning\n\nThis run cost $%s, above the $%s guard (MAX_RUN_COST).\nCost has grown run-over-run before ($1.97→$8.26) with no cap — investigate\nbefore the next scheduled run if this keeps climbing.\n' "$TOTAL_COST" "$MAX_RUN_COST"
  fi
} > "$SUMMARY"

cp -f "$SUMMARY" "$RESULTS_DIR/latest-summary${AGENT:+-$AGENT}.md"
[ "$AGENT" = "duke" ] && cp -f "$SUMMARY" "$RESULTS_DIR/latest-summary.md"
cp -f "$RESULTS_JSONL" "$RESULTS_DIR/latest-${AGENT}.jsonl"
[ "$AGENT" = "duke" ] && cp -f "$RESULTS_JSONL" "$RESULTS_DIR/latest.jsonl"
printf '%s pass=%s fail=%s\n' "$RUN_ID" "$PASSED" "$FAILED" > "$RESULTS_DIR/last-run-stamp-${AGENT}"
[ "$AGENT" = "duke" ] && printf '%s pass=%s fail=%s\n' "$RUN_ID" "$PASSED" "$FAILED" > "$RESULTS_DIR/last-run-stamp"

# missed-pulse heartbeat stamp (P0.0f) — best-effort, never fails the run
HB_JSON="$(jq -nc --arg run "$RUN_ID" --argjson pass "$PASSED" --argjson fail "$FAILED" \
  '{run_id: $run, pass: $pass, fail: $fail, ts: (now | todate)}')"
if command -v psql >/dev/null 2>&1; then
  psql --host=/var/run/postgresql --port=5433 -d duke_data -q -c \
    "INSERT INTO project_state (key, value, updated_at)
     VALUES ('heartbeat:${AGENT}-evals', '$HB_JSON'::jsonb, NOW())
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();" \
    >/dev/null 2>&1 && ok "heartbeat stamp written (project_state, heartbeat:${AGENT}-evals)" \
    || warn "heartbeat stamp failed (postgres down?) — run results still on disk"
else
  warn "psql not found — skipped heartbeat stamp"
fi

log "done: $PASSED/$TOTAL pass — summary: $SUMMARY"
[ "$FAILED" -eq 0 ] && exit 0 || exit 1
