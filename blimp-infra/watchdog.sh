#!/usr/bin/env bash
# watchdog.sh — keep DUKE alive. (Hermes is now systemd-managed; do NOT watch
# his tmux because he doesn't have one — see audit-finding 2026-05-27.)
#
# DANGER if this watches Hermes: it spawns a new tmux Hermes every 30s thinking
# the systemd one is "down", that tmux Hermes conflicts on the Discord token,
# both crash, Hermes posts "Gateway shutting down" to Discord, Duke processes
# that message via the channels plugin, BURNS CLAUDE TOKENS on a loop.
# Cost us a real Claude usage spike — kept this comment so the bug doesn't
# get re-introduced.
#
# Hermes auto-restart is now handled by systemd (`hermes-gateway.service`).
# This watchdog ONLY supervises Duke's tmux session.
#
# Usage:
#   bash watchdog.sh start      # spawn watchdog in background (default)
#   bash watchdog.sh stop       # stop the watchdog (but leave Duke alive)
#   bash watchdog.sh status     # check whether watchdog is running
#   bash watchdog.sh once       # liveness + runaway check once, exit (cron use)
#   bash watchdog.sh runaway    # repeat-call/token-cap check once, exit (no restart)
#   bash watchdog.sh telemetry  # append a per-session token/cost snapshot
#   bash watchdog.sh cron-audit # flag scheduled jobs with a stale/missing pulse
#
# P1.7 hardening (sources watchdog/lib.sh; all parsing off Claude Code's native
# JSONL transcript, never tmux scrollback):
#   - repeat-call kill: >= DUKE_REPEAT_LIMIT (5) identical consecutive tool calls
#     in the live transcript -> kill Duke's tmux session only + incident note.
#   - hard token cap: DUKE_TOKEN_CAP (default 2,000,000) per session, warn at 80%.
#   - cron-audit: pulse:* stamps older than 26h -> incident (see cron-audit.sh).
#   - telemetry: per-session token/cost JSONL into ~/blimp/telemetry/ (telemetry.sh).
#   Kills are ALWAYS scope-narrowed to Duke's session, never shared units, and
#   are suppressed (incident-only) when a peer claude shares the cwd.

set -uo pipefail

PIDFILE="$HOME/blimp/watchdog.pid"
LOGFILE="$HOME/blimp/watchdog.log"
INTERVAL=30  # seconds between checks

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"  # repo root, wherever it is checked out
INFRA_DIR="$REPO_ROOT/blimp-infra"
WATCHDOG_DIR="$INFRA_DIR/watchdog"

# P1.7 hardening helpers (JSONL-transcript analytics, incident logging, pulse
# stamps, Duke-session/transcript resolution). Sourcing has no side effects.
# shellcheck source=watchdog/lib.sh
if [ -f "$WATCHDOG_DIR/lib.sh" ]; then
  source "$WATCHDOG_DIR/lib.sh"
fi

# ── P1.7 tunables (env-overridable) ───────────────────────────────────────
# Hard per-session token cap; warn at 80%. Default 2,000,000 (plan §P1.7b).
DUKE_TOKEN_CAP="${DUKE_TOKEN_CAP:-2000000}"
# Identical-consecutive tool calls that count as a runaway loop (plan: >=5).
DUKE_REPEAT_LIMIT="${DUKE_REPEAT_LIMIT:-5}"
# How often (in loop iterations) to run the per-session telemetry snapshot.
# At INTERVAL=30s, 40 iterations ≈ every 20 min — cheap, jq-only.
DUKE_TELEMETRY_EVERY="${DUKE_TELEMETRY_EVERY:-40}"
# Set to 1 to log-only and never actually kill (for dry-running on prod).
DUKE_WATCHDOG_DRY="${DUKE_WATCHDOG_DRY:-0}"

mkdir -p "$(dirname "$PIDFILE")"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOGFILE"; }

start_duke() {
  if ! tmux has-session -t duke 2>/dev/null; then
    log "Duke is down — starting"
    bash "$INFRA_DIR/boot-text-blimp.sh" >> "$LOGFILE" 2>&1
  fi
}

# NOTE: do NOT add start_hermes() here. Hermes lives in systemd
# (~/.config/systemd/user/hermes-gateway.service). Polling for his tmux
# session is harmful — see header for the cost incident.

# kill_duke_session — restart ONLY Duke's tmux session, never a shared unit.
# This is the single mutating action the watchdog takes on a runaway. It kills
# the tmux session (which terminates the claude CLI inside it) and relies on
# start_duke() on the next iteration to bring a fresh session back. The Hermes
# systemd unit and every other tmux session are untouched — the never-broad-
# pkill / cost-incident discipline in this file's header, in scope-narrowed form.
kill_duke_session() {
  local reason="$1"
  if [ "$DUKE_WATCHDOG_DRY" = "1" ]; then
    log "DRY-RUN: would kill Duke session ($reason)"
    return 0
  fi
  log "KILLING Duke tmux session: $reason"
  tmux kill-session -t duke 2>/dev/null || true
  # Boot normally resumes Duke's last session. After a runaway kill that would resume the very
  # transcript that tripped the cap and loop kill/resume forever: agent/launch.sh sees this marker
  # and starts the next session blank (works for loop, once and cron modes alike).
  mkdir -p "$HOME/.cache/blimp-agent" && touch "$HOME/.cache/blimp-agent/fresh-duke"
}

# check_runaway — repeat-call kill + hard token cap, both off the live JSONL
# transcript (ground truth, not scrollback). No-op when lib.sh / jq absent or
# Duke isn't up. Refuses to kill when the transcript is ambiguous (a peer
# claude process shares the cwd) — incident only, per wd_claude_peers_in.
check_runaway() {
  command -v jq >/dev/null 2>&1 || return 0
  declare -F wd_resolve_duke_transcript >/dev/null 2>&1 || return 0

  local pid transcript dir peers max off total warn
  pid="$(wd_find_duke_claude_pid)"
  [ -n "$pid" ] || return 0
  transcript="$(wd_resolve_duke_transcript "$pid")"
  [ -n "$transcript" ] || return 0

  dir="$(wd_project_dir_for_pid "$pid")"
  peers="$(wd_claude_peers_in "$dir" "$pid")"

  # (a) repeat-call kill — >=5 identical consecutive tool calls.
  max="$(wd_max_consecutive_tool_calls "$transcript")"
  if [ "${max:-0}" -ge "$DUKE_REPEAT_LIMIT" ]; then
    off="$(wd_repeat_offender "$transcript")"
    if [ "${peers:-0}" -gt 0 ]; then
      log "REPEAT-LOOP detected ($max x '$off') but $peers peer claude session(s) share the cwd — incident only, NOT killing"
      wd_incident "repeat-loop-ambiguous" \
        "${max}x consecutive '${off}' in $(basename "$transcript"); $peers peer(s) share cwd — no kill"
    else
      log "REPEAT-LOOP: ${max}x consecutive '$off' >= ${DUKE_REPEAT_LIMIT} — killing Duke"
      wd_incident "repeat-loop" \
        "${max}x consecutive '${off}' tool calls (>= ${DUKE_REPEAT_LIMIT}) in $(basename "$transcript")"
      kill_duke_session "repeat-loop ${max}x '$off'"
      return 0  # don't also token-check a session we just killed
    fi
  fi

  # (b) hard per-session token cap — warn at 80%, kill at 100%.
  total="$(wd_token_total "$transcript")"
  warn=$(( DUKE_TOKEN_CAP * 80 / 100 ))
  if [ "${total:-0}" -ge "$DUKE_TOKEN_CAP" ]; then
    if [ "${peers:-0}" -gt 0 ]; then
      log "TOKEN-CAP hit ($total >= $DUKE_TOKEN_CAP) but $peers peer session(s) share cwd — incident only, NOT killing"
      wd_incident "token-cap-ambiguous" \
        "session tokens $total >= cap $DUKE_TOKEN_CAP; $peers peer(s) share cwd — no kill"
    else
      log "TOKEN-CAP: session tokens $total >= cap $DUKE_TOKEN_CAP — killing Duke"
      wd_incident "token-cap" \
        "session $(basename "$transcript") burned $total tokens (>= cap $DUKE_TOKEN_CAP)"
      kill_duke_session "token-cap $total >= $DUKE_TOKEN_CAP"
    fi
  elif [ "${total:-0}" -ge "$warn" ]; then
    log "TOKEN-WARN: session tokens $total >= 80% of cap ($warn / $DUKE_TOKEN_CAP)"
  fi
}

# snapshot_telemetry — append a per-session token/cost record (jq-only, cheap).
snapshot_telemetry() {
  [ -x "$WATCHDOG_DIR/telemetry.sh" ] || [ -f "$WATCHDOG_DIR/telemetry.sh" ] || return 0
  bash "$WATCHDOG_DIR/telemetry.sh" >> "$LOGFILE" 2>&1 || true
}

check_once() {
  start_duke
  check_runaway
}

case "${1:-start}" in

  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "watchdog running (PID $(cat "$PIDFILE"))"
      exit 0
    else
      echo "watchdog NOT running"
      exit 1
    fi
    ;;

  stop)
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "watchdog stopped"
      rm -f "$PIDFILE"
    else
      echo "watchdog was not running"
      rm -f "$PIDFILE"
    fi
    ;;

  once)
    check_once
    ;;

  runaway)
    # Runaway check only (no liveness restart). Useful from cron at a tighter
    # cadence than the liveness loop, or for manual inspection.
    check_runaway
    ;;

  telemetry)
    snapshot_telemetry
    ;;

  cron-audit)
    bash "$WATCHDOG_DIR/cron-audit.sh" "${2:-}"
    ;;

  start)
    # If a watchdog is already running, do nothing
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "watchdog already running (PID $(cat "$PIDFILE"))"
      exit 0
    fi

    # Spawn background loop, detached from this shell
    (
      echo $$ > "$PIDFILE"
      log "watchdog started (PID $$, interval ${INTERVAL}s, token-cap ${DUKE_TOKEN_CAP}, repeat-limit ${DUKE_REPEAT_LIMIT})"
      trap 'log "watchdog stopping"; rm -f "$PIDFILE"; exit 0' SIGTERM SIGINT
      iter=0
      while true; do
        check_once
        # Periodic per-session telemetry snapshot (every DUKE_TELEMETRY_EVERY
        # iterations) so the digest has measured token/cost actuals.
        if [ "$DUKE_TELEMETRY_EVERY" -gt 0 ] && [ $(( iter % DUKE_TELEMETRY_EVERY )) -eq 0 ]; then
          snapshot_telemetry
        fi
        iter=$(( iter + 1 ))
        sleep "$INTERVAL"
      done
    ) </dev/null >> "$LOGFILE" 2>&1 &
    disown
    sleep 1
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "watchdog started (PID $(cat "$PIDFILE")). log: $LOGFILE"
    else
      echo "watchdog failed to start — see $LOGFILE"
      exit 1
    fi
    ;;

  *)
    echo "Usage: $0 {start|stop|status|once|runaway|telemetry|cron-audit}" >&2
    exit 1
    ;;
esac
