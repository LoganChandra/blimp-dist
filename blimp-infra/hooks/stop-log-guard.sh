#!/usr/bin/env bash
# stop-log-guard.sh — Duke v2 P1.2, Stop hook.
#
# Deterministic enforcement of the memory protocol's "log every chat turn"
# rule: if THIS turn's transcript contains no mcp__blimp-brain__log_message
# tool call, block the stop (exit 2, stderr fed back to Claude) exactly once
# so Duke logs before finishing the turn.
#
# Designed to not wedge (the P1.2 spec's hard requirement):
#   - exits 0 instantly unless DUKE_CHANNEL=discord (boot-text-blimp.sh exports
#     it inside the duke tmux session; headless Dreams/eval/`claude -p` runs
#     never see the var, so they can never be Stop-hook-looped at 3am);
#   - exits 0 when stop_hook_active=true (per hooks docs this is set when
#     Claude is already continuing because of a Stop hook — loop prevention);
#   - a /tmp marker keyed by session+turn caps the block at ONCE per turn even
#     if stop_hook_active semantics ever shift;
#   - every parse/read failure fails OPEN (exit 0) — a guard must never trap
#     the agent. `set -e` is deliberately absent for the same reason.
#
# Stdin (hooks API): {"session_id", "transcript_path", "stop_hook_active", ...}
# Turn boundary: the last transcript line that is a REAL user prompt (type
# "user", not isMeta, content is a string or an array with no tool_result
# blocks). Everything after it is "this turn".
set -uo pipefail

# Gate: only live Discord-served Duke sessions are enforced.
[ "${DUKE_CHANNEL:-}" = "discord" ] || exit 0

# No stdin (e.g. run by hand on a tty) or no jq -> nothing we can check; allow.
[ -t 0 ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload="$(cat 2>/dev/null)" || exit 0
[ -n "$payload" ] || exit 0

session="$(jq -r '.session_id // "unknown"' <<<"$payload" 2>/dev/null)" || exit 0
transcript="$(jq -r '.transcript_path // ""' <<<"$payload" 2>/dev/null)" || exit 0
active="$(jq -r '.stop_hook_active // false' <<<"$payload" 2>/dev/null)" || exit 0

# Already continuing because of a Stop hook -> never block twice.
[ "$active" = "true" ] && exit 0

# Transcript missing/unreadable -> fail open.
[ -n "$transcript" ] && [ -r "$transcript" ] || exit 0

# One jq pass over the transcript tail:
#  - find the index of the last real user prompt (turn start),
#  - count log_message tool_use blocks at/after it,
#  - emit "<turn-id>\t<count>" (turn-id = that prompt's uuid, for the marker).
result="$(tail -n 5000 -- "$transcript" 2>/dev/null | jq -Rrs '
  split("\n")
  | map(fromjson? // empty)
  | map(select(type == "object"))
  | . as $lines
  | ([ $lines | to_entries[]
       | select(.value.type? == "user")
       | select((.value.isMeta? // false) | not)
       | select(
           ((.value.message.content? | type) == "string")
           or (((.value.message.content? | type) == "array")
               and (([ .value.message.content[]? | select(.type? == "tool_result") ] | length) == 0))
         )
       | .key
     ]) as $prompts
  | if ($prompts | length) == 0
    then "noturn\t0"
    else ($prompts | max) as $start
       | (($lines[$start].uuid? // ($start | tostring)) | tostring) as $turn
       | ([ $lines[$start:][]
            | select(.type? == "assistant")
            | .message.content[]?
            | select(.type? == "tool_use")
            | select((.name? // "") | test("(^|__)log_message$"))
          ] | length) as $n
       | "\($turn)\t\($n)"
    end
' 2>/dev/null)" || exit 0
[ -n "$result" ] || exit 0

turn="${result%%$'\t'*}"
count="${result##*$'\t'}"

# Could not identify a turn, or count is non-numeric -> fail open.
[ "$turn" = "noturn" ] && exit 0
case "$count" in '' | *[!0-9]*) exit 0 ;; esac

# Turn already logged -> allow.
[ "$count" -ge 1 ] && exit 0

# Max once per turn: marker in /tmp keyed by session+turn.
key="$(printf '%s' "${session}:${turn}" | md5sum 2>/dev/null | cut -d' ' -f1)" || exit 0
[ -n "$key" ] || exit 0
marker="/tmp/.duke-stop-guard-${key}"
[ -e "$marker" ] && exit 0
touch "$marker" 2>/dev/null || true
# Opportunistic cleanup of markers older than a day (cheap, best-effort).
find /tmp -maxdepth 1 -name '.duke-stop-guard-*' -mmin +1440 -delete 2>/dev/null || true

echo "Stop blocked (once per turn): no log_message call this turn — call mcp__blimp-brain__log_message(channel=<this channel>, speaker=\"duke\", message=<1-line summary of this turn>) so Hermes can see it, then finish." >&2
exit 2
