# Terminal UI for setup.sh: banner, numbered steps, spinners, a summary.
# Sourced, not run. Works in macOS's built-in bash 3.2 (no mapfile, no
# associative arrays, no ${x,,}). Colour and the spinner only when stdout is a
# terminal and NO_COLOR is unset (https://no-color.org), so logs and CI stay clean.

UI_LOG="${UI_LOG:-/dev/null}"
UI_STEP=0
UI_STEPS="${UI_STEPS:-0}"
UI_T0="$(date +%s)"
UI_WARNINGS=""
UI_PID=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
  UI_TTY=1
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_ACCENT=$'\033[38;5;75m'; C_OK=$'\033[38;5;78m'; C_WARN=$'\033[38;5;214m'; C_ERR=$'\033[38;5;203m'
else
  UI_TTY=0; C_RESET=""; C_BOLD=""; C_DIM=""; C_ACCENT=""; C_OK=""; C_WARN=""; C_ERR=""
fi
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
  *UTF-8*|*utf-8*|*UTF8*|*utf8*) M_OK="✓"; M_ERR="✗"; M_WARN="!"; M_DOT="·"; UI_SPIN="⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏" ;;
  *) M_OK="ok"; M_ERR="x"; M_WARN="!"; M_DOT="-"; UI_SPIN="| / - \\" ;;
esac

ui_elapsed() {  # seconds -> "4s" / "2m 05s"
  local s="$1"
  if [ "$s" -lt 60 ]; then printf '%ss' "$s"; else printf '%sm %02ds' "$((s / 60))" "$((s % 60))"; fi
}

ui_banner() {  # ui_banner "Name" "subtitle"
  printf '\n'
  printf '  %s%s%s\n' "$C_BOLD$C_ACCENT" "$1" "$C_RESET"
  printf '  %s%s%s\n' "$C_DIM" "$2" "$C_RESET"
  printf '  %s────────────────────────────────────────────%s\n' "$C_DIM" "$C_RESET"
}

ui_step() {  # ui_step "Title"
  UI_STEP=$((UI_STEP + 1))
  printf '\n  %s%s/%s%s  %s%s%s\n' "$C_DIM" "$UI_STEP" "$UI_STEPS" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"
  printf '\n== step %s: %s (%s)\n' "$UI_STEP" "$1" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$UI_LOG"
}

ui_ok()   { printf '     %s%s%s %s\n' "$C_OK" "$M_OK" "$C_RESET" "$1"; }
ui_info() { printf '     %s%s %s%s\n' "$C_DIM" "$M_DOT" "$1" "$C_RESET"; }
ui_warn() {
  printf '     %s%s%s %s\n' "$C_WARN" "$M_WARN" "$C_RESET" "$1"
  UI_WARNINGS="${UI_WARNINGS}$1
"
}
ui_err()  { printf '     %s%s%s %s\n' "$C_ERR" "$M_ERR" "$C_RESET" "$1"; }

# ui_run "label" command args...   Runs the command with its output in the log,
# shows a spinner and the time taken, and on failure prints the last lines of
# the log. Returns the command's exit code.
ui_run() {
  local label="$1"; shift
  local t0 rc=0 pid i=0 frames
  t0="$(date +%s)"
  printf '\n$ %s\n' "$*" >> "$UI_LOG"
  if [ "$UI_TTY" = 1 ]; then
    ( "$@" ) >> "$UI_LOG" 2>&1 </dev/null &
    pid=$!; UI_PID=$pid   # the caller's Ctrl+C handler stops this step through UI_PID
    set -- $UI_SPIN; frames="$#"
    while kill -0 "$pid" 2>/dev/null; do
      i=$((i % frames + 1))
      eval "f=\${$i}"
      printf '\r     %s%s%s %s %s%s%s ' "$C_ACCENT" "$f" "$C_RESET" "$label" "$C_DIM" "$(ui_elapsed $(( $(date +%s) - t0 )))" "$C_RESET"
      sleep 0.1
    done
    wait "$pid" || rc=$?
    UI_PID=""
    printf '\r\033[K'
  else
    printf '     %s ...\n' "$label"
    ( "$@" ) >> "$UI_LOG" 2>&1 </dev/null || rc=$?
  fi
  local took; took="$(ui_elapsed $(( $(date +%s) - t0 )))"
  if [ "$rc" -eq 0 ]; then
    printf '     %s%s%s %s %s%s%s\n' "$C_OK" "$M_OK" "$C_RESET" "$label" "$C_DIM" "$took" "$C_RESET"
  else
    printf '     %s%s%s %s %s(failed after %s)%s\n' "$C_ERR" "$M_ERR" "$C_RESET" "$label" "$C_DIM" "$took" "$C_RESET"
    if [ "$UI_LOG" != /dev/null ]; then
      printf '%s' "$C_DIM"; tail -n 12 "$UI_LOG" | sed 's/^/       | /'; printf '%s' "$C_RESET"
      printf '       Full log: %s\n' "$UI_LOG"
    fi
  fi
  return "$rc"
}

ui_ask() {  # ui_ask VAR "question" [default]   (reads from the terminal)
  local __v __d="${3:-}"
  if [ -n "$__d" ]; then printf '\n     %s %s[%s]%s ' "$2" "$C_DIM" "$__d" "$C_RESET"; else printf '\n     %s ' "$2"; fi
  IFS= read -r __v || return 1
  [ -z "$__v" ] && __v="$__d"
  eval "$1=\$__v"
}

ui_secret() {  # ui_secret VAR "question"   (no echo; for tokens)
  local __v
  printf '\n     %s ' "$2"
  if [ -t 0 ]; then stty -echo 2>/dev/null; IFS= read -r __v; local r=$?; stty echo 2>/dev/null; printf '%s\n' "$C_DIM(hidden)$C_RESET"; [ $r -eq 0 ] || return 1
  else IFS= read -r __v || return 1; fi
  eval "$1=\$__v"
}

ui_yes() {  # ui_yes "question" y|n   -> 0 for yes
  local a d="${2:-n}" hint="[y/N]"
  [ "$d" = y ] && hint="[Y/n]"
  printf '\n     %s %s%s%s ' "$1" "$C_DIM" "$hint" "$C_RESET"
  IFS= read -r a || a=""
  [ -z "$a" ] && a="$d"
  case "$a" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

ui_done() {  # ui_done "headline"
  printf '\n  %s────────────────────────────────────────────%s\n' "$C_DIM" "$C_RESET"
  printf '  %s%s %s%s  %s%s%s\n' "$C_OK$C_BOLD" "$M_OK" "$1" "$C_RESET" "$C_DIM" "$(ui_elapsed $(( $(date +%s) - UI_T0 )))" "$C_RESET"
  if [ -n "$UI_WARNINGS" ]; then
    printf '\n  %sWorth a look:%s\n' "$C_WARN" "$C_RESET"
    printf '%s' "$UI_WARNINGS" | sed '/^$/d; s/^/     - /'
  fi
}
