#!/usr/bin/env bash
# The blimp command. setup.sh installs it as ~/.local/bin/blimp (with the folder filled in).
REPO="@REPO@"
H="${BLIMP_HOME:-$HOME/blimp}"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { eval "$("$b" shellenv)"; break; }; done
py() { if [ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "$HOMEBREW_PREFIX/bin/python3.13" ]; then echo "$HOMEBREW_PREFIX/bin/python3.13"; else command -v python3; fi; }  # prints the path (exec cannot run a function)
[ -d "$REPO" ] || { echo "Blimp's folder is gone ($REPO). Download it again and run: bash setup.sh"; exit 1; }
case "${1:-help}" in
  start)  exec bash "$REPO/setup.sh" --start ;;
  stop)   tmux kill-session -t duke 2>/dev/null && echo "Stopped. (Memory keeps running; it is light.)" || echo "It was not running." ;;
  restart) tmux kill-session -t duke 2>/dev/null; exec bash "$REPO/setup.sh" --start ;;
  status) exec "$(py)" "$REPO/blimp-infra/doctor/doctor.py" --lint ;;
  doctor) shift; exec "$(py)" "$REPO/blimp-infra/doctor/doctor.py" "$@" ;;
  logs)
    tmux has-session -t duke 2>/dev/null || { echo "The agent is not running. Start it: blimp start"; exit 1; }
    echo "Watching the agent. Leave with Ctrl+B, then D (it keeps running)."; sleep 1
    exec tmux attach -t duke ;;
  memory-logs) exec tail -n 80 -f "$H/logs/hindsight.log" ;;
  update)
    if [ -d "$REPO/.git" ]; then git -C "$REPO" pull --ff-only || { echo "Could not update (local changes?). Nothing was changed."; exit 1; }
    else echo "This copy was downloaded as a ZIP, so it cannot update itself. Download the new ZIP, then run bash setup.sh in it."; exit 1; fi
    exec bash "$REPO/setup.sh" ;;
  help|-h|--help)
    cat <<H
blimp start     start the agent (and its memory)
blimp stop      stop the agent
blimp restart   stop, then start
blimp status    check every part, change nothing
blimp doctor    find problems and fix them with you  (blimp doctor --chat: talk it through)
blimp logs      watch the agent work (leave with Ctrl+B, then D)
blimp update    get the newest version and set it up
H
    ;;
  *) echo "Unknown: $1. Try: blimp help"; exit 2 ;;
esac
