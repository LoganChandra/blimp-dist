#!/usr/bin/env bash
# Blimp one-step setup. Run it again any time: the first run installs, later
# runs just start Blimp. Works on Ubuntu 22.04+ and on WSL2 (Windows).
# macOS is experimental and untested (see README).
#   Windows: double-click START-BLIMP.bat (it calls this file).
#   Linux / macOS: bash setup.sh
# If anything fails, setup offers the doctor (blimp-infra/doctor): a chat that
# diagnoses and fixes setup problems. Run it any time: python3 blimp-infra/doctor/doctor.py
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REPO="$(pwd)"
BLIMP_HOME="$HOME/blimp"
ENV_FILE="$BLIMP_HOME/.env"
STATE="$BLIMP_HOME/discord-state"
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mStopped: %s\033[0m\n' "$*"; exit 1; }

mkdir -p "$BLIMP_HOME/logs" "$STATE/approved" "$STATE/inbox"
CONF="$BLIMP_HOME/config.env"; touch "$CONF"; chmod 600 "$CONF"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
cfg_get() { grep "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2-; }
cfg_set() { sed -i.bak "/^$1=/d" "$CONF" && rm -f "$CONF.bak"; printf '%s=%s\n' "$1" "$2" >> "$CONF"; }

# If setup stops for any reason, offer the doctor.
on_exit() {
  local rc=$?
  (( rc == 0 )) && return
  [[ -f "$REPO/blimp-infra/doctor/doctor.py" ]] || return
  printf '\n\033[1;33mSetup stopped. The doctor can find out why and fix it with you.\033[0m\n'
  read -rp "Start the doctor now? [Y/n] " a || return
  [[ "${a:-y}" =~ ^[Yy] ]] && python3 "$REPO/blimp-infra/doctor/doctor.py" || true
}
trap on_exit EXIT

# ---- 0. First-run choices (asked once, saved to ~/blimp/config.env) --------
choose() {  # choose KEY "question" opt1 opt2 ...  -> stores the option text
  local key="$1" q="$2"; shift 2
  [[ -n "$(cfg_get "$key")" ]] && return 0
  local i=1 o v; printf '\n%s\n' "$q"
  for o in "$@"; do printf '  %d) %s\n' "$i" "$o"; i=$((i+1)); done
  while :; do
    read -rp "Number: " v
    if [[ "$v" =~ ^[0-9]+$ ]] && (( v >= 1 && v <= $# )); then cfg_set "$key" "${!v}"; return 0; fi
    echo "     Type one of the numbers above."
  done
}
choose BLIMP_OS "What computer are you on?" windows linux macos
HOST="$(uname -s)"
case "$(cfg_get BLIMP_OS):$HOST" in
  windows:Linux|linux:Linux|macos:Darwin) ;;  # windows runs this inside WSL, which reports Linux
  *) sed -i.bak '/^BLIMP_OS=/d' "$CONF"; rm -f "$CONF.bak"; fail "you picked $(cfg_get BLIMP_OS) but this machine reports $HOST. Run setup again and pick the right one." ;;
esac
BLIMP_OS="$(cfg_get BLIMP_OS)"
choose BLIMP_AI "Which AI should run your system?" claude codex deepseek "other (I will type the model name)"
if [[ "$(cfg_get BLIMP_AI)" == other* ]]; then
  read -rp "Model name (as listed on openrouter.ai, e.g. qwen/qwen3-max): " m; m="${m//[[:space:]]/}"
  [[ -n "$m" ]] || fail "no model typed."
  cfg_set BLIMP_AI custom; cfg_set BLIMP_MODEL "$m"
fi
BLIMP_AI="$(cfg_get BLIMP_AI)"
# Which program runs the Discord agent: codex only for Codex users, Claude otherwise.
RUNTIME=claude; [[ "$BLIMP_AI" == codex ]] && RUNTIME=codex
[[ "$BLIMP_AI" == deepseek || "$BLIMP_AI" == custom ]] && echo "Note: the doctor and memory will use $BLIMP_AI, but the Discord agent itself still needs Claude."
if [[ -z "$(cfg_get BLIMP_NAME)" ]]; then
  read -rp $'\nWhat do you want to call your system? [Blimp] ' n; n="${n:-Blimp}"; n="${n//[^A-Za-z0-9 _-]/}"
  cfg_set BLIMP_NAME "${n:-Blimp}"
fi
BLIMP_NAME="$(cfg_get BLIMP_NAME)"
if [[ -z "$(cfg_get BLIMP_AUTO)" ]]; then
  printf '\nThe doctor fixes problems by running commands (some need admin rights).\n'
  read -rp "Let it run them itself without asking each time? [y/N] " a
  [[ "${a:-n}" =~ ^[Yy] ]] && cfg_set BLIMP_AUTO yes || cfg_set BLIMP_AUTO no
fi
if [[ "$BLIMP_AI" == codex && -z "$(cfg_get BLIMP_AGENT_AUTO)" ]]; then
  printf '\nSeparate question: your Discord agent can also run commands on this computer when you ask it to.\n'
  read -rp "Let it do that without asking first? Anyone who can DM your bot as you could use this. [y/N] " a
  [[ "${a:-n}" =~ ^[Yy] ]] && cfg_set BLIMP_AGENT_AUTO yes || cfg_set BLIMP_AGENT_AUTO no
fi
say "$BLIMP_NAME setup on $BLIMP_OS, AI: $BLIMP_AI"
[[ "$BLIMP_OS" == macos ]] && echo "Note: macOS is experimental and untested. The memory service may need the doctor."

# ---- 1. Your AI login or key, then your answers (saved to ~/blimp/.env) ----
no_key_help() { cat <<HELP

  You need an AI login or key before setup can go on. A short checklist:
    Claude:    sign up for a Claude Pro or Max plan at claude.ai (the login comes later in setup),
               and an OpenRouter key below (it powers the memory).
    Codex:     sign up for ChatGPT Plus or higher at chatgpt.com, then run: npm install -g @openai/codex && codex login
    DeepSeek / other: make an account at openrouter.ai, add a few dollars of credit,
               then Keys -> Create key. It starts with sk-or-
  Run setup again when you have it.

HELP
}
ask() {  # ask NAME "question" [regex]   -> skips if already saved
  local name="$1" q="$2" re="${3:-.}" v
  grep -q "^$name=." "$ENV_FILE" && return 0
  while :; do
    read -rp "$q " v || fail "no input."
    v="${v//[[:space:]]/}"
    if [[ -z "$v" ]]; then no_key_help; trap - EXIT; exit 1; fi
    [[ "$v" =~ $re ]] && break
    echo "     That doesn't look right, try again (press Enter on an empty line if you don't have one yet)."
  done
  sed -i.bak "/^$name=/d" "$ENV_FILE" && rm -f "$ENV_FILE.bak"; printf '%s=%s\n' "$name" "$v" >> "$ENV_FILE"
}
say "Three questions (see README.md, step 2, for where to find each)"
NOMEM=no
BM="$(cfg_get BLIMP_MEMORY)"
if [[ "$RUNTIME" == codex ]] && [[ "$BM" == pip ]] && ! grep -q '^OPENROUTER_API_KEY=.' "$ENV_FILE"; then export HINDSIGHT_MODE=pip
elif [[ "$RUNTIME" == codex ]] && [[ "$BM" == none ]] && ! grep -q '^OPENROUTER_API_KEY=.' "$ENV_FILE"; then NOMEM=yes
elif [[ "$RUNTIME" == codex ]] && ! grep -q '^OPENROUTER_API_KEY=.' "$ENV_FILE"; then
  while :; do
    read -rp "1/3  OpenRouter key for memory (sk-or-...). Enter = use your ChatGPT login instead, no key; type skip = no memory: " k || fail "no input."; k="${k//[[:space:]]/}"
    if [[ "$k" =~ ^sk-or- ]]; then printf 'OPENROUTER_API_KEY=%s\n' "$k" >> "$ENV_FILE"; cfg_set BLIMP_MEMORY docker; break
    elif [[ "$k" == skip ]]; then NOMEM=yes; cfg_set BLIMP_MEMORY none; echo "     OK: no-memory mode. To turn memory on later, set BLIMP_MEMORY= (empty) in $CONF and run setup again."; break
    elif [[ -z "$k" ]]; then export HINDSIGHT_MODE=pip; cfg_set BLIMP_MEMORY pip; echo "     OK: memory runs on your ChatGPT login (personal use only per Hindsight's docs; one extra login later)."; break
    else echo "     That is not a key. Paste a key starting with sk-or-, or press Enter, or type skip."; fi
  done
else
  ask OPENROUTER_API_KEY    "1/3  OpenRouter API key (sk-or-...):" '^sk-or-'
fi
ask DISCORD_BOT_TOKEN     "2/3  Discord bot token for the agent:"
ask DISCORD_OWNER_USER_ID "3/3  Your own Discord user ID (a long number):" '^[0-9]{15,21}$'
set -a; source "$ENV_FILE"; set +a

# ---- 2. Tools: git, python, node, bun, Claude Code -------------------------
say "Installing tools (asks for your password once)"
if [[ "$BLIMP_OS" == macos ]]; then
  command -v brew >/dev/null || fail "install Homebrew first (https://brew.sh), then run setup again."
  brew install git python jq tmux node@20 unzip >/dev/null
  [[ "${HINDSIGHT_MODE:-}" == pip ]] && brew install python@3.13 >/dev/null
  export PATH="$(brew --prefix node@20)/bin:$PATH"   # node@20 is keg-only
  grep -q "node@20" "$HOME/.zprofile" 2>/dev/null || echo "export PATH=\"$(brew --prefix node@20)/bin:\$PATH\"" >> "$HOME/.zprofile"
else
  command -v apt-get >/dev/null || fail "this needs Ubuntu (or WSL2 Ubuntu on Windows)."
  sudo apt-get update -y -qq
  sudo apt-get install -y -qq curl ca-certificates git build-essential python3 python3-pip python3-venv jq tmux unzip
  if [[ "${HINDSIGHT_MODE:-}" == pip ]] && ! command -v python3.13 python3.12 python3.11 >/dev/null 2>&1; then
    sudo apt-get install -y -qq python3.12 python3.12-venv || sudo apt-get install -y -qq python3.11 python3.11-venv || true
  fi
  if ! command -v node >/dev/null || (( $(node -v | cut -d. -f1 | tr -d v) < 20 )); then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y -qq nodejs
  fi
fi
[[ "$RUNTIME" == codex ]] || command -v bun >/dev/null || curl -fsSL https://bun.sh/install | bash
if [[ "$RUNTIME" == codex ]]; then
  command -v codex >/dev/null || { [[ "$BLIMP_OS" == macos ]] && npm install -g @openai/codex || sudo npm install -g @openai/codex; }
else
  command -v claude >/dev/null || { [[ "$BLIMP_OS" == macos ]] && npm install -g @anthropic-ai/claude-code || sudo npm install -g @anthropic-ai/claude-code; }
fi

# ---- 3. The shared brain: Postgres + Hindsight memory + MCP server --------
ln -sfn "$REPO" "$BLIMP_HOME/repo"
if [[ "$NOMEM" == yes ]]; then
  say "Skipping the shared brain (no OpenRouter key): memory is off"
elif [[ ! -x "$BLIMP_HOME/duke-venv/bin/python" ]] || ! curl -fs "http://127.0.0.1:${HINDSIGHT_API_PORT:-8888}/health" >/dev/null 2>&1; then
  say "Setting up the shared brain (first run takes 5-10 minutes)"
  if [[ "$BLIMP_OS" == macos ]]; then REPO_ROOT="$REPO" bash blimp-infra/postgres/bootstrap-duke-memory-mac.sh
  else REPO_ROOT="$REPO" bash blimp-infra/postgres/bootstrap-duke-memory.sh; fi
fi
set -a; source "$ENV_FILE"; set +a   # the brain script may have added DUKE_DB_URL
if [[ "$NOMEM" != yes ]]; then
MCP_PY="$BLIMP_HOME/duke-venv/bin/python"; MCP_PATH="$REPO/blimp-infra/mcp/blimp_brain_mcp.py"
if [[ "$RUNTIME" == codex ]]; then
  codex mcp get blimp-brain >/dev/null 2>&1 || codex mcp add blimp-brain --env A2A_AGENT_NAME=duke \
    ${DUKE_DB_URL:+--env "DUKE_DB_URL=$DUKE_DB_URL"} -- "$MCP_PY" "$MCP_PATH"
else
  claude mcp get blimp-brain >/dev/null 2>&1 || claude mcp add -s user blimp-brain \
    -e A2A_AGENT_NAME=duke ${DUKE_DB_URL:+-e "DUKE_DB_URL=$DUKE_DB_URL"} -- "$MCP_PY" "$MCP_PATH"
fi
fi

# ---- 4. Discord: token + who Duke listens to -------------------------------
printf 'DISCORD_BOT_TOKEN=%s\n' "$DISCORD_BOT_TOKEN" > "$STATE/.env"; chmod 600 "$STATE/.env"
[[ -f "$STATE/access.json" ]] || cat > "$STATE/access.json" <<JSON
{ "dmPolicy": "allowlist", "allowFrom": ["$DISCORD_OWNER_USER_ID"], "groups": {}, "pending": {} }
JSON

# ---- 5. Claude login + Discord plugin (only the first time) ----------------
if [[ "$RUNTIME" == codex ]]; then
  if ! codex login status >/dev/null 2>&1; then
    say "Log in to Codex with your ChatGPT account: a browser window opens."
    codex login || fail "Codex login did not finish. Run setup again."
  fi
else
  if ! claude auth status >/dev/null 2>&1; then
    say "Log in to Claude: a browser window opens. Sign in, then type /exit to come back here."
    claude || true
  fi
  if ! claude plugin list 2>/dev/null | grep -q 'discord@claude-plugins-official'; then
    claude plugin marketplace add anthropics/claude-plugins-official >/dev/null 2>&1 || true
    claude plugin install discord@claude-plugins-official
  fi
fi

# ---- 6. Start Duke ----------------------------------------------------------
say "Starting $BLIMP_NAME"
if [[ "$RUNTIME" == codex ]]; then
  tmux kill-session -t duke 2>/dev/null || true
  tmux new-session -d -s duke "export BLIMP_HOME=$(printf %q "$BLIMP_HOME") PATH=$(printf %q "$PATH"); cd $(printf %q "$REPO"); while true; do $(printf %q "$BLIMP_HOME")/duke-venv/bin/python blimp-infra/codex-bridge/bridge.py; sleep 5; done"
else
  bash blimp-infra/boot-text-blimp.sh >/dev/null
fi
sleep 3
tmux has-session -t duke 2>/dev/null || fail "the agent did not start. See README.md, 'If something goes wrong'."
cat <<DONE

  $BLIMP_NAME is running.

  - DM your bot on Discord and say hi. It answers only you.
  - Watch it work:      tmux attach -t duke     (leave with Ctrl+B then D)
  - Stop it:                tmux kill-session -t duke
  - Start it again:     double-click START-BLIMP.bat (or: bash setup.sh)
  - Something wrong?    python3 blimp-infra/doctor/doctor.py  (a chat that fixes it with you)

DONE
