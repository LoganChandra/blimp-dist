#!/usr/bin/env bash
# fix-hermes-discord.sh — diagnose + fix Hermes's Discord adapter token loading.
#
# Hermes's plugin reads `self.config.token`, not `os.getenv("DISCORD_BOT_TOKEN")`.
# Something in the gateway config loader is supposed to bridge env→config but
# isn't. This script:
#   1. Traces where the loader actually reads from
#   2. Tries the candidate fix locations in order
#   3. Restarts gateway after each, tests if Discord connects
#   4. Reports which fix worked
#
# Safe to re-run. Idempotent.

set -uo pipefail

HERMES_DIR="$HOME/.hermes"
HERMES_REPO="$HERMES_DIR/hermes-agent"
LOG="$HERMES_DIR/logs/gateway-blimp.log"
REPORT="$HOME/blimp/hermes-discord-fix-report.txt"
mkdir -p "$(dirname "$REPORT")"

# Reset report file
: > "$REPORT"

log() {
  printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$REPORT"
}
ok()   { printf '\033[1;32m  ok\033[0m %s\n'    "$*" | tee -a "$REPORT"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*" | tee -a "$REPORT"; }
err()  { printf '\033[1;31m  err\033[0m %s\n'  "$*" | tee -a "$REPORT"; }

# ------------------------------------------------------------------
# Step 1 — preflight: env loads, token valid via REST, network reaches Discord
# ------------------------------------------------------------------
log "Step 1: preflight"

set -a
source "$HERMES_DIR/.env" 2>/dev/null || true
set +a

if [ -z "${DISCORD_BOT_TOKEN:-}" ]; then
  err "DISCORD_BOT_TOKEN missing from ~/.hermes/.env — bail"
  exit 1
fi
ok "DISCORD_BOT_TOKEN present (${#DISCORD_BOT_TOKEN} chars)"

REST_RESP=$(curl -s --max-time 10 -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
  https://discord.com/api/v10/users/@me)
if echo "$REST_RESP" | grep -q '"id"'; then
  BOT_ID=$(echo "$REST_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  ok "Discord REST auth OK (bot id $BOT_ID)"
else
  err "Discord REST auth FAILED: $REST_RESP"
  exit 1
fi

# ------------------------------------------------------------------
# Step 2 — find where the gateway config loader reads the discord token
# ------------------------------------------------------------------
log "Step 2: trace config loader"

# Find the gateway's platform-config loader
LOADER_FILES=$(grep -rln -E "(discord.*token|DISCORD_BOT_TOKEN)" \
  "$HERMES_REPO/gateway/" "$HERMES_REPO/hermes_cli/" 2>/dev/null | head -10)

if [ -z "$LOADER_FILES" ]; then
  warn "no obvious config-loader file in gateway/ or hermes_cli/"
  # broaden the search
  LOADER_FILES=$(grep -rln -E "platform.*config.*discord" "$HERMES_REPO" 2>/dev/null | head -5)
fi

echo "$LOADER_FILES" | tee -a "$REPORT"
echo "" | tee -a "$REPORT"

# Inspect each candidate for env-reading patterns
for f in $LOADER_FILES; do
  echo "--- $f ---" | tee -a "$REPORT"
  grep -n -E "(os\.environ|getenv|DISCORD_BOT_TOKEN|discord.*token|self\.token)" "$f" 2>/dev/null \
    | head -20 | tee -a "$REPORT"
  echo "" | tee -a "$REPORT"
done

# ------------------------------------------------------------------
# Step 3 — show current config.yaml structure for the discord section
# ------------------------------------------------------------------
log "Step 3: current config.yaml discord section"

python3 - <<'PY' | tee -a "$REPORT"
import yaml, sys, os, pprint
try:
    with open(os.path.expanduser("~/.hermes/config.yaml")) as f:
        cfg = yaml.safe_load(f)
except Exception as e:
    print(f"  err: {e}")
    sys.exit(0)

# Dump every key that mentions discord or platforms or messaging
def walk(d, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{path}.{k}" if path else k
            if any(s in p.lower() for s in ("discord", "platform", "messaging", "gateway", "token")):
                if isinstance(v, (dict, list)):
                    print(f"  {p}:")
                    walk(v, p)
                else:
                    val = "(none)" if v in (None, "", False) else "(set)" if "token" in p.lower() else repr(v)
                    print(f"  {p}: {val}")
            elif isinstance(v, (dict, list)):
                walk(v, p)

walk(cfg)
PY

# ------------------------------------------------------------------
# Step 4 — try the three candidate fixes, test each
# ------------------------------------------------------------------

restart_gateway() {
  tmux kill-session -t hermes 2>/dev/null || true
  pkill -f "hermes-agent/venv.*hermes" 2>/dev/null || true
  sleep 2
  tmux new -d -s hermes 'export PATH="$HOME/.local/bin:$PATH"; set -a; source ~/.hermes/.env; set +a; hermes gateway 2>&1 | tee -a ~/.hermes/logs/gateway-blimp.log'
  sleep 15
}

check_discord_connected() {
  # Look for last 60 lines of log; success markers vary by hermes version
  local out
  out=$(tail -60 "$LOG" 2>/dev/null)
  if echo "$out" | grep -qE "(connected to Gateway|discord.*ready|discord adapter connected|Logged in as)"; then
    return 0
  fi
  return 1
}

last_error() {
  tail -40 "$LOG" 2>/dev/null | grep -E "(ERROR|✗|failed|timed out)" | tail -3
}

# Truncate log so we measure THIS attempt only
: > "$LOG"

# ATTEMPT A — set discord.token in config.yaml
log "Step 4A: set discord.token in config.yaml"
hermes config set discord.token "$DISCORD_BOT_TOKEN" 2>&1 | head -3 | tee -a "$REPORT"
restart_gateway
if check_discord_connected; then
  ok "SUCCESS — discord.token in config.yaml fixed it"
  log "DONE"; exit 0
else
  warn "attempt A failed: $(last_error)"
fi

: > "$LOG"

# ATTEMPT B — set messaging.discord.token (alt naming)
log "Step 4B: set messaging.discord.token"
hermes config set messaging.discord.token "$DISCORD_BOT_TOKEN" 2>&1 | head -3 | tee -a "$REPORT"
restart_gateway
if check_discord_connected; then
  ok "SUCCESS — messaging.discord.token fixed it"
  log "DONE"; exit 0
else
  warn "attempt B failed: $(last_error)"
fi

: > "$LOG"

# ATTEMPT C — set platforms.discord.token
log "Step 4C: set platforms.discord.token"
hermes config set platforms.discord.token "$DISCORD_BOT_TOKEN" 2>&1 | head -3 | tee -a "$REPORT"
restart_gateway
if check_discord_connected; then
  ok "SUCCESS — platforms.discord.token fixed it"
  log "DONE"; exit 0
else
  warn "attempt C failed: $(last_error)"
fi

# ATTEMPT D — try `hermes setup gateway` style (non-interactive: env-only fallback)
log "Step 4D: explicitly export DISCORD_BOT_TOKEN before launching"
: > "$LOG"
tmux kill-session -t hermes 2>/dev/null || true
pkill -f "hermes-agent/venv.*hermes" 2>/dev/null || true
sleep 2
# Try launching with explicit env (not source-then-launch, but env-prefix)
tmux new -d -s hermes "DISCORD_BOT_TOKEN='$DISCORD_BOT_TOKEN' DISCORD_ALLOWED_USERS='${DISCORD_ALLOWED_USERS:-}' DISCORD_HOME_CHANNEL='${DISCORD_HOME_CHANNEL:-}' hermes gateway 2>&1 | tee -a $LOG"
sleep 15
if check_discord_connected; then
  ok "SUCCESS — env-prefix launch fixed it"
  log "DONE"; exit 0
else
  warn "attempt D failed: $(last_error)"
fi

# ------------------------------------------------------------------
# All attempts failed — capture detailed diagnostic for human review
# ------------------------------------------------------------------
log "ALL ATTEMPTS FAILED — capturing full diagnostic"

{
  echo ""
  echo "=== FULL recent gateway log ==="
  tail -100 "$LOG"
  echo ""
  echo "=== hermes config show (discord-related) ==="
  hermes config show 2>&1 | grep -A 2 -iE "(discord|token|messaging|platform)" | head -40
  echo ""
  echo "=== Adapter init code (lines 600-640) ==="
  sed -n '600,640p' "$HERMES_REPO/plugins/platforms/discord/adapter.py" 2>/dev/null
  echo ""
  echo "=== Where the config object gets `token` field ==="
  grep -rn -E "config\.token|self\._token|\.token =" "$HERMES_REPO/plugins/platforms/discord/" 2>/dev/null | head -20
} | tee -a "$REPORT"

err "All four candidate fixes failed. See $REPORT for full diagnostic."
exit 2
