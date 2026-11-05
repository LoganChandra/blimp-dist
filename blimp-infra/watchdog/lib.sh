#!/usr/bin/env bash
# lib.sh — shared helpers for the Duke watchdog (P1.7).
# Sourced by watchdog.sh, cron-audit.sh, telemetry.sh. Defines functions only;
# sourcing this file has no side effects.
#
# Ground truth lives in two places:
#   - Claude Code JSONL transcripts: ~/.claude/projects/<sanitized-cwd>/<session>.jsonl
#   - Postgres duke_data (port 5433, unix socket): project_state pulse:%/incident:% keys

# Postgres duke_data on the WSL instance (Hindsight's embedded pg squats 5432).
WD_PSQL=(psql --host=/var/run/postgresql --port=5433 -d duke_data -X -q -At)

wd_sql_escape() { printf '%s' "$1" | sed "s/'/''/g"; }

# Claude Code sanitizes the session cwd into a project dir name by replacing
# every non-alphanumeric character with '-'. Verified against the live dir:
# "$HOME/blimp/repo"
#   -> "-home-USER-blimp-repo"
wd_sanitize_path() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }

# wd_incident <type> <detail>
# Writes an incident note into project_state (key incident:watchdog:<epoch>:<type>)
# via psql, and best-effort mirrors it into the brain via duke-mem so it shows
# up in recall. Never fails the caller — incidents must not crash the watchdog.
wd_incident() {
  local type="$1" detail="$2" ts key val
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  key="incident:watchdog:$(date +%s):${type}"
  val="$(jq -cn --arg type "$type" --arg detail "$detail" --arg at "$ts" \
        '{type:$type, detail:$detail, at:$at, source:"watchdog"}')"
  "${WD_PSQL[@]}" -c "INSERT INTO project_state (key, value)
      VALUES ('$(wd_sql_escape "$key")', '$(wd_sql_escape "$val")'::jsonb)
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();" \
      >/dev/null 2>&1 || true
  # The brain mirror is append-only and has no CLI delete — set
  # DUKE_INCIDENT_NO_BRAIN=1 for dry-runs / tests / verification so they don't
  # leave un-removable noise in Hindsight. The psql row above is cheap to clean.
  if [ "${DUKE_INCIDENT_NO_BRAIN:-0}" != "1" ] && command -v duke-mem >/dev/null 2>&1; then
    duke-mem remember \
      --content "WATCHDOG INCIDENT [$type] $ts: $detail" \
      --tags "watchdog,incident,$type" \
      --source "blimp-infra/watchdog" >/dev/null 2>&1 || true
  fi
}

# wd_stamp_pulse <job> [by]
# Heartbeat stamp per P0.0(f): every scheduled job upserts pulse:<job> when it
# completes; the cron-audit flags any stamp older than 26h.
wd_stamp_pulse() {
  local job="$1" by="${2:-watchdog}" val
  val="$(jq -cn --arg by "$by" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{by:$by, at:$at}')"
  "${WD_PSQL[@]}" -c "INSERT INTO project_state (key, value)
      VALUES ('pulse:$(wd_sql_escape "$job")', '$(wd_sql_escape "$val")'::jsonb)
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();" \
      >/dev/null 2>&1 || true
}

# wd_find_duke_claude_pid — PID of the claude CLI inside the 'duke' tmux
# session, or "" when Duke isn't up. The pane process is usually the shell
# tmux spawned, with claude as its child — check pane + 2 levels of children.
wd_find_duke_claude_pid() {
  local pane_pid pid kid args
  pane_pid="$(tmux list-panes -t duke -F '#{pane_pid}' 2>/dev/null | head -n1 || true)"
  if [ -z "${pane_pid:-}" ]; then echo ""; return 0; fi
  for pid in "$pane_pid" $(pgrep -P "$pane_pid" 2>/dev/null || true); do
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    case "$args" in claude|claude\ *|*/claude|*/claude\ *) echo "$pid"; return 0;; esac
    for kid in $(pgrep -P "$pid" 2>/dev/null || true); do
      args="$(ps -o args= -p "$kid" 2>/dev/null || true)"
      case "$args" in claude|claude\ *|*/claude|*/claude\ *) echo "$kid"; return 0;; esac
    done
  done
  echo ""
}

# wd_project_dir_for_pid <pid> — the ~/.claude/projects/ dir for that
# process's cwd (symlinks resolved, matching Claude Code's behavior), or "".
wd_project_dir_for_pid() {
  local pid="$1" cwd dir
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  if [ -z "$cwd" ]; then echo ""; return 0; fi
  dir="$HOME/.claude/projects/$(wd_sanitize_path "$cwd")"
  if [ -d "$dir" ]; then echo "$dir"; else echo ""; fi
}

# wd_resolve_duke_transcript [pid] — path to the live Duke session's JSONL
# transcript, or "". Heuristic: the newest *.jsonl in the project dir that was
# modified AFTER the claude process started. The claude CLI does not hold the
# transcript fd open (verified 2026-06-10), so newest-since-start is the best
# available mapping. Misattribution risk (another claude session in the same
# cwd) is handled by wd_claude_peers_in + the no-kill-when-ambiguous rule.
wd_resolve_duke_transcript() {
  local pid="${1:-}" dir etimes start f
  [ -n "$pid" ] || pid="$(wd_find_duke_claude_pid)"
  if [ -z "$pid" ]; then echo ""; return 0; fi
  dir="$(wd_project_dir_for_pid "$pid")"
  if [ -z "$dir" ]; then echo ""; return 0; fi
  etimes="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  start=$(( $(date +%s) - ${etimes:-0} ))
  f="$(find "$dir" -maxdepth 1 -name '*.jsonl' -newermt "@$start" -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn | head -n1 | cut -d' ' -f2- || true)"
  echo "${f:-}"
}

# wd_claude_peers_in <project_dir> <duke_pid> — count of OTHER claude processes
# whose cwd maps to the same project dir. >0 means the newest-transcript
# heuristic is ambiguous (e.g. a headless `claude -p` run in the repo) and the
# watchdog must NOT kill on its evidence — incident only.
wd_claude_peers_in() {
  local dir="$1" duke_pid="$2" pid n=0
  for pid in $(pgrep -x claude 2>/dev/null || true); do
    [ "$pid" = "$duke_pid" ] && continue
    [ "$(wd_project_dir_for_pid "$pid")" = "$dir" ] && n=$((n + 1))
  done
  echo "$n"
}

# ---------------------------------------------------------------------------
# JSONL transcript analytics (pure functions — take a path, no side effects,
# no tmux/psql/process deps, so the test suite can exercise them on fixtures).
#
# Schema (verified against a live transcript 2026-06-10):
#   assistant lines: {type:"assistant", message:{model, usage:{...},
#                     content:[{type:"tool_use", name, input}, ...]}}
#   usage:           {input_tokens, output_tokens,
#                     cache_creation_input_tokens, cache_read_input_tokens}
# ---------------------------------------------------------------------------

# wd_max_consecutive_tool_calls <transcript> — the length of the LONGEST run of
# identical consecutive tool calls (same name AND same input) across the whole
# transcript, in tool-call order. A tool's signature is "<name>\t<input-json>".
# Empty/absent transcript -> 0. This is the $150-class repeat-loop detector.
wd_max_consecutive_tool_calls() {
  local transcript="$1"
  [ -n "$transcript" ] && [ -f "$transcript" ] || { echo 0; return 0; }
  # Emit one "name<TAB>compact-input" line per tool_use, in document order,
  # then run-length the max identical-consecutive streak in awk.
  jq -rc 'select(.type=="assistant")
            | .message.content[]?
            | select(.type=="tool_use")
            | (.name + "\t" + (.input|tostring))' "$transcript" 2>/dev/null \
    | awk '
        { sig = $0 }
        sig == prev { run++; if (run > max) max = run; next }
        { prev = sig; run = 1; if (run > max) max = run }
        END { print (max == "" ? 0 : max) }
      '
}

# wd_repeat_offender <transcript> — the name of the tool in the longest
# consecutive run (for the incident note), or "" if there is no run >= 2.
wd_repeat_offender() {
  local transcript="$1"
  [ -n "$transcript" ] && [ -f "$transcript" ] || { echo ""; return 0; }
  jq -rc 'select(.type=="assistant")
            | .message.content[]?
            | select(.type=="tool_use")
            | (.name + "\t" + (.input|tostring))' "$transcript" 2>/dev/null \
    | awk -F'\t' '
        { sig = $0; name = $1 }
        sig == prev { run++; if (run > max) { max = run; off = name }; next }
        { prev = sig; run = 1; if (run > max) { max = run; off = name } }
        END { if (max >= 2) print off }
      '
}

# wd_token_total <transcript> — total billable input+output tokens for the
# session (cache reads excluded; they are cheap and not the burn surface the
# cap protects against). cache_creation IS counted — it is full-price writes.
# Absent transcript -> 0.
wd_token_total() {
  local transcript="$1"
  [ -n "$transcript" ] && [ -f "$transcript" ] || { echo 0; return 0; }
  jq -s '[ .[]
           | select(.type=="assistant")
           | .message.usage
           | ( (.input_tokens // 0)
             + (.output_tokens // 0)
             + (.cache_creation_input_tokens // 0) ) ]
         | add // 0' "$transcript" 2>/dev/null || echo 0
}

# wd_telemetry_json <transcript> <session_label> — one compact JSON object
# summarizing the session's token usage per model, for the daily telemetry
# JSONL. Breaks out cache reads separately so cost can be computed downstream.
wd_telemetry_json() {
  local transcript="$1" label="${2:-unknown}"
  [ -n "$transcript" ] && [ -f "$transcript" ] || { echo ""; return 0; }
  jq -s --arg label "$label" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
    [ .[] | select(.type=="assistant") | {model: (.message.model // "unknown"), u: .message.usage} ]
    | { at: $at,
        session: $label,
        turns: length,
        input_tokens:          (map(.u.input_tokens // 0)                 | add // 0),
        output_tokens:         (map(.u.output_tokens // 0)                | add // 0),
        cache_creation_tokens: (map(.u.cache_creation_input_tokens // 0)  | add // 0),
        cache_read_tokens:     (map(.u.cache_read_input_tokens // 0)      | add // 0),
        by_model: ( group_by(.model)
                    | map({ (.[0].model):
                            { turns: length,
                              input_tokens:          (map(.u.input_tokens // 0)                | add // 0),
                              output_tokens:         (map(.u.output_tokens // 0)               | add // 0),
                              cache_creation_tokens: (map(.u.cache_creation_input_tokens // 0) | add // 0),
                              cache_read_tokens:     (map(.u.cache_read_input_tokens // 0)     | add // 0) } })
                    | add // {} ) }
  ' "$transcript" 2>/dev/null || echo ""
}
