#!/usr/bin/env bash
# launch.sh <claude args...>: start a Claude Code agent in the current directory, resuming its last
# real Discord session so a restart keeps the thread. Used by boot-text-blimp.sh (Duke) and
# boot-text-frank.sh (Frank). Hermes resumes natively (hermes-gateway keeps sessions in state.db).
#
#   BLIMP_FRESH=1 duke-up    start with a blank session instead
#
# If the resume fails fast (session gone or unreadable), it falls back to a fresh start, so a
# restart never leaves the agent offline. A normal exit after a real run does not relaunch.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The watchdog leaves this marker after killing a runaway session: that transcript must not come back.
marker="$HOME/.cache/blimp-agent/fresh-${BLIMP_AGENT:-}"
[ -n "${BLIMP_AGENT:-}" ] && [ -e "$marker" ] && { rm -f "$marker"; BLIMP_FRESH=1; echo "[launch] fresh start (runaway marker)"; }
sid=""
[ -z "${BLIMP_FRESH:-}" ] && sid="$(python3 "$here/last-session.py" "$PWD" 2>/dev/null)"
if [ -n "$sid" ]; then
  echo "[launch] resuming session $sid (BLIMP_FRESH=1 for a blank one)"
  start=$(date +%s)
  claude --resume "$sid" "$@"
  rc=$?
  [ $rc -eq 0 ] || [ $(( $(date +%s) - start )) -ge 60 ] && exit $rc
  echo "[launch] resume failed (exit $rc); starting fresh"
fi
exec claude "$@"
