#!/usr/bin/env bash
# bootstrap-hermes.sh
#
# Sets up Hermes — Blimp's Technical Lead, second AI cofounder. Runs on
# Hermes Agent framework + qwen3.7-max via OpenRouter, talks to the same
# Hindsight memory bank and Postgres database as Duke (via the blimp-brain
# MCP server installed by bootstrap-duke-memory.sh).
#
# Prerequisites (in order):
#   1. Run bootstrap-duke-memory.sh first — Hindsight + Postgres + blimp-brain MCP
#      must be in place. Hermes won't have a brain without it.
#   2. Have a Discord application created at
#      https://discord.com/developers/applications named "Hermes",
#      with Server Members + Message Content intents enabled,
#      and the bot invited to your Blimp server (scopes: bot + applications.commands).
#   3. Have ~/blimp/.env populated with OPENROUTER_API_KEY (the existing duke
#      bootstrap creates this).
#
# What this does:
#   1. Installs Hermes Agent via the official one-liner if not already
#   2. Configures OpenRouter + qwen/qwen3-max
#   3. Symlinks hermes-system-context.md → ~/.hermes/SOUL.md
#   4. Registers blimp-brain MCP with Hermes
#   5. Applies the Discord adapter patch (works around upstream bug)
#   6. Prompts for the Discord bot token (only if not already in ~/.hermes/.env)
#   7. Starts the gateway in a tmux session named `hermes`
#   8. Verifies Discord connection
#
# Idempotent — re-running is safe.

set -uo pipefail

log()  { printf '\n\033[1;36m[hermes]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m  err\033[0m %s\n' "$*"; }

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"  # repo root, wherever it is checked out
INFRA_DIR="$REPO_ROOT/blimp-infra"
HERMES_DIR="$HOME/.hermes"
HERMES_REPO="$HERMES_DIR/hermes-agent"
VENV="$HOME/blimp/duke-venv"
DUKE_USER_ID="${DISCORD_OWNER_USER_ID:-<ID>}"

# Load existing env (might contain OPENROUTER_API_KEY)
[ -f "$HOME/blimp/.env" ] && source "$HOME/blimp/.env" 2>/dev/null || true
[ -f "$HERMES_DIR/.env" ] && {
  set -a; source "$HERMES_DIR/.env" 2>/dev/null; set +a
} || true

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  err "OPENROUTER_API_KEY not set."
  err "Source it from your shell or add to ~/blimp/.env, then re-run."
  exit 1
fi
ok "OpenRouter API key present"

# ------------------------------------------------------------------
# 1) Install Hermes Agent if not present
# ------------------------------------------------------------------
log "[1/8] Hermes Agent installation"
if [ ! -d "$HERMES_REPO" ] || ! command -v hermes >/dev/null 2>&1; then
  log "    installing via official one-liner (~5 min)"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  # Reload PATH so `hermes` is available in this shell
  export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v hermes >/dev/null 2>&1; then
  err "hermes still not on PATH after install. Check ~/.local/bin/ and your shell's rc files."
  exit 1
fi
ok "hermes installed: $(hermes --version 2>&1 | head -1)"

# ------------------------------------------------------------------
# 2) Configure OpenRouter + qwen3.7-max
# ------------------------------------------------------------------
log "[2/8] Model provider config"
# Hermes uses ~/.hermes/.env for API keys + config.yaml for model selection.
# Persist key:
ENV_FILE="$HERMES_DIR/.env"
mkdir -p "$HERMES_DIR"
touch "$ENV_FILE"
_set_env_kv() {
  local k="$1" v="$2"
  if grep -q "^${k}=" "$ENV_FILE"; then
    sed -i.bak "s|^${k}=.*|${k}=${v}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
  else
    echo "${k}=${v}" >> "$ENV_FILE"
  fi
}
_set_env_kv OPENROUTER_API_KEY "$OPENROUTER_API_KEY"
_set_env_kv HERMES_API_LLM_PROVIDER "openrouter"
_set_env_kv HERMES_API_LLM_MODEL    "qwen/qwen3-max"
_set_env_kv HERMES_API_LLM_BASE_URL "https://openrouter.ai/api/v1"
ok "OpenRouter + qwen/qwen3-max written to $ENV_FILE"

# Tell Hermes which provider/model is default via config.yaml too
hermes config set model.default "qwen/qwen3-max" 2>&1 | head -1 || true
hermes config set model.provider "openrouter" 2>&1 | head -1 || true
ok "model defaults set in config.yaml"

# ------------------------------------------------------------------
# 3) SOUL.md symlink → repo-canonical persona
# ------------------------------------------------------------------
log "[3/8] Hermes SOUL (persona)"
SOUL_SRC="$INFRA_DIR/hermes-system-context.md"
SOUL_DST="$HERMES_DIR/SOUL.md"
if [ ! -f "$SOUL_SRC" ]; then
  err "missing $SOUL_SRC — repo state is wrong"; exit 1
fi
# Back up existing SOUL.md if it isn't already our symlink
if [ -L "$SOUL_DST" ] && [ "$(readlink -f "$SOUL_DST")" = "$(readlink -f "$SOUL_SRC")" ]; then
  ok "SOUL.md already pointing at repo file"
else
  [ -f "$SOUL_DST" ] && mv "$SOUL_DST" "$SOUL_DST.bak.$(date +%s)" || true
  ln -sf "$SOUL_SRC" "$SOUL_DST"
  ok "SOUL.md → $SOUL_SRC"
fi

# ------------------------------------------------------------------
# 4) Disable Hermes built-in memory — everything routes through blimp-brain
# ------------------------------------------------------------------
log "[4/8] Built-in memory off (using blimp-brain MCP instead)"
hermes config set memory.memory_enabled false 2>&1 | head -1 || true
hermes config set memory.user_profile_enabled false 2>&1 | head -1 || true
ok "built-in memory disabled"

# ------------------------------------------------------------------
# 5) Register blimp-brain MCP server
# ------------------------------------------------------------------
log "[5/8] Register blimp-brain MCP"
MCP_PY="$VENV/bin/python"
MCP_SCRIPT="$INFRA_DIR/mcp/blimp_brain_mcp.py"
if [ ! -x "$MCP_PY" ]; then
  err "duke-venv python not at $MCP_PY — run bootstrap-duke-memory.sh first"
  exit 1
fi
if [ ! -f "$MCP_SCRIPT" ]; then
  err "blimp-brain MCP source missing at $MCP_SCRIPT"; exit 1
fi
# Remove any prior registration to avoid duplicates, then add fresh
hermes mcp remove blimp-brain 2>/dev/null || true
hermes mcp add blimp-brain --command "$MCP_PY" --args "$MCP_SCRIPT" 2>&1 | head -3
if hermes mcp list 2>&1 | grep -q "^blimp-brain:.*Connected"; then
  ok "blimp-brain MCP connected"
else
  warn "blimp-brain MCP didn't immediately show Connected — check with 'hermes mcp list'"
fi

# ------------------------------------------------------------------
# 6) Discord — token, allowlist, channel routing, then adapter patch
# ------------------------------------------------------------------
log "[6/8] Discord wiring"
if [ -z "${DISCORD_BOT_TOKEN:-}" ]; then
  warn "DISCORD_BOT_TOKEN not in ~/.hermes/.env"
  echo "Paste the Hermes bot token from Discord Developer Portal:"
  read -r -s DISCORD_BOT_TOKEN; echo
  if [ -z "$DISCORD_BOT_TOKEN" ]; then
    err "no token entered, skipping Discord wiring"
  else
    _set_env_kv DISCORD_BOT_TOKEN "$DISCORD_BOT_TOKEN"
    ok "token saved to $ENV_FILE"
  fi
fi

# Allowlist + home channel — pull from existing env if there, else from defaults
_set_env_kv DISCORD_ALLOWED_USERS "${DISCORD_ALLOWED_USERS:-$DUKE_USER_ID}"
[ -n "${DISCORD_HOME_CHANNEL:-}" ] && _set_env_kv DISCORD_HOME_CHANNEL "$DISCORD_HOME_CHANNEL" || true
[ -n "${DISCORD_ALLOWED_CHANNELS:-}" ] && _set_env_kv DISCORD_ALLOWED_CHANNELS "$DISCORD_ALLOWED_CHANNELS" || true
[ -n "${DISCORD_FREE_RESPONSE_CHANNELS:-}" ] && _set_env_kv DISCORD_FREE_RESPONSE_CHANNELS "$DISCORD_FREE_RESPONSE_CHANNELS" || true
ok "discord env vars persisted"

# Apply the upstream-bug patch
log "    applying Discord adapter patch (works around Hermes config-loader bug)"
bash "$INFRA_DIR/patches/patch-hermes-discord-adapter.sh" 2>&1 | tail -15

# ------------------------------------------------------------------
# 7a) RE-VERIFY SOUL symlink — `hermes setup` / `hermes gateway install` are
#     known to clobber it. This is the audit fix from 2026-05-27.
# ------------------------------------------------------------------
log "[7/8] Final symlink + adapter patch (after any hermes init that may have clobbered them)"
if [ ! -L "$SOUL_DST" ] || [ "$(readlink -f "$SOUL_DST")" != "$(readlink -f "$SOUL_SRC")" ]; then
  warn "SOUL symlink drifted — re-linking"
  rm -f "$SOUL_DST"
  ln -sf "$SOUL_SRC" "$SOUL_DST"
fi
ok "SOUL symlink verified: $(readlink "$SOUL_DST")"

# 7b) Re-apply Discord adapter patch — `hermes update` (which `hermes gateway
#     install` can trigger) overwrites the patched adapter.
bash "$INFRA_DIR/patches/patch-hermes-discord-adapter.sh" 2>&1 | tail -10 || warn "patch re-apply errored — check manually"

# ------------------------------------------------------------------
# 7c) Install systemd user service. Note: we copy the repo unit file
#     AFTER hermes gateway install (which would otherwise overwrite it
#     with its default Restart=always + StartLimitIntervalSec=0 — the
#     exact config that caused the crash-loop on 2026-05-27).
# ------------------------------------------------------------------
log "[7c/8] Install repo systemd unit (overwrite Hermes's default if it landed)"
tmux kill-session -t hermes 2>/dev/null || true
pkill -f "hermes-agent/venv.*hermes" 2>/dev/null || true
sleep 2
mkdir -p "$HERMES_DIR/logs"

# Run the systemd installer (idempotent — copies repo unit, enables linger, restarts)
bash "$INFRA_DIR/systemd/install-systemd-units.sh" 2>&1 | tail -25

# Force-copy our unit AGAIN in case install-systemd-units.sh ran before any
# upstream installer dropped its own version
cp -f "$INFRA_DIR/systemd/hermes-gateway.service" "$HOME/.config/systemd/user/hermes-gateway.service"
systemctl --user daemon-reload
systemctl --user restart hermes-gateway

# ------------------------------------------------------------------
# 8) Verify
# ------------------------------------------------------------------
log "[8/8] Verifying"
DEADLINE=$(( $(date +%s) + 45 ))
SUCCESS=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if grep -qE "(Shard ID None has connected|discord adapter connected|Logged in as)" "$HERMES_DIR/logs/gateway.log" 2>/dev/null \
     || grep -qE "(Shard ID None has connected|discord adapter connected|Logged in as)" "$HERMES_DIR/logs/gateway-blimp.log" 2>/dev/null; then
    SUCCESS=1
    break
  fi
  sleep 2
done

if [ "$SUCCESS" -eq 1 ]; then
  ok "Hermes connected to Discord — should be green in your server"
else
  warn "Hermes didn't show a Discord-connected line in 45s — check 'systemctl --user status hermes-gateway' and ~/.hermes/logs/gateway.log"
fi

cat <<NEXT

============================================================
 Hermes is online.
============================================================

 Sessions:
   tmux ls         # should show: duke, hermes, other-agent
   tmux attach -t hermes
   (detach: Ctrl-b, d)

 Logs:
   tail -f ~/.hermes/logs/gateway-blimp.log

 Test:
   DM Hermes in Discord — try "recall what we know about pricing for SG med spas"
   He should call mcp__blimp-brain__recall and surface the team brain.

 Cross-agent:
   Have Hermes save a memory, then ask Duke to recall it.
   Both agents share the same Hindsight bank ("duke") and Postgres ("duke_data").

 If something looks off:
   bash $INFRA_DIR/patches/patch-hermes-discord-adapter.sh   # re-apply patch
   tail -50 ~/.hermes/logs/gateway-blimp.log
   hermes mcp list
   hermes config show | head -30

============================================================
NEXT
