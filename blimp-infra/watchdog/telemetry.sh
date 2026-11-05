#!/usr/bin/env bash
# telemetry.sh — P1.7(e): per-session token/cost telemetry from the JSONL
# transcript, appended to a daily JSONL under ~/blimp/telemetry/.
#
# One append per run, keyed to the live Duke session transcript. The morning
# digest / P1 budget rule reads these files to report "tokens/run measured
# actuals" — the plan's token-budget machinery is fed from here, not from
# tmux scrollback.
#
# Cost is computed from a small per-model rate table (USD per million tokens).
# Rates are best-effort and overridable via env; tokens are the ground truth,
# cost is a convenience column. cache_read is priced separately (cheap).
#
# Usage:
#   bash telemetry.sh                 # snapshot the live Duke session
#   bash telemetry.sh <transcript>    # snapshot a specific transcript (tests)
#   bash telemetry.sh --print <t>     # print the JSON, don't append

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

TELEMETRY_DIR="${DUKE_TELEMETRY_DIR:-$HOME/blimp/telemetry}"

PRINT_ONLY=0
if [ "${1:-}" = "--print" ]; then PRINT_ONLY=1; shift; fi

TRANSCRIPT="${1:-}"
SESSION_LABEL="duke"
if [ -z "$TRANSCRIPT" ]; then
  TRANSCRIPT="$(wd_resolve_duke_transcript || true)"
fi
[ -n "$TRANSCRIPT" ] && SESSION_LABEL="$(basename "$TRANSCRIPT" .jsonl)"

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  echo "telemetry: no transcript to snapshot" >&2
  exit 0
fi

base="$(wd_telemetry_json "$TRANSCRIPT" "$SESSION_LABEL")"
if [ -z "$base" ]; then
  echo "telemetry: could not parse $TRANSCRIPT" >&2
  exit 0
fi

# Cost estimate (USD/million tokens). Cache-read is ~1/10 input; cache-create
# ~1.25x input. Numbers are conservative placeholders; override with env if the
# rate card changes — the goal is a directional cost column, not invoicing.
RATE_IN="${DUKE_RATE_IN_PER_M:-5}"        # input
RATE_OUT="${DUKE_RATE_OUT_PER_M:-25}"     # output
RATE_CC="${DUKE_RATE_CC_PER_M:-6.25}"     # cache creation
RATE_CR="${DUKE_RATE_CR_PER_M:-0.5}"      # cache read

record="$(printf '%s' "$base" | jq -c \
  --argjson rin "$RATE_IN" --argjson rout "$RATE_OUT" \
  --argjson rcc "$RATE_CC" --argjson rcr "$RATE_CR" '
  . + { est_cost_usd: ( ( (.input_tokens          * $rin)
                        + (.output_tokens         * $rout)
                        + (.cache_creation_tokens * $rcc)
                        + (.cache_read_tokens     * $rcr) ) / 1000000
                        | (. * 10000 | round) / 10000 ) }
')"

if [ "$PRINT_ONLY" -eq 1 ]; then
  printf '%s\n' "$record"
  exit 0
fi

mkdir -p "$TELEMETRY_DIR"
outfile="$TELEMETRY_DIR/$(date +%Y-%m-%d).jsonl"
printf '%s\n' "$record" >> "$outfile"
echo "telemetry: appended session $SESSION_LABEL to $outfile"

# Stamp a pulse so the cron-audit knows telemetry is running.
wd_stamp_pulse "telemetry" "telemetry"
