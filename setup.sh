#!/usr/bin/env bash
# Blimp one-step setup. Run it again any time: the first run installs, later
# runs just start Blimp. Works on Ubuntu 22.04+ and on WSL2 (Windows).
#   Windows: double-click START-BLIMP.bat (it calls this file).
#   Linux:   bash setup.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REPO="$(pwd)"
BLIMP_HOME="$HOME/blimp"
ENV_FILE="$BLIMP_HOME/.env"
STATE="$BLIMP_HOME/discord-state"
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mStopped: %s\033[0m\n' "$*"; exit 1; }

command -v apt-get >/dev/null || fail "this needs Ubuntu (or WSL2 Ubuntu on Windows)."
mkdir -p "$BLIMP_HOME/logs" "$STATE/approved" "$STATE/inbox"

# ---- 1. Your three answers (asked once, saved to ~/blimp/.env) ------------
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
ask() {  # ask NAME "question" [regex]   -> skips if already saved
  local name="$1" q="$2" re="${3:-.}" v
  grep -q "^$name=." "$ENV_FILE" && return 0
  while :; do
    read -rp "$q " v; v="${v//[[:space:]]/}"
    [[ "$v" =~ $re ]] && break
    echo "     That doesn't look right, try again."
  done
  sed -i "/^$name=/d" "$ENV_FILE"; printf '%s=%s\n' "$name" "$v" >> "$ENV_FILE"
}
say "Three questions (see README.md, step 2, for where to find each)"
ask OPENROUTER_API_KEY    "1/3  OpenRouter API key (sk-or-...):" '^sk-or-'
ask DISCORD_BOT_TOKEN     "2/3  Discord bot token for Duke:"
ask DISCORD_OWNER_USER_ID "3/3  Your own Discord user ID (a long number):" '^[0-9]{15,21}$'
set -a; source "$ENV_FILE"; set +a

# ---- 2. Tools: git, python, node, bun, Claude Code -------------------------
say "Installing tools (asks for your password once)"
sudo apt-get update -y -qq
sudo apt-get install -y -qq curl ca-certificates git build-essential python3 python3-pip python3-venv jq tmux unzip
if ! command -v node >/dev/null || (( $(node -v | cut -d. -f1 | tr -d v) < 20 )); then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y -qq nodejs
fi
command -v bun >/dev/null || curl -fsSL https://bun.sh/install | bash
command -v claude >/dev/null || sudo npm install -g @anthropic-ai/claude-code

# ---- 3. The shared brain: Postgres + Hindsight memory + MCP server --------
ln -sfn "$REPO" "$BLIMP_HOME/repo"
if [[ ! -x "$BLIMP_HOME/duke-venv/bin/python" ]] || ! curl -fs "http://127.0.0.1:${HINDSIGHT_API_PORT:-8888}/health" >/dev/null 2>&1; then
  say "Setting up the shared brain (first run takes 5-10 minutes)"
  REPO_ROOT="$REPO" bash blimp-infra/postgres/bootstrap-duke-memory.sh
fi
claude mcp get blimp-brain >/dev/null 2>&1 || claude mcp add -s user blimp-brain \
  -e A2A_AGENT_NAME=duke -- "$BLIMP_HOME/duke-venv/bin/python" "$REPO/blimp-infra/mcp/blimp_brain_mcp.py"

# ---- 4. Discord: token + who Duke listens to -------------------------------
printf 'DISCORD_BOT_TOKEN=%s\n' "$DISCORD_BOT_TOKEN" > "$STATE/.env"; chmod 600 "$STATE/.env"
[[ -f "$STATE/access.json" ]] || cat > "$STATE/access.json" <<JSON
{ "dmPolicy": "allowlist", "allowFrom": ["$DISCORD_OWNER_USER_ID"], "groups": {}, "pending": {} }
JSON

# ---- 5. Claude login + Discord plugin (only the first time) ----------------
if ! claude auth status >/dev/null 2>&1; then
  say "Log in to Claude: a browser window opens. Sign in, then type /exit to come back here."
  claude || true
fi
if ! claude plugin list 2>/dev/null | grep -q 'discord@claude-plugins-official'; then
  claude plugin marketplace add anthropics/claude-plugins-official >/dev/null 2>&1 || true
  claude plugin install discord@claude-plugins-official
fi

# ---- 6. Start Duke ----------------------------------------------------------
say "Starting Duke"
bash blimp-infra/boot-text-blimp.sh >/dev/null
sleep 3
tmux has-session -t duke 2>/dev/null || fail "Duke did not start. See README.md, 'If something goes wrong'."
cat <<DONE

  Blimp is running.

  - DM your Duke bot on Discord and say hi. He answers only you.
  - Watch him work:      tmux attach -t duke     (leave with Ctrl+B then D)
  - Stop him:            tmux kill-session -t duke
  - Start him again:     double-click START-BLIMP.bat (or: bash setup.sh)

DONE
