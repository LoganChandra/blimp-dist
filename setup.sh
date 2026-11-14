#!/usr/bin/env bash
# Blimp setup. One command installs everything; running it again is always safe.
#
#   bash setup.sh            install (first time) or repair, then start
#   bash setup.sh --start    start only: memory, then the agent (used after a restart)
#   bash setup.sh --help
#
# Works on macOS 13+ (Apple silicon or Intel), Ubuntu 22.04+, and Windows through
# WSL2 (double-click START-BLIMP.bat). Written for macOS's own bash 3.2.
# Everything it installs or saves lives in ~/blimp, plus the tools themselves.
# Unattended runs (CI): BLIMP_CI=1 with the answers in the environment
# (BLIMP_AI, BLIMP_NAME, BLIMP_MEMORY, OPENROUTER_API_KEY, DISCORD_BOT_TOKEN,
# DISCORD_OWNER_USER_ID). CI skips the logins and does not start the agent.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
BLIMP_HOME="${BLIMP_HOME:-$HOME/blimp}"; export BLIMP_HOME
ENV_FILE="$BLIMP_HOME/.env"; CONF="$BLIMP_HOME/config.env"; STATE="$BLIMP_HOME/discord-state"
MODE=install
case "${1:-}" in
  --start) MODE=start ;;
  -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
esac
CI=0; { [ "${BLIMP_CI:-}" = 1 ] || [ "${BLIMP_NONINTERACTIVE:-}" = 1 ]; } && CI=1

mkdir -p "$BLIMP_HOME/logs" "$STATE/approved" "$STATE/inbox"
touch "$CONF" "$ENV_FILE"; chmod 600 "$CONF" "$ENV_FILE"
UI_LOG="$BLIMP_HOME/logs/setup-$(date +%Y%m%d-%H%M%S).log"; : > "$UI_LOG"
UI_STEPS=9; [ "$MODE" = start ] && UI_STEPS=2
. "$REPO/blimp-infra/lib/ui.sh"

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:$PATH"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { eval "$("$b" shellenv)"; break; }; done

cfg_get() { grep "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
cfg_set() { sed -i.bak "/^$1=/d" "$CONF" && rm -f "$CONF.bak"; printf '%s=%s\n' "$1" "$2" >> "$CONF"; }
key_get() { grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
key_set() { sed -i.bak "/^$1=/d" "$ENV_FILE" && rm -f "$ENV_FILE.bak"; printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }
load_keys() { set -a; . "$ENV_FILE"; set +a; }
add_path_line() {  # make a PATH change survive new terminals
  local line="$1" f
  for f in "$HOME/.zprofile" "$HOME/.bashrc"; do
    { [ "$OS" = macos ] || [ "$f" = "$HOME/.bashrc" ]; } || continue
    grep -qF "$line" "$f" 2>/dev/null || printf '\n%s\n' "$line" >> "$f"
  done
}
py3() {  # the best Python we have: Homebrew's 3.13 on a Mac, else python3
  if [ -x "${HOMEBREW_PREFIX:-/nonexistent}/bin/python3.13" ]; then echo "$HOMEBREW_PREFIX/bin/python3.13"; else command -v python3; fi
}

STOPPED_AT=""
stop() {  # stop "what went wrong" ["what to do"]
  ui_err "$1"
  [ -n "${2:-}" ] && printf '       %s\n' "$2"
  STOPPED_AT="$1"; exit 1
}
on_exit() {
  local rc=$?
  stty echo 2>/dev/null || true
  [ -n "${SUDO_KEEPER:-}" ] && kill "$SUDO_KEEPER" 2>/dev/null
  [ $rc -eq 0 ] && return
  printf '\n  %sSetup stopped.%s The full log is %s\n' "$C_WARN$C_BOLD" "$C_RESET" "$UI_LOG"
  [ "$CI" = 1 ] && { tail -n 60 "$UI_LOG"; return; }
  [ -t 0 ] || return
  if ui_yes "Open the doctor? It checks every part of the install and fixes what it can." y; then
    "$(py3)" "$REPO/blimp-infra/doctor/doctor.py" || true
  else
    printf '\n     Any time later: %sblimp doctor%s\n\n' "$C_BOLD" "$C_RESET"
  fi
}
trap on_exit EXIT
kill_tree() { local c; for c in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$c"; done; kill "$1" 2>/dev/null || true; }
# Ctrl+C: a step running in the background ignores it, so stop that step (and what it started) too.
trap '[ -n "${UI_PID:-}" ] && kill_tree "$UI_PID"; exit 130' INT

# Detect the computer instead of asking.
case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux) if grep -qi microsoft /proc/version 2>/dev/null; then OS=windows; else OS=linux; fi ;;
  *) echo "This computer ($(uname -s)) is not supported. Use a Mac, Ubuntu, or Windows with WSL2."; exit 1 ;;
esac

# macOS does not let programs started at login read Downloads, Desktop or Documents,
# so a copy unzipped there is moved to ~/blimp/app and setup carries on from there.
if [ "$OS" = macos ] && [ "$CI" != 1 ]; then
  case "$REPO/" in
    "$HOME/Downloads/"*|"$HOME/Desktop/"*|"$HOME/Documents/"*)
      APP="$BLIMP_HOME/app"
      if [ -f "$APP/setup.sh" ]; then  # an update: keep the old copy (and any edits made in it) first
        BAK="$BLIMP_HOME/app-before-$(date +%Y%m%d-%H%M%S)"; cp -Rp "$APP" "$BAK"
        printf '\n  Your previous copy, with any edits you made, is saved in %s\n' "$BAK"
      fi
      mkdir -p "$APP"
      printf '\n  Blimp will live in %s\n  (macOS does not let programs that start at login read %s).\n' "$APP" "${REPO#$HOME/}"
      rsync -a --update "$REPO/" "$APP/" 2>/dev/null || cp -Rp "$REPO/." "$APP/"
      printf '  From now on, edit the files in %s. You can delete the downloaded copy.\n' "$APP"
      exec bash "$APP/setup.sh" ${1+"$@"} ;;
  esac
fi

sudo_once() {  # ask for the password once, then keep it fresh while setup runs
  [ "$CI" = 1 ] && return 0
  sudo -n true 2>/dev/null || {
    ui_info "Your computer password is needed to install tools (it is not saved anywhere)."
    sudo -v || stop "the password was not accepted." "Run setup again and type your login password."
  }
  [ -n "${SUDO_KEEPER:-}" ] && return 0
  ( while kill -0 $$ 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) &
  SUDO_KEEPER=$!
}

memory_on() { [ "$(cfg_get BLIMP_MEMORY)" != none ]; }

# ---------------------------------------------------------------------------
# start: memory first, then the agent. Also what --start, the login item,
# START-BLIMP.bat and the doctor's fixes run.
# ---------------------------------------------------------------------------
start_memory() {
  memory_on || { ui_info "memory is off"; return 0; }
  if [ "$OS" = macos ]; then
    if [ -n "${HOMEBREW_PREFIX:-}" ] && [ -x "$HOMEBREW_PREFIX/opt/postgresql@17/bin/pg_isready" ]; then
      "$HOMEBREW_PREFIX/opt/postgresql@17/bin/pg_isready" -q -h 127.0.0.1 2>/dev/null \
        || ui_run "Starting the database" brew services start postgresql@17 || true
    fi
  elif command -v pg_isready >/dev/null && ! pg_isready -q 2>/dev/null; then
    ui_info "starting the database (your password may be asked)"
    sudo -n service postgresql start >>"$UI_LOG" 2>&1 || { [ -t 0 ] && sudo service postgresql start >>"$UI_LOG" 2>&1; } || ui_warn "the database did not start: sudo service postgresql start"
  fi
  local port="${HINDSIGHT_API_PORT:-8888}"
  curl -fs "http://127.0.0.1:$port/health" >/dev/null 2>&1 && { ui_ok "long-term memory is running"; return 0; }
  if [ -x "$BLIMP_HOME/run-hindsight.sh" ]; then
    tmux kill-session -t hindsight 2>/dev/null || true
    tmux new-session -d -s hindsight "while true; do bash '$BLIMP_HOME/run-hindsight.sh' 2>&1 | tee -a '$BLIMP_HOME/logs/hindsight.log'; sleep 5; done"
  elif command -v docker >/dev/null && docker inspect duke-hindsight >/dev/null 2>&1; then
    docker start duke-hindsight >/dev/null 2>&1 || sudo docker start duke-hindsight >/dev/null 2>&1 || true
  fi
  wait_health() { local i=0; while [ $i -lt 90 ]; do curl -fs "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0; sleep 2; i=$((i + 1)); done; return 1; }
  ui_run "Waking the long-term memory" wait_health || ui_warn "the long-term memory is slow to start; the agent works without it meanwhile (blimp doctor checks it)"
}

trust_prompt() {  # Claude asks once whether it may work in this folder
  local i=0 pane
  while [ $i -lt 20 ]; do
    pane="$(tmux capture-pane -p -t duke 2>/dev/null || true)"
    case "$pane" in
      *"trust this folder"*|*"trust the files"*|*"I trust"*)
        if [ -t 0 ] && ui_yes "Claude asks once: may it work in $REPO (the folder you installed)? It needs this to run." y; then
          tmux send-keys -t duke Enter; ui_ok "allowed"; sleep 3
        else
          ui_warn "the agent is waiting for that answer: run 'blimp logs' and choose Yes"
        fi
        return 0 ;;
      *"Listening for channel"*|*"bypass permissions"*|*"? for shortcuts"*) return 0 ;;
    esac
    sleep 1; i=$((i + 1))
  done
}

start_agent() {  # start_agent [keep]   keep = leave a running agent alone
  local ai; ai="$(cfg_get BLIMP_AI)"
  if [ "${1:-}" = keep ] && tmux has-session -t duke 2>/dev/null; then ui_ok "the agent is already running"; return 0; fi
  ln -sfn "$REPO" "$BLIMP_HOME/repo"
  if [ "$ai" = codex ]; then
    local bpy="$BLIMP_HOME/duke-venv/bin/python"; [ -x "$bpy" ] || bpy="$(command -v "$(py3)")"
    tmux kill-session -t duke 2>/dev/null || true
    tmux new-session -d -s duke -c "$REPO" "export BLIMP_HOME='$BLIMP_HOME' PATH='$PATH'; while true; do '$bpy' blimp-infra/codex-bridge/bridge.py; sleep 5; done"
  else
    bash "$REPO/blimp-infra/boot-text-blimp.sh" >> "$UI_LOG" 2>&1 || stop "the agent did not start." "See the log: $UI_LOG"
    trust_prompt
  fi
  sleep 2
  tmux has-session -t duke 2>/dev/null || stop "the agent stopped right after starting." "Watch it start: tmux attach -t duke"
  ui_ok "the agent is running"
}

if [ "$MODE" = start ]; then
  NAME="$(cfg_get BLIMP_NAME)"; ui_banner "${NAME:-Blimp}" "Starting"
  [ -n "$(cfg_get BLIMP_AI)" ] || stop "setup has not finished yet." "Run: bash $REPO/setup.sh"
  load_keys
  ui_step "Memory"; start_memory
  ui_step "Agent"; start_agent keep
  ui_done "Running"
  exit 0
fi

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
ui_banner "Blimp" "Your own AI agent, on your own computer"
[ "$CI" = 1 ] && ui_info "unattended mode (CI): logins and the agent start are skipped"

# 1 -------------------------------------------------------------------------
ui_step "Checking this computer"
[ "$(id -u)" != 0 ] || stop "setup is running as the administrator (root)." "Run it as yourself: bash setup.sh (no sudo)."
case "$OS" in
  macos)
    MACV="$(sw_vers -productVersion)"; MAJOR="${MACV%%.*}"
    ui_ok "macOS $MACV on $(uname -m | sed 's/arm64/Apple silicon/; s/x86_64/Intel/')"
    [ "$MAJOR" -ge 13 ] || ui_warn "macOS $MACV is old; Homebrew may not support it. macOS 14 or newer is safest." ;;
  linux|windows)
    . /etc/os-release 2>/dev/null || true
    [ "${ID:-}" = ubuntu ] || stop "this needs Ubuntu (found ${PRETTY_NAME:-unknown Linux})." "On Windows, use START-BLIMP.bat: it installs Ubuntu for you."
    ui_ok "${PRETTY_NAME:-Ubuntu}$([ "$OS" = windows ] && echo ' on Windows (WSL)')" ;;
esac
FREE_GB=$(( $(df -Pk "$HOME" | awk 'NR==2 {print $4}') / 1048576 ))
if [ "$FREE_GB" -lt 8 ]; then stop "only ${FREE_GB} GB free on this disk; setup needs about 8 GB." "Free some space and run setup again."
else ui_ok "${FREE_GB} GB free"; fi
curl -fsS -m 15 -o /dev/null https://github.com 2>>"$UI_LOG" || stop "cannot reach the internet (github.com)." "Check the Wi-Fi or VPN and run setup again."
ui_ok "online"
cfg_set BLIMP_OS "$OS"

# 2 -------------------------------------------------------------------------
ui_step "A few questions"
if [ "$CI" = 1 ]; then
  cfg_set BLIMP_AI "${BLIMP_AI:-claude}"; cfg_set BLIMP_NAME "${BLIMP_NAME:-Blimp}"
  cfg_set BLIMP_AUTO no; cfg_set BLIMP_AGENT_AUTO no
  [ -n "${BLIMP_MEMORY:-}" ] && cfg_set BLIMP_MEMORY "$BLIMP_MEMORY"
  for k in OPENROUTER_API_KEY DISCORD_BOT_TOKEN DISCORD_OWNER_USER_ID; do
    eval "v=\${$k:-}"; if [ -n "$v" ]; then key_set "$k" "$v"; fi
  done
else
  case "$(cfg_get BLIMP_AI)" in
    claude|codex) ui_ok "AI: $(cfg_get BLIMP_AI)" ;;
    *)
      printf '\n     Which AI runs your agent?\n'
      printf '       1) Claude  %s(needs a Claude Pro or Max plan; recommended)%s\n' "$C_DIM" "$C_RESET"
      printf '       2) Codex   %s(needs ChatGPT Plus or higher)%s\n' "$C_DIM" "$C_RESET"
      while :; do
        ui_ask a "Type 1 or 2:" 1 || stop "no answer."
        case "$a" in 1) cfg_set BLIMP_AI claude; break ;; 2) cfg_set BLIMP_AI codex; break ;; esac
      done ;;
  esac
  if [ -z "$(cfg_get BLIMP_NAME)" ]; then
    ui_ask n "What should your system be called?" Blimp || stop "no answer."
    n="$(printf '%s' "$n" | tr -cd 'A-Za-z0-9 _-')"; cfg_set BLIMP_NAME "${n:-Blimp}"
  fi
  ui_ok "name: $(cfg_get BLIMP_NAME)"

  # Memory runs on a small cheap model through OpenRouter, or (Codex only) on the ChatGPT login.
  case "$(cfg_get BLIMP_MEMORY)" in ""|none) ask_mem=1 ;; *) ask_mem=0 ;; esac
  if [ -z "$(key_get OPENROUTER_API_KEY)" ] && [ "$ask_mem" = 1 ]; then
    printf '\n     Long-term memory uses a small, cheap AI model through OpenRouter (openrouter.ai, Keys).\n'
    if [ "$(cfg_get BLIMP_AI)" = codex ]; then
      printf '     %sNo key? Press Enter to run it on your ChatGPT login instead, or type skip for no memory.%s\n' "$C_DIM" "$C_RESET"
    else
      printf '     %sNo key yet? Type skip: everything else works, and you can add memory later.%s\n' "$C_DIM" "$C_RESET"
    fi
    while :; do
      ui_secret k "OpenRouter key (starts with sk-or-):" || stop "no answer."
      k="$(printf '%s' "$k" | tr -d '[:space:]')"
      case "$k" in
        sk-or-*) key_set OPENROUTER_API_KEY "$k"; cfg_set BLIMP_MEMORY ""; break ;;
        skip) cfg_set BLIMP_MEMORY none; break ;;
        "") [ "$(cfg_get BLIMP_AI)" = codex ] && { cfg_set BLIMP_MEMORY pip; break; } ;;
      esac
      ui_info "That is not an OpenRouter key. It starts with sk-or-."
    done
  fi
  if [ -z "$(key_get DISCORD_BOT_TOKEN)" ]; then
    printf '\n     Your Discord bot token: discord.com/developers, your app, Bot, Reset Token.\n'
    while :; do
      ui_secret t "Bot token:" || stop "no answer."
      t="$(printf '%s' "$t" | tr -d '[:space:]')"
      case "$t" in *.*.*) [ ${#t} -ge 50 ] && { key_set DISCORD_BOT_TOKEN "$t"; break; } ;; esac
      ui_info "That does not look like a bot token (it is long, with two dots). Copy it again."
    done
  fi
  if [ -z "$(key_get DISCORD_OWNER_USER_ID)" ]; then
    printf '\n     Your own Discord user ID, so the bot answers only you.\n'
    printf '     %sDiscord, Settings, Advanced, turn on Developer Mode; then right-click your name, Copy User ID.%s\n' "$C_DIM" "$C_RESET"
    while :; do
      ui_ask u "Your user ID:" || stop "no answer."
      u="$(printf '%s' "$u" | tr -d '[:space:]')"
      case "$u" in *[!0-9]*|"") ;; *) [ ${#u} -ge 15 ] && [ ${#u} -le 21 ] && { key_set DISCORD_OWNER_USER_ID "$u"; break; } ;; esac
      ui_info "A user ID is a number, 17 to 20 digits long."
    done
  fi
  if [ -z "$(cfg_get BLIMP_AUTO)" ]; then
    if ui_yes "The doctor fixes problems by running commands. Let it run them without asking each time?" n
    then cfg_set BLIMP_AUTO yes; else cfg_set BLIMP_AUTO no; fi
  fi
  if [ "$(cfg_get BLIMP_AI)" = codex ] && [ -z "$(cfg_get BLIMP_AGENT_AUTO)" ]; then
    if ui_yes "Let the agent run commands on this computer without asking first? Anyone who can message the bot as you could use this." n
    then cfg_set BLIMP_AGENT_AUTO yes; else cfg_set BLIMP_AGENT_AUTO no; fi
  fi
fi
AI="$(cfg_get BLIMP_AI)"; NAME="$(cfg_get BLIMP_NAME)"
# A key added by hand turns the memory back on.
if [ -n "$(key_get OPENROUTER_API_KEY)" ] && ! memory_on; then cfg_set BLIMP_MEMORY ""; fi
# The Mac always runs memory without Docker; elsewhere Docker unless Codex chose the ChatGPT login.
if memory_on; then
  if [ "$OS" = macos ] || [ "$(cfg_get BLIMP_MEMORY)" = pip ]; then cfg_set BLIMP_MEMORY pip; else cfg_set BLIMP_MEMORY docker; fi
fi
[ -n "$(key_get OPENROUTER_API_KEY)" ] || { [ "$(cfg_get BLIMP_MEMORY)" != docker ] || stop "no OpenRouter key for the memory." "Run setup again and paste one, or type skip."; }
[ -n "$(key_get DISCORD_BOT_TOKEN)" ] && [ -n "$(key_get DISCORD_OWNER_USER_ID)" ] || stop "the Discord answers are missing." "Run setup again."
load_keys
ui_ok "memory: $(cfg_get BLIMP_MEMORY | sed 's/pip/on/; s/docker/on/; s/none/off/')"

# 3 -------------------------------------------------------------------------
ui_step "Installing tools"
if [ "$OS" = macos ]; then
  if ! command -v brew >/dev/null; then
    sudo_once
    install_brew() { NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; }
    ui_run "Homebrew (the Mac's package manager; a few minutes)" install_brew \
      || stop "Homebrew did not install." "Your Mac account must be an administrator. Details are in the log above."
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && { eval "$("$b" shellenv)"; add_path_line "eval \"\$($b shellenv)\""; break; }; done
    command -v brew >/dev/null || stop "Homebrew installed but cannot be found." "Open a new Terminal window and run setup again."
  fi
  ui_ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"
  PKGS="git jq tmux python@3.13"; [ "$AI" = codex ] && PKGS="$PKGS node"
  missing=""; for p in $PKGS; do brew list --versions "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then
    ui_run "Installing$missing" env HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install -q $missing \
      || stop "Homebrew could not install$missing." "Run: brew doctor   then run setup again."
  else ui_ok "git, jq, tmux, Python"; fi
else
  sudo_once
  PKGS="curl ca-certificates git build-essential python3 python3-pip python3-venv jq tmux unzip"
  apt_go() { sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS; }
  ui_run "System packages" apt_go || stop "the system packages did not install." "Run: sudo apt-get update   then setup again."
  # The memory's local models need Python 3.11-3.13 (Hindsight docs). Newer Ubuntu
  # (26.04) ships only 3.14, so fall back to uv's standalone Python 3.13.
  export PATH="$HOME/.local/bin:$PATH"
  have_py=0; for v in 3.13 3.12 3.11; do command -v "python$v" >/dev/null 2>&1 && have_py=1 && break; done
  if [ "$(cfg_get BLIMP_MEMORY)" = pip ] && [ "$have_py" = 0 ]; then
    py_new() {
      local v
      for v in 3.13 3.12 3.11; do
        apt-cache show "python$v-venv" >/dev/null 2>&1 && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "python$v" "python$v-venv" && return 0
      done
      local uvd="$BLIMP_HOME/uv"; mkdir -p "$uvd" "$HOME/.local/bin"
      [ -x "$uvd/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$uvd" UV_NO_MODIFY_PATH=1 sh || return 1
      UV_PYTHON_INSTALL_DIR="$BLIMP_HOME/python" "$uvd/uv" python install 3.13 || return 1
      ln -sf "$(UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_DIR="$BLIMP_HOME/python" "$uvd/uv" python find 3.13)" "$HOME/.local/bin/python3.13"
      "$HOME/.local/bin/python3.13" -m venv --help >/dev/null
    }
    ui_run "Python 3.13 for the memory" py_new || stop "Python 3.11-3.13 could not be installed." "Check the internet connection, then run setup again."
  fi
  if [ "$AI" = codex ] && { ! command -v node >/dev/null || [ "$(node -v | tr -d v | cut -d. -f1)" -lt 22 ]; }; then
    node_go() { curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y -qq nodejs; }
    ui_run "Node.js 22" node_go || stop "Node.js did not install."
  fi
  ui_ok "git, jq, tmux, Python"
fi
add_path_line 'export PATH="$HOME/.local/bin:$PATH"'
if [ "$AI" = codex ]; then
  if ! command -v codex >/dev/null; then
    if [ "$OS" = macos ]; then ui_run "Codex" npm install -g @openai/codex || stop "Codex did not install."
    else ui_run "Codex" sudo npm install -g @openai/codex || stop "Codex did not install."; fi
  fi
  ui_ok "Codex $(codex --version 2>/dev/null | awk '{print $NF}')"
else
  if ! command -v bun >/dev/null; then
    bun_go() { curl -fsSL https://bun.sh/install | bash; }
    ui_run "Bun (runs the Discord link)" bun_go || stop "Bun did not install."
  fi
  ui_ok "Bun $(bun --version 2>/dev/null)"
  if ! command -v claude >/dev/null; then
    # Anthropic's native installer (docs: code.claude.com/docs/en/setup); it updates itself.
    claude_go() { curl -fsSL https://claude.ai/install.sh | bash; }
    ui_run "Claude Code" claude_go || stop "Claude Code did not install."
    hash -r
    command -v claude >/dev/null || stop "Claude Code installed but cannot be found." "Open a new terminal window and run setup again."
  fi
  ui_ok "Claude Code $(claude --version 2>/dev/null | awk '{print $1}')"
fi

# 4 + 5 -----------------------------------------------------------------------
ln -sfn "$REPO" "$BLIMP_HOME/repo"
HS_PORT="${HINDSIGHT_API_PORT:-8888}"
ui_step "Memory"
if ! memory_on; then
  ui_info "memory is off (run setup again with a key to turn it on)"
elif [ -x "$BLIMP_HOME/duke-venv/bin/python" ] && [ -n "$(key_get DUKE_DB_URL)" ] && curl -fs "http://127.0.0.1:$HS_PORT/health" >/dev/null 2>&1; then
  ui_ok "already set up and running"
else
  export REPO_ROOT="$REPO"
  [ "$CI" = 1 ] && export HINDSIGHT_CI=1
  [ "$(cfg_get BLIMP_MEMORY)" = pip ] && export HINDSIGHT_MODE=pip
  # Memory on the ChatGPT login: its own one-time sign-in, shown here (not behind a spinner).
  if [ "$AI" = codex ] && [ -z "$(key_get OPENROUTER_API_KEY)" ] && [ "$CI" != 1 ] && [ ! -s "$BLIMP_HOME/hindsight-codex/auth.json" ]; then
    mkdir -p "$BLIMP_HOME/hindsight-codex"
    ui_info "the memory needs its own ChatGPT sign-in (separate from the agent's). Follow the link it shows."
    CODEX_HOME="$BLIMP_HOME/hindsight-codex" codex login || true
    [ -s "$BLIMP_HOME/hindsight-codex/auth.json" ] || stop "the memory's ChatGPT sign-in did not finish." "Run setup again to retry."
  fi
  if [ "$OS" = macos ]; then
    ui_run "Database, vector search and long-term memory (first time: 10 to 20 minutes)" bash blimp-infra/postgres/bootstrap-duke-memory-mac.sh \
      || stop "the memory did not finish setting up." "Run setup again; it carries on where it stopped. Or: blimp doctor"
  else
    ui_run "Database, vector search and long-term memory (first time: 10 to 20 minutes)" bash blimp-infra/postgres/bootstrap-duke-memory.sh \
      || stop "the memory did not finish setting up." "Run setup again; it carries on where it stopped. Or: blimp doctor"
    if [ -z "$(key_get DUKE_DB_URL)" ]; then  # the Linux script leaves the address in its wrappers only
      PG_PORT="$(sudo -u postgres psql -tAc 'SHOW port;' 2>/dev/null | tr -d ' ')"
      key_set DUKE_DB_URL "'postgresql:///duke_data?host=/var/run/postgresql&port=${PG_PORT:-5432}'"
    fi
  fi
  load_keys
  ui_ok "Postgres with pgvector, empty and ready"
  ui_ok "long-term memory answering on port $HS_PORT"
fi

ui_step "Connecting the memory to the agent"
if memory_on; then
  load_keys
  MCP_PY="$BLIMP_HOME/duke-venv/bin/python"; MCP_PATH="$REPO/blimp-infra/mcp/blimp_brain_mcp.py"
  if [ "$AI" = codex ]; then
    codex mcp get blimp-brain >/dev/null 2>&1 || ui_run "Registering the memory tools" \
      codex mcp add blimp-brain --env A2A_AGENT_NAME=duke ${DUKE_DB_URL:+--env "DUKE_DB_URL=$DUKE_DB_URL"} -- "$MCP_PY" "$MCP_PATH" \
      || stop "Codex did not accept the memory tools."
  else
    claude mcp get blimp-brain >/dev/null 2>&1 || ui_run "Registering the memory tools" \
      claude mcp add -s user blimp-brain -e A2A_AGENT_NAME=duke ${DUKE_DB_URL:+-e "DUKE_DB_URL=$DUKE_DB_URL"} -- "$MCP_PY" "$MCP_PATH" \
      || stop "Claude Code did not accept the memory tools."
  fi
  ui_ok "memory tools connected"
else
  ui_info "skipped (memory is off)"
fi
ui_ok "$(ls -d "$REPO"/.claude/skills/*/ 2>/dev/null | wc -l | tr -d ' ') skills and $(ls "$REPO"/.claude/agents/*.md 2>/dev/null | wc -l | tr -d ' ') helper agents included"

# 6 -------------------------------------------------------------------------
ui_step "Discord"
printf 'DISCORD_BOT_TOKEN=%s\n' "$DISCORD_BOT_TOKEN" > "$STATE/.env"; chmod 600 "$STATE/.env"
if [ ! -f "$STATE/access.json" ]; then
  printf '{ "dmPolicy": "allowlist", "allowFrom": ["%s"], "groups": {}, "pending": {} }\n' "$DISCORD_OWNER_USER_ID" > "$STATE/access.json"
fi
ui_ok "the bot answers only you"
if [ "$CI" != 1 ]; then
  out="$("$(py3)" "$REPO/blimp-infra/doctor/doctor.py" --json --only token 2>/dev/null || true)"
  case "$out" in
    *'"status": "ok"'*) ui_ok "$(printf '%s' "$out" | sed -n 's/.*"detail": "\(.*\)",*/\1/p' | head -1)" ;;
    *'"status": "warn"'*) ui_warn "could not test the bot token right now (Discord did not answer)" ;;
    *'rejects the bot token'*)
       sed -i.bak '/^DISCORD_BOT_TOKEN=/d' "$ENV_FILE"; rm -f "$ENV_FILE.bak"
       stop "Discord rejects the bot token (it was reset or mistyped)." "Copy a fresh one (Bot, Reset Token), then run setup again; it asks for it." ;;
    *) printf '%s\n' "$out" | sed -n 's/.*"detail": "\(.*\)",*/\1/p; s/.*"hint": "\(.*\)",*/\1/p' | sed 's/^/       /'
       stop "the Discord bot is not ready yet (above)." "Fix it as shown, then run setup again. Your token is kept." ;;
  esac
fi

# 7 -------------------------------------------------------------------------
ui_step "Signing in"
if [ "$CI" = 1 ]; then
  ui_info "skipped in unattended mode"
elif [ "$AI" = codex ]; then
  if codex login status >/dev/null 2>&1; then ui_ok "Codex is signed in"
  else
    ui_info "A browser window opens: sign in with your ChatGPT account, then come back here."
    codex login || stop "the Codex sign-in did not finish." "Run setup again."
    ui_ok "Codex is signed in"
  fi
else
  if claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then ui_ok "Claude is signed in"
  else
    ui_info "A browser window opens: sign in with your Claude account, then come back here."
    claude auth login || stop "the Claude sign-in did not finish." "Run setup again."
    ui_ok "Claude is signed in"
  fi
fi
if [ "$AI" != codex ]; then
  if claude plugin list 2>/dev/null | grep -q 'discord@claude-plugins-official'; then ui_ok "Discord plugin installed"
  else
    plug_go() { claude plugin marketplace add anthropics/claude-plugins-official || true; claude plugin install discord@claude-plugins-official; }
    ui_run "Discord plugin" plug_go || stop "the Discord plugin did not install." "Run setup again, or: blimp doctor"
  fi
fi

# 8 -------------------------------------------------------------------------
ui_step "Starting $NAME"
BLIMP_CMD="$HOME/.local/bin/blimp"; mkdir -p "$HOME/.local/bin"
sed "s|@REPO@|$REPO|g" "$REPO/blimp-infra/agent/blimp-cli.sh" > "$BLIMP_CMD"; chmod 755 "$BLIMP_CMD"
ui_ok "command installed: blimp (start, stop, status, logs, doctor, update)"
if [ "$CI" = 1 ]; then
  ui_info "the agent is not started in unattended mode"
else
  start_agent
  if [ "$OS" = macos ]; then
    PLIST="$HOME/Library/LaunchAgents/com.blimp.start.plist"
    if [ ! -f "$PLIST" ] && [ -z "$(cfg_get BLIMP_LOGIN_ITEM)" ]; then
      if ui_yes "Start $NAME by itself when you log in to this Mac?" y; then
        mkdir -p "$HOME/Library/LaunchAgents"
        sed -e "s|@REPO@|$REPO|g" -e "s|@LOGS@|$BLIMP_HOME/logs|g" "$REPO/blimp-infra/agent/com.blimp.start.plist" > "$PLIST"
        # macOS loads it at the next login; nothing to start now (the agent is already running).
        cfg_set BLIMP_LOGIN_ITEM yes; ui_ok "starts at login"
      else cfg_set BLIMP_LOGIN_ITEM no; ui_info "after a restart, run: blimp start"; fi
    fi
  fi
fi

# 9 -------------------------------------------------------------------------
ui_step "Final check"
HUMAN=""; [ "$CI" = 1 ] && HUMAN="--skip-human"
if summary="$("$(py3)" "$REPO/blimp-infra/doctor/doctor.py" --lint --quiet $HUMAN 2>>"$UI_LOG")"; then
  ui_ok "$summary"
else
  "$(py3)" "$REPO/blimp-infra/doctor/doctor.py" --lint $HUMAN || true
  stop "the final check found problems (above)." "Fix them with: blimp doctor"
fi

cfg_set BLIMP_SETUP_DONE yes
ui_done "$NAME is ready"
if [ "$CI" != 1 ]; then
  printf '\n  %sSay hi:%s DM your bot on Discord. It answers only you.\n' "$C_BOLD" "$C_RESET"
  printf '\n  %sblimp status%s   is everything healthy?\n' "$C_BOLD" "$C_RESET"
  printf '  %sblimp logs%s     watch the agent work (leave with Ctrl+B, then D)\n' "$C_BOLD" "$C_RESET"
  printf '  %sblimp stop%s     stop it;  %sblimp start%s  start it again\n' "$C_BOLD" "$C_RESET" "$C_BOLD" "$C_RESET"
  printf '  %sblimp doctor%s   something wrong? It finds and fixes it with you.\n' "$C_BOLD" "$C_RESET"
  printf '\n  %sNew terminal windows know the blimp command; this one may need: export PATH="$HOME/.local/bin:$PATH"%s\n\n' "$C_DIM" "$C_RESET"
fi
