#!/usr/bin/env bash
# Start the agent (Claude Code + the Discord plugin) in the background, in tmux
# session 'duke'. Called by setup.sh (--start, the Mac login item, START-BLIMP.bat
# and blimp start). Safe to run again: it restarts the agent.
set -e
H="${BLIMP_HOME:-$HOME/blimp}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$H/discord-state"

tmux kill-session -t duke 2>/dev/null || true
mkdir -p "$STATE/approved" "$STATE/inbox"
if [ ! -f "$STATE/.env" ]; then
  grep -E '^DISCORD_BOT_TOKEN=' "$H/.env" > "$STATE/.env" || { echo "No DISCORD_BOT_TOKEN in $H/.env: run setup.sh"; exit 1; }
  chmod 600 "$STATE/.env"
fi
if [ ! -f "$STATE/access.json" ]; then
  owner="$(grep -E '^DISCORD_OWNER_USER_ID=' "$H/.env" | tail -1 | cut -d= -f2- | tr -cd '0-9')"
  [ -n "$owner" ] || { echo "No DISCORD_OWNER_USER_ID in $H/.env: run setup.sh"; exit 1; }
  printf '{ "dmPolicy": "allowlist", "allowFrom": ["%s"], "groups": {}, "pending": {} }\n' "$owner" > "$STATE/access.json"
fi
[ -f "$REPO/CLAUDE.local.md" ] || bash "$REPO/blimp-infra/agent/set-identity.sh" duke >/dev/null

# Homebrew's tools (Mac) and the user's own installs must be on PATH inside tmux too.
P="\$HOME/.bun/bin:\$HOME/.local/bin:\$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
tmux new-session -d -s duke -c "$REPO" \
  "export CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 BLIMP_FRESH=${BLIMP_FRESH:-} BLIMP_AGENT=duke BLIMP_HOME='$H' PATH=\"$P\" DISCORD_STATE_DIR='$STATE' DUKE_CHANNEL=discord; set -a; . '$H/.env'; set +a; bash '$REPO/blimp-infra/agent/launch.sh' --model ${BLIMP_CLAUDE_MODEL:-claude-sonnet-5-5} --effort medium --channels plugin:discord@claude-plugins-official"
