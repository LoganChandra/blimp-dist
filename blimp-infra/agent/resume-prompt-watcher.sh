#!/usr/bin/env bash
# resume-prompt-watcher.sh: keep unattended agents from freezing on Claude Code's idle-resume question.
#
# Since Claude Code 2.1.304 (2026-10-19), the first message to a long, idle conversation opens
# "Resume this conversation? ... will use about N% of your 5-hour usage limit" and waits for a
# keypress. An agent served over Discord has nobody at the keyboard, so it stops answering until
# someone presses Enter. That happened to Duke on 2026-11-06 (536k tokens, idle 3.5 h).
#
# Every 20 s this checks the panes of the agent tmux sessions (duke, nanook) and, only when that exact
# question is on screen with "Resume" selected, presses Enter once. No model calls; zero idle spend.
# It never types anything else. Log: ~/.cache/blimp-agent/resume-watcher.log
#   run once:  resume-prompt-watcher.sh --once      install: systemctl --user enable --now blimp-resume-watcher
set -uo pipefail
SESSIONS="${BLIMP_WATCH_SESSIONS:-duke nanook}"
LOG="$HOME/.cache/blimp-agent/resume-watcher.log"; mkdir -p "$(dirname "$LOG")"
check() {
  local s pane
  for s in $SESSIONS; do
    tmux has-session -t "$s" 2>/dev/null || continue
    pane="$(tmux capture-pane -t "$s" -p 2>/dev/null | awk NF | tail -12)"  # last 12 non-blank lines
    # Plain bash matching (no grep): the question must be on screen AND the cursor on "Resume".
    if [[ "$pane" == *"Resume this conversation?"* ]] && [[ "$pane" =~ ❯\ (1\.\ )?Resume[[:space:]]*($'\n'|$) ]]; then
      tmux send-keys -t "$s" Enter
      local why="${pane#*inactive for }"; why="${why%%.*}"
      printf '%s %s: answered the idle-resume question (inactive for %s)\n' "$(date '+%F %T')" "$s" "$why" >> "$LOG"
    fi
  done
}
[ "${1:-}" = "--once" ] && { check; exit 0; }
while true; do check; sleep 20; done
