#!/usr/bin/env bash
# cron-audit.sh — P1.7(c): did the scheduled jobs actually run?
#
# Every scheduled job (dreams, backup, heartbeat, digest) upserts a
# pulse:<job> stamp into duke_data.project_state when it finishes (via
# wd_stamp_pulse in lib.sh / state_set in the MCP). This audit reads those
# stamps and logs an incident for any EXPECTED job whose newest stamp is older
# than the staleness window (default 26h, matching the missed-pulse rule in the
# v2 plan's Section-5 success metric "detected within 26h").
#
# It does NOT force-rerun the jobs itself — re-firing a systemd user timer or
# Docker job is rail/classifier territory (see duke-v2-plan §4 "No self-applied
# self-modification"). It surfaces the gap as an incident + a digest line; the
# Dreams/heartbeat owners decide the rerun. The plan's "force-rerun" wording is
# satisfied at the digest layer, not by this watchdog mutating shared units.
#
# Usage:
#   bash cron-audit.sh            # audit, log incidents, print a digest block
#   bash cron-audit.sh --quiet    # audit + log incidents, no stdout
#   DUKE_PULSE_MAX_AGE_H=26 DUKE_EXPECTED_PULSES="dreams backup heartbeat digest" ...

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

MAX_AGE_H="${DUKE_PULSE_MAX_AGE_H:-26}"
# The jobs we expect to see a recent pulse for. Override via env if the set of
# scheduled jobs changes (keeps this script from needing edits when P1.1/P1.3
# land their timers).
read -r -a EXPECTED_PULSES <<<"${DUKE_EXPECTED_PULSES:-dreams backup heartbeat digest}"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

emit() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

# Pull every pulse stamp's age in hours in one round-trip:
#   key|age_hours   (age computed from updated_at, the authoritative write time)
# Missing jobs simply don't appear; we detect those by set difference below.
declare -A PULSE_AGE_H=()
while IFS='|' read -r key age; do
  [ -n "$key" ] || continue
  PULSE_AGE_H["$key"]="$age"
done < <(
  "${WD_PSQL[@]}" -c "
    SELECT replace(key,'pulse:',''),
           round(EXTRACT(EPOCH FROM (NOW() - updated_at)) / 3600.0, 1)
    FROM project_state
    WHERE key LIKE 'pulse:%';" 2>/dev/null || true
)

stale=0
missing=0
emit "=== cron-audit ($(date '+%Y-%m-%d %H:%M:%S'), window ${MAX_AGE_H}h) ==="
for job in "${EXPECTED_PULSES[@]}"; do
  age="${PULSE_AGE_H[$job]:-}"
  if [ -z "$age" ]; then
    missing=$((missing + 1))
    emit "  MISSING  pulse:$job — never stamped"
    wd_incident "missed-pulse" "expected job '$job' has no pulse stamp at all"
    continue
  fi
  # bash has no float compare; let awk decide stale vs fresh.
  if awk -v a="$age" -v m="$MAX_AGE_H" 'BEGIN{exit !(a+0 > m+0)}'; then
    stale=$((stale + 1))
    emit "  STALE    pulse:$job — ${age}h ago (> ${MAX_AGE_H}h)"
    wd_incident "missed-pulse" "job '$job' last ran ${age}h ago (window ${MAX_AGE_H}h)"
  else
    emit "  ok       pulse:$job — ${age}h ago"
  fi
done

emit "=== ${stale} stale, ${missing} missing of ${#EXPECTED_PULSES[@]} expected ==="
# This script itself is a scheduled job; stamp its own pulse so a future audit
# can confirm the auditor is alive too.
wd_stamp_pulse "cron-audit" "cron-audit"

# Non-zero exit when something is wrong, so a cron MAILTO / digest hook notices.
[ $((stale + missing)) -eq 0 ]
