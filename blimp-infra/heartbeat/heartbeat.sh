#!/usr/bin/env bash
# heartbeat.sh — Duke's daytime pulse. Pure bash, NO LLM calls, zero tokens.
#
# Runs every 20 min (08:00–23:00 Asia/Kuala_Lumpur) via duke-heartbeat.timer.
# Parses the watch-item registry in HEARTBEAT.md (same dir), runs every
# non-expired check, and:
#   - nothing wrong -> stamps pulse:heartbeat in project_state and exits
#                      SILENTLY (no Discord, no tokens, no log noise)
#   - a check FIRES -> ONE alert line to #duke via the `hermes send` CLI
#                      (the same no-LLM send path blimp-brain's send_discord
#                      tool shells out to — see blimp-infra/mcp/) and, unless
#                      HEARTBEAT_WAKE=0, one tmux send-keys nudge to the
#                      'duke' session (session name per boot-text-blimp.sh)
#                      so Duke investigates.
#
# Check contract (builtins and registry check_cmds alike — see HEARTBEAT.md):
#   exit 0 = OK    (silent)
#   exit 1 = FIRE  (last stdout line is the one-line alert reason)
#   exit 2 = SKIP  (no data / dependency not deployed yet — degrade gracefully)
#
# Usage:
#   bash heartbeat.sh              # normal run (the timer calls this)
#   bash heartbeat.sh --dry-run    # run checks read-only, print what WOULD
#                                  # fire; no Discord, no tmux, no pulse stamp;
#                                  # always exits 0 (safe to run repeatedly)
#   bash heartbeat.sh --validate   # parse the HEARTBEAT.md registry, report
#                                  # each row, exit non-zero on malformed rows;
#                                  # runs nothing, sends nothing
#   bash heartbeat.sh --list       # print the parsed registry rows
#   DRY_RUN=1 bash heartbeat.sh    # same as --dry-run
#
# Env overrides:
#   HEARTBEAT_REGISTRY  path to registry (default: HEARTBEAT.md next to this script)
#   HEARTBEAT_WAKE      0 = never tmux-nudge Duke (default 1)
#   HERMES_BIN          hermes CLI (default ~/.local/bin/hermes)
#   DUKE_CHANNEL_ID     Discord channel for alerts (default #duke)
#   DISK_MAX_USE_PCT    disk fire threshold (default 90)
#   TELEMETRY_FACTOR    token-spend anomaly multiplier (default 3)

set -euo pipefail

# Timer-spawned shells are bare — make sure the CLIs we shell out to resolve.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY="${HEARTBEAT_REGISTRY:-$HERE/HEARTBEAT.md}"
STATE_DIR="$HOME/blimp/heartbeat"
DIGEST_QUEUE="$STATE_DIR/digest-queue.txt"
LOCKFILE="$STATE_DIR/heartbeat.lock"

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
DUKE_CHANNEL_ID="${DUKE_CHANNEL_ID:-<ID>}"   # #duke
TMUX_SESSION="duke"                                          # per boot-text-blimp.sh
WAKE="${HEARTBEAT_WAKE:-1}"
DRY_RUN="${DRY_RUN:-0}"

PSQL=(psql --host=/var/run/postgresql --port=5433 duke_data)

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ------------------------------------------------------------------
# Registry parsing — strict format, see HEARTBEAT.md "Registry format".
# Rows land in parallel arrays; PARSE_ERRORS collects violations.
# ------------------------------------------------------------------
R_ITEM=() ; R_CMD=() ; R_THRESHOLD=() ; R_HELPED=() ; R_HARMED=() ; R_EXPIRES=()
PARSE_ERRORS=()

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

parse_registry() {
  if [ ! -f "$REGISTRY" ]; then
    PARSE_ERRORS+=("registry file missing: $REGISTRY")
    return 0
  fi
  local in_table=0 rownum=0 line
  while IFS= read -r line; do
    # Strip a trailing CR — HEARTBEAT.md lives on /mnt/c and carries CRLF.
    line="${line%$'\r'}"
    # Match the markers ONLY on a line that is nothing but the marker.
    # 2026-09-02: the prose near the top of HEARTBEAT.md *mentions* the literal
    # marker, so a substring match opened the table ~45 lines early, ate the
    # illustrative example row as the header, and reported the real separator
    # row as 3 malformed columns on every single run.
    case "${line#"${line%%[![:space:]]*}"}" in
      '<!-- registry:start -->') in_table=1; continue ;;
      '<!-- registry:end -->')   in_table=0; continue ;;
    esac
    [ "$in_table" = 1 ] || continue
    [[ "$line" == \|* ]] || continue
    rownum=$((rownum + 1))
    if [ "$rownum" -le 2 ]; then continue; fi   # header + separator rows
    # split on | ; field 0 is the empty string before the leading pipe
    local IFS='|'
    read -r -a f <<< "$line"
    if [ "${#f[@]}" -lt 7 ]; then
      PARSE_ERRORS+=("row $rownum: expected 6 columns, got $((${#f[@]} - 1)): $line")
      continue
    fi
    local item cmd threshold helped harmed expires
    item="$(trim "${f[1]}")"
    cmd="$(trim "${f[2]}")"
    threshold="$(trim "${f[3]}")"
    helped="$(trim "${f[4]}")"
    harmed="$(trim "${f[5]}")"
    expires="$(trim "${f[6]}")"
    local bad=0
    [ -n "$item" ]                                   || { PARSE_ERRORS+=("row $rownum: empty item");        bad=1; }
    [ -n "$cmd" ]                                    || { PARSE_ERRORS+=("row $rownum: empty check_cmd");   bad=1; }
    [[ "$helped"  =~ ^[0-9]+$ ]]                     || { PARSE_ERRORS+=("row $rownum ($item): helped_count not an integer: '$helped'"); bad=1; }
    [[ "$harmed"  =~ ^[0-9]+$ ]]                     || { PARSE_ERRORS+=("row $rownum ($item): harmed_count not an integer: '$harmed'"); bad=1; }
    [[ "$expires" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}|never)$ ]] \
                                                     || { PARSE_ERRORS+=("row $rownum ($item): expires must be YYYY-MM-DD or 'never': '$expires'"); bad=1; }
    if [[ "$cmd" == builtin:* ]]; then
      local fn="check_${cmd#builtin:}"; fn="${fn//-/_}"
      declare -F "$fn" >/dev/null                    || { PARSE_ERRORS+=("row $rownum ($item): unknown builtin '$cmd'"); bad=1; }
    fi
    local i
    for i in "${!R_ITEM[@]}"; do
      [ "${R_ITEM[$i]}" = "$item" ] && { PARSE_ERRORS+=("row $rownum: duplicate item '$item'"); bad=1; }
    done
    if [ "$bad" = 1 ]; then continue; fi
    R_ITEM+=("$item"); R_CMD+=("$cmd"); R_THRESHOLD+=("$threshold")
    R_HELPED+=("$helped"); R_HARMED+=("$harmed"); R_EXPIRES+=("$expires")
  done < "$REGISTRY"
  if [ "${#R_ITEM[@]}" -eq 0 ] && [ "${#PARSE_ERRORS[@]}" -eq 0 ]; then
    PARSE_ERRORS+=("no registry rows found between <!-- registry:start --> and <!-- registry:end -->")
  fi
}

# ------------------------------------------------------------------
# Builtin checks. Each: exit 0 OK / 1 FIRE (echo reason) / 2 SKIP.
# All read-only; each bounded to a few seconds.
# ------------------------------------------------------------------

check_hindsight_health() {
  curl -fsS -m 5 -o /dev/null http://127.0.0.1:8888/health 2>/dev/null \
    || { echo "hindsight not answering on 127.0.0.1:8888/health"; return 1; }
}

check_hermes_webhook_port() {
  timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8644 && exec 3>&-' 2>/dev/null \
    || { echo "hermes webhook port 8644 not accepting connections"; return 1; }
}

check_backup_freshness() {
  # backup.sh writes duke_data-YYYY-MM-DD.sql.gz + hindsight-YYYY-MM-DD.tar.gz
  local dir="$HOME/blimp/backups"
  [ -d "$dir" ] || { echo "backup dir missing ($dir) — backups have never run"; return 1; }
  if [ -z "$(find "$dir" -maxdepth 1 -name 'duke_data-*.sql.gz' -mmin -1560 -print -quit 2>/dev/null)" ]; then
    echo "no duke_data backup in the last 26h"; return 1
  fi
  if [ -z "$(find "$dir" -maxdepth 1 -name 'hindsight-*.tar.gz' -mmin -1560 -print -quit 2>/dev/null)" ]; then
    echo "no hindsight backup in the last 26h"; return 1
  fi
  return 0
}

check_disk_space() {
  local max="${DISK_MAX_USE_PCT:-90}" fired="" mnt use
  for mnt in "$HOME" /mnt/c; do
    use="$(df -P "$mnt" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
    [ -n "$use" ] || continue
    if [ "$use" -ge "$max" ] 2>/dev/null; then
      fired="${fired}${mnt} at ${use}%; "
    fi
  done
  if [ -n "$fired" ]; then echo "disk pressure: ${fired%; }"; return 1; fi
  return 0
}

check_telemetry_anomaly() {
  # Contract (see HEARTBEAT.md "Telemetry contract"): the telemetry job
  # (P1.7) and overnight.sh keep one plain-integer file per day at
  # ~/blimp/telemetry/daily/YYYY-MM-DD.tokens. No data yet -> SKIP.
  local dir="$HOME/blimp/telemetry/daily" factor="${TELEMETRY_FACTOR:-3}"
  local today total sum=0 n=0 i f v mean
  [ -d "$dir" ] || { echo "no telemetry dir yet ($dir)"; return 2; }
  today="$dir/$(date +%F).tokens"
  [ -s "$today" ] || { echo "no telemetry for today yet"; return 2; }
  total="$(tr -dc '0-9' < "$today")"
  [ -n "$total" ] || { echo "today's telemetry file is not an integer"; return 2; }
  for i in 1 2 3 4 5 6 7; do
    f="$dir/$(date -d "-$i day" +%F).tokens"
    [ -s "$f" ] || continue
    v="$(tr -dc '0-9' < "$f")"
    [ -n "$v" ] || continue
    sum=$((sum + v)); n=$((n + 1))
  done
  [ "$n" -ge 3 ] || { echo "fewer than 3 days of telemetry history"; return 2; }
  mean=$((sum / n))
  if [ "$mean" -lt 1000 ]; then mean=1000; fi   # floor so near-zero weeks don't false-fire
  if [ "$total" -gt $((mean * factor)) ]; then
    echo "token spend today ${total} vs ${n}-day mean ${mean} (>${factor}x)"
    return 1
  fi
  return 0
}

check_pulse_stamps() {
  # Every scheduled job stamps project_state 'pulse:<job>' (P0.0(f)). A stamp
  # older than 26h means that job silently died. updated_at is the stamp time.
  local out
  out="$("${PSQL[@]}" -tAc \
    "SELECT key FROM project_state WHERE key LIKE 'pulse:%' AND updated_at < NOW() - INTERVAL '26 hours'" \
    2>&1)" || { echo "postgres duke_data unreachable on port 5433"; return 1; }
  if [ -n "$out" ]; then
    echo "stale pulse stamp(s): $(printf '%s' "$out" | tr '\n' ' ')"
    return 1
  fi
  return 0
}

# ------------------------------------------------------------------
# Side effects (all suppressed under --dry-run)
# ------------------------------------------------------------------

stamp_pulse() {
  # FIXED 2026-07-08 (see docs/foundry-registry/lessons.md L-020): this was
  # NOT actually working despite the "known-working" label copied elsewhere —
  # psql's `:'var'` interpolation silently fails via `-c "..."` (confirmed
  # live, psql 16.14); undetected because heartbeat.timer has been disabled
  # since 2026-06-13. Fix: pipe the SQL via stdin, keep the -v binding.
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  "${PSQL[@]}" -qv ts="$(date -Is)" <<SQL >/dev/null 2>&1 || log "WARN could not stamp pulse:heartbeat (postgres down?)"
INSERT INTO project_state (key, value) VALUES ('pulse:heartbeat', jsonb_build_object('ts', :'ts'::text))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
SQL
}

send_discord() {
  local msg="$1"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY_RUN would send to #duke: $msg"
    return 0
  fi
  if ! "$HERMES_BIN" send --to "discord:$DUKE_CHANNEL_ID" --quiet "$msg" 2>/dev/null; then
    log "WARN discord send failed: $msg"
  fi
}

wake_duke() {
  local instr="$1"
  [ "$WAKE" = 1 ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY_RUN would tmux-nudge '$TMUX_SESSION': $instr"
    return 0
  fi
  if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    log "WARN tmux session '$TMUX_SESSION' not running — skipping wake"
    return 0
  fi
  tmux send-keys -t "$TMUX_SESSION" -l -- "$instr" 2>/dev/null || return 0
  tmux send-keys -t "$TMUX_SESSION" Enter 2>/dev/null || true
}

queue_digest_line() {
  # Dedup'd one-liners the morning-digest job drains (it owns clearing the file).
  local line="$1"
  mkdir -p "$STATE_DIR"
  grep -qxF -- "$line" "$DIGEST_QUEUE" 2>/dev/null || printf '%s\n' "$line" >> "$DIGEST_QUEUE"
}

# ------------------------------------------------------------------
# Modes
# ------------------------------------------------------------------

run_one() {
  # runs check $1 (cmd), captures output -> CHECK_OUT, returns its exit code
  local cmd="$1" rc=0
  CHECK_OUT=""
  if [[ "$cmd" == builtin:* ]]; then
    local fn="check_${cmd#builtin:}"; fn="${fn//-/_}"
    CHECK_OUT="$("$fn" 2>&1)" || rc=$?
  else
    CHECK_OUT="$(timeout 30 bash -c "$cmd" 2>&1)" || rc=$?
    if [ "$rc" = 124 ]; then CHECK_OUT="check timed out after 30s"; fi
  fi
  # last stdout line is the reason
  if [ -n "$CHECK_OUT" ]; then CHECK_OUT="$(printf '%s' "$CHECK_OUT" | tail -n 1)"; fi
  return "$rc"
}

mode_validate() {
  parse_registry
  echo "registry: $REGISTRY"
  echo "rows parsed: ${#R_ITEM[@]}"
  local i
  for i in "${!R_ITEM[@]}"; do
    printf '  ok  %-22s %-32s helped=%s harmed=%s expires=%s\n' \
      "${R_ITEM[$i]}" "${R_CMD[$i]}" "${R_HELPED[$i]}" "${R_HARMED[$i]}" "${R_EXPIRES[$i]}"
  done
  if [ "${#PARSE_ERRORS[@]}" -gt 0 ]; then
    echo "errors: ${#PARSE_ERRORS[@]}"
    printf '  ERR %s\n' "${PARSE_ERRORS[@]}"
    return 1
  fi
  echo "errors: 0"
  return 0
}

mode_run() {
  mkdir -p "$STATE_DIR"
  # never overlap with a previous run
  exec 200>"$LOCKFILE"
  flock -n 200 || { log "another heartbeat run holds the lock — exiting"; return 0; }

  parse_registry
  if [ "${#PARSE_ERRORS[@]}" -gt 0 ]; then
    # a broken registry is itself an alert condition — but never a crash
    send_discord "[heartbeat] HEARTBEAT.md registry has ${#PARSE_ERRORS[@]} malformed row(s) — run: bash blimp-infra/heartbeat/heartbeat.sh --validate"
  fi

  local today fired=() i rc
  today="$(date +%F)"

  for i in "${!R_ITEM[@]}"; do
    local item="${R_ITEM[$i]}" cmd="${R_CMD[$i]}" expires="${R_EXPIRES[$i]}"
    local helped="${R_HELPED[$i]}" harmed="${R_HARMED[$i]}"

    # expired -> don't run; flag for retirement in the digest
    if [ "$expires" != "never" ] && [[ "$expires" < "$today" ]]; then
      queue_digest_line "heartbeat retire-candidate: $item (expired $expires)"
      continue
    fi
    # net-harmful -> still runs, but flagged for review
    if [ $((helped + harmed)) -ge 5 ] && [ "$harmed" -gt "$helped" ]; then
      queue_digest_line "heartbeat retire-candidate: $item (harmed=$harmed > helped=$helped)"
    fi

    rc=0
    run_one "$cmd" || rc=$?
    case "$rc" in
      0) ;;                                          # OK — silent
      2) if [ "$DRY_RUN" = 1 ]; then log "skip $item: $CHECK_OUT"; fi ;;
      *) fired+=("$item: ${CHECK_OUT:-check failed (rc=$rc)}")
         if [ "$DRY_RUN" = 1 ]; then log "FIRE $item: $CHECK_OUT"; fi ;;
    esac
  done

  if [ "${#fired[@]}" -gt 0 ]; then
    local joined alert
    joined="$(printf '%s; ' "${fired[@]}")"; joined="${joined%; }"
    alert="[heartbeat $(date +%H:%M)] ${#fired[@]} check(s) firing — $joined"
    send_discord "$alert"
    wake_duke "Heartbeat fired: $joined. Investigate (blimp-infra/heartbeat/), fix or escalate in #duke, log the incident to blimp-brain."
    log "$alert"
  fi

  stamp_pulse
  return 0
}

case "${1:-run}" in
  --validate) mode_validate ;;
  --list)     parse_registry
              for i in "${!R_ITEM[@]}"; do
                printf '%-22s %-32s expires=%s\n' "${R_ITEM[$i]}" "${R_CMD[$i]}" "${R_EXPIRES[$i]}"
              done
              [ "${#PARSE_ERRORS[@]}" -eq 0 ] || { printf 'ERR %s\n' "${PARSE_ERRORS[@]}"; exit 1; } ;;
  --dry-run)  DRY_RUN=1; mode_run ;;
  run)        mode_run ;;
  *)          echo "Usage: $0 [--validate|--list|--dry-run]" >&2; exit 1 ;;
esac
