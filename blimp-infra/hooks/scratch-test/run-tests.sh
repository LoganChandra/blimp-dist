#!/usr/bin/env bash
# run-tests.sh — Duke v2 P1.2 hook test harness ("verify, don't assert").
#
# Self-contained: builds /tmp/duke-hook-scratch with its own minimal
# .claude/settings.json, fixture transcripts (one turn WITH log_message, one
# WITHOUT — the without-file has an EARLIER turn that did log, to prove
# turn-scoping), and fixture stdin payloads. Then invokes each hook script
# exactly the way the hooks API does (JSON on stdin) and asserts exit codes,
# stderr, and stdout. Safe to run repeatedly; touches nothing outside
# /tmp/duke-hook-scratch and the hooks' own /tmp/.duke-stop-guard-* markers.
#
# Usage: bash run-tests.sh        # exits 0 only if every case passes
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="$(cd "$HERE/.." && pwd)"
FIXTURES="$HERE/fixtures"
SCRATCH="/tmp/duke-hook-scratch"

STOP_HOOK="$HOOKS_DIR/stop-log-guard.sh"
PRETOOL_HOOK="$HOOKS_DIR/pretooluse-prod-guard.sh"
PROMPT_HOOK="$HOOKS_DIR/promptsubmit-context.sh"

PASS=0
FAIL=0
FAILED_NAMES=()

# ---------------------------------------------------------------- setup ----

setup() {
  rm -rf "$SCRATCH"
  mkdir -p "$SCRATCH/.claude" "$SCRATCH/transcripts" "$SCRATCH/payloads"

  cp "$FIXTURES/transcript-with-log.jsonl"    "$SCRATCH/transcripts/with-log.jsonl"
  cp "$FIXTURES/transcript-without-log.jsonl" "$SCRATCH/transcripts/without-log.jsonl"
  cp "$FIXTURES"/pretool-*.json "$FIXTURES/promptsubmit-payload.json" "$SCRATCH/payloads/"

  # Minimal scratch settings.json — same wiring the Boss will merge for real.
  sed "s|__HOOKS_DIR__|$HOOKS_DIR|g" "$FIXTURES/scratch-settings.json.tpl" \
    > "$SCRATCH/.claude/settings.json"
  jq -e '.hooks.Stop and .hooks.PreToolUse and .hooks.UserPromptSubmit' \
    "$SCRATCH/.claude/settings.json" >/dev/null

  # Stop-hook payloads from the template (unique session ids per case so the
  # once-per-turn markers never collide between cases or reruns).
  local run_id
  run_id="$$-$(date +%s)"
  mk_stop_payload "gateoff-$run_id" "$SCRATCH/transcripts/without-log.jsonl" false > "$SCRATCH/payloads/stop-gateoff.json"
  mk_stop_payload "withlog-$run_id" "$SCRATCH/transcripts/with-log.jsonl"    false > "$SCRATCH/payloads/stop-withlog.json"
  mk_stop_payload "nolog-$run_id"   "$SCRATCH/transcripts/without-log.jsonl" false > "$SCRATCH/payloads/stop-nolog.json"
  mk_stop_payload "active-$run_id"  "$SCRATCH/transcripts/without-log.jsonl" true  > "$SCRATCH/payloads/stop-active.json"
  mk_stop_payload "missing-$run_id" "$SCRATCH/transcripts/does-not-exist.jsonl" false > "$SCRATCH/payloads/stop-missing.json"
}

mk_stop_payload() { # session transcript stop_hook_active
  sed -e "s|__SESSION__|sess-$1|" \
      -e "s|__TRANSCRIPT__|$2|" \
      -e "s|__ACTIVE__|$3|" \
      "$FIXTURES/stop-payload.json.tpl"
}

# ------------------------------------------------------------- assertion ---

# check NAME EXPECTED_EXIT SCRIPT PAYLOAD CHANNEL [STDOUT_EXPECT] [STDERR_EXPECT]
#   CHANNEL: "discord" sets DUKE_CHANNEL=discord, "" runs with it unset.
#   STDOUT_EXPECT/STDERR_EXPECT: grep -q pattern, or "EMPTY" to assert no output.
check() {
  local name="$1" expect="$2" script="$3" payload="$4" channel="$5"
  local want_out="${6:-}" want_err="${7:-}"
  local out="$SCRATCH/last-stdout.txt" err="$SCRATCH/last-stderr.txt" rc=0 ok=1 why=""

  set +e
  if [ -n "$channel" ]; then
    DUKE_CHANNEL="$channel" bash "$script" < "$payload" > "$out" 2> "$err"
  else
    env -u DUKE_CHANNEL bash "$script" < "$payload" > "$out" 2> "$err"
  fi
  rc=$?
  set -e

  if [ "$rc" -ne "$expect" ]; then ok=0; why="exit=$rc want=$expect"; fi
  if [ -n "$want_out" ]; then
    if [ "$want_out" = "EMPTY" ]; then
      [ -s "$out" ] && { ok=0; why="$why; expected empty stdout"; }
    else
      grep -q "$want_out" "$out" || { ok=0; why="$why; stdout missing '$want_out'"; }
    fi
  fi
  if [ -n "$want_err" ]; then
    if [ "$want_err" = "EMPTY" ]; then
      [ -s "$err" ] && { ok=0; why="$why; expected empty stderr"; }
    else
      grep -q "$want_err" "$err" || { ok=0; why="$why; stderr missing '$want_err'"; }
    fi
  fi

  if [ "$ok" -eq 1 ]; then
    PASS=$((PASS + 1))
    echo "PASS  $name"
  else
    FAIL=$((FAIL + 1))
    FAILED_NAMES+=("$name")
    echo "FAIL  $name ($why)"
    sed 's/^/      stderr: /' "$err" | head -3
    sed 's/^/      stdout: /' "$out" | head -3
  fi
}

# ----------------------------------------------------------------- tests ---

echo "== P1.2 hook tests (scratch: $SCRATCH) =="

bash -n "$STOP_HOOK" && bash -n "$PRETOOL_HOOK" && bash -n "$PROMPT_HOOK"
echo "PASS  syntax: bash -n on all three hooks"
PASS=$((PASS + 1))

setup

P="$SCRATCH/payloads"

echo "-- stop-log-guard.sh --"
check "stop: DUKE_CHANNEL unset -> allow (gate off)"            0 "$STOP_HOOK" "$P/stop-gateoff.json" ""        EMPTY EMPTY
check "stop: turn WITH log_message -> allow"                    0 "$STOP_HOOK" "$P/stop-withlog.json" "discord" EMPTY EMPTY
check "stop: turn WITHOUT log (prior turn logged) -> block"     2 "$STOP_HOOK" "$P/stop-nolog.json"   "discord" ""    "log_message"
check "stop: same turn again -> allow (once-per-turn marker)"   0 "$STOP_HOOK" "$P/stop-nolog.json"   "discord" EMPTY EMPTY
check "stop: stop_hook_active=true -> allow (loop prevention)"  0 "$STOP_HOOK" "$P/stop-active.json"  "discord" EMPTY EMPTY
check "stop: missing transcript -> allow (fail open)"           0 "$STOP_HOOK" "$P/stop-missing.json" "discord" EMPTY EMPTY

echo "-- pretooluse-prod-guard.sh --"
check "pretool: DUKE_CHANNEL unset -> allow (gate off)"         0 "$PRETOOL_HOOK" "$P/pretool-git-push-main.json"    ""        EMPTY EMPTY
check "pretool: git push origin main -> block"                  2 "$PRETOOL_HOOK" "$P/pretool-git-push-main.json"    "discord" ""    "the Boss"
check "pretool: bare git push -> block"                         2 "$PRETOOL_HOOK" "$P/pretool-git-push-bare.json"    "discord" ""    "prod-guard"
check "pretool: git push --force -> block"                      2 "$PRETOOL_HOOK" "$P/pretool-git-push-force.json"   "discord" ""    "force"
check "pretool: git push origin HEAD -> block"                  2 "$PRETOOL_HOOK" "$P/pretool-git-push-head.json"    "discord" ""    "prod-guard"
check "pretool: git push origin <feature> -> allow"             0 "$PRETOOL_HOOK" "$P/pretool-git-push-feature.json" "discord" EMPTY EMPTY
check "pretool: vercel deploy --prod -> block"                  2 "$PRETOOL_HOOK" "$P/pretool-vercel-prod.json"      "discord" ""    "vercel"
check "pretool: supabase db push -> block"                      2 "$PRETOOL_HOOK" "$P/pretool-supabase-db-push.json" "discord" ""    "supabase"
check "pretool: git status / git log -> allow"                  0 "$PRETOOL_HOOK" "$P/pretool-safe.json"             "discord" EMPTY EMPTY
check "pretool: non-Bash tool -> allow"                         0 "$PRETOOL_HOOK" "$P/pretool-non-bash.json"         "discord" EMPTY EMPTY

echo "-- promptsubmit-context.sh --"
check "prompt: DUKE_CHANNEL unset -> exit 0, no output"         0 "$PROMPT_HOOK" "$P/promptsubmit-payload.json" ""        EMPTY EMPTY
check "prompt: gated on -> exit 0, context line"                0 "$PROMPT_HOOK" "$P/promptsubmit-payload.json" "discord" "recall-first"
check "prompt: gated on -> open-task count present"             0 "$PROMPT_HOOK" "$P/promptsubmit-payload.json" "discord" "open tasks in duke_data:"

# -------------------------------------------------------------- summary ----

echo
echo "== $PASS passed, $FAIL failed =="
if [ "$FAIL" -gt 0 ]; then
  printf 'failed: %s\n' "${FAILED_NAMES[@]}"
  exit 1
fi
exit 0
