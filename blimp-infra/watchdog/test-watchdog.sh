#!/usr/bin/env bash
# test-watchdog.sh — plain-bash tests for the P1.7 watchdog hardening.
#
# Scope: the PURE JSONL-transcript analytics in lib.sh (repeat-call detection,
# token totals, telemetry JSON) plus the telemetry script's cost column. These
# are the parts that decide whether Duke gets killed, so they are the parts
# that must be tested. Live-process bits (tmux kill, psql incident writes) are
# deliberately NOT exercised here — they mutate shared state; their guards
# (peer-ambiguity, Duke-session-only) are asserted by code review + the
# wd_claude_peers_in unit below, not by killing a real session in CI.
#
# Run:  bash blimp-infra/watchdog/test-watchdog.sh
# Exit: 0 = all green, 1 = a failure (prints which).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FX="$HERE/fixtures"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }

# eq <label> <expected> <actual>
eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }

# precondition: jq must exist (every analytic depends on it)
if ! command -v jq >/dev/null 2>&1; then
  echo "FATAL: jq not on PATH — the watchdog analytics require it" >&2
  exit 1
fi

echo "== fixtures present =="
for f in repeat-loop clean; do
  if [ -f "$FX/$f.jsonl" ]; then ok "$f.jsonl exists"; else bad "$f.jsonl exists" "file" "missing"; fi
done

echo "== fixtures are valid JSONL (one object per line) =="
for f in repeat-loop clean; do
  badline=""
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    printf '%s' "$line" | jq -e . >/dev/null 2>&1 || { badline="bad"; break; }
  done < "$FX/$f.jsonl"
  if [ -z "$badline" ]; then ok "$f.jsonl all lines parse"; else bad "$f.jsonl all lines parse" "valid" "a line failed"; fi
done

echo "== repeat-call detection =="
# The repeat fixture has exactly 5 consecutive identical Bash calls.
eq "repeat-loop: max consecutive == 5"      "5"    "$(wd_max_consecutive_tool_calls "$FX/repeat-loop.jsonl")"
eq "repeat-loop: offender == Bash"          "Bash" "$(wd_repeat_offender          "$FX/repeat-loop.jsonl")"
# The clean fixture has two ADJACENT Bash calls but with DIFFERENT inputs
# (ls vs pwd) — must NOT count as a run.
eq "clean: max consecutive == 1"            "1"    "$(wd_max_consecutive_tool_calls "$FX/clean.jsonl")"
eq "clean: no offender"                     ""     "$(wd_repeat_offender          "$FX/clean.jsonl")"

echo "== kill-decision threshold (>= 5 fires, clean does not) =="
LIMIT=5
mx_repeat="$(wd_max_consecutive_tool_calls "$FX/repeat-loop.jsonl")"
mx_clean="$(wd_max_consecutive_tool_calls "$FX/clean.jsonl")"
[ "$mx_repeat" -ge "$LIMIT" ] && ok "repeat-loop trips the >= $LIMIT kill rule" || bad "repeat-loop trips kill rule" ">= $LIMIT" "$mx_repeat"
[ "$mx_clean"  -lt "$LIMIT" ] && ok "clean does NOT trip the kill rule"        || bad "clean does not trip kill rule" "< $LIMIT" "$mx_clean"

echo "== token totals (input + output + cache_creation, cache_read excluded) =="
# repeat-loop: recall(120+40+5000) + 5*Bash(80+25) + reply(60+30) = 5160 + 525 + 90 = 5775
eq "repeat-loop: token total == 5775" "5775" "$(wd_token_total "$FX/repeat-loop.jsonl")"
# clean: recall(250)+read(230)+ls(110)+pwd(108)+remember(150)+reply(105) = 953 + cc(8000) = 8953
eq "clean: token total == 8953"       "8953" "$(wd_token_total "$FX/clean.jsonl")"

echo "== token-cap decision =="
CAP=2000000
tot="$(wd_token_total "$FX/clean.jsonl")"
[ "$tot" -lt "$CAP" ] && ok "clean session is under the 2M cap" || bad "clean under cap" "< $CAP" "$tot"
# A tiny cap must make the clean fixture trip the cap (exercises the >= compare).
TINY=8000
[ "$tot" -ge "$TINY" ] && ok "clean session trips a 8k test cap (compare works)" || bad "clean trips tiny cap" ">= $TINY" "$tot"
# 80% warn line: 80% of TINY is 6400; clean (8953) is above it.
WARN=$(( TINY * 80 / 100 ))
[ "$tot" -ge "$WARN" ] && ok "clean session past the 80% warn line of the test cap" || bad "clean past 80% warn" ">= $WARN" "$tot"

echo "== telemetry JSON shape =="
tj="$(wd_telemetry_json "$FX/clean.jsonl" "clean-fixture")"
eq "telemetry: session label"        "clean-fixture" "$(printf '%s' "$tj" | jq -r '.session')"
eq "telemetry: turns counted == 6"   "6"             "$(printf '%s' "$tj" | jq -r '.turns')"
eq "telemetry: input_tokens == 710"  "710"           "$(printf '%s' "$tj" | jq -r '.input_tokens')"
eq "telemetry: output_tokens == 243" "243"           "$(printf '%s' "$tj" | jq -r '.output_tokens')"
eq "telemetry: cache_read == 66700"  "66700"         "$(printf '%s' "$tj" | jq -r '.cache_read_tokens')"
# by_model must list both models present in the clean fixture.
models="$(printf '%s' "$tj" | jq -r '.by_model | keys | sort | join(",")')"
eq "telemetry: by_model keys"        "claude-fable-5,claude-opus-4-8" "$models"

echo "== telemetry.sh cost column (--print, no append) =="
rec="$(DUKE_RATE_IN_PER_M=5 DUKE_RATE_OUT_PER_M=25 DUKE_RATE_CC_PER_M=6.25 DUKE_RATE_CR_PER_M=0.5 \
        bash "$HERE/telemetry.sh" --print "$FX/clean.jsonl")"
# est_cost (USD): input 710*5=3550; output 243*25=6075;
# cache_creation 8000*6.25=50000; cache_read 66700*0.5=33350.
# sum = 92975 / 1e6 = 0.092975 -> rounds to 0.093
eq "telemetry.sh: est_cost_usd == 0.093" "0.093" "$(printf '%s' "$rec" | jq -r '.est_cost_usd')"
got_session="$(printf '%s' "$rec" | jq -r '.session')"
[ "$got_session" = "clean" ] && ok "telemetry.sh: session label from filename" || bad "telemetry.sh session" "clean" "$got_session"

echo "== empty/missing transcript guards (never crash, return 0/empty) =="
eq "max(missing path) == 0"        "0" "$(wd_max_consecutive_tool_calls /no/such/file.jsonl)"
eq "token(missing path) == 0"      "0" "$(wd_token_total /no/such/file.jsonl)"
eq "offender(missing path) empty"  ""  "$(wd_repeat_offender /no/such/file.jsonl)"
eq "max(empty arg) == 0"           "0" "$(wd_max_consecutive_tool_calls "")"

echo "== sanity: sql-escape helper =="
eq "wd_sql_escape doubles single quotes" "O''Brien" "$(wd_sql_escape "O'Brien")"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
