#!/usr/bin/env bash
# bootstrap-a2a.sh — one-shot A2A infrastructure bring-up.
#
# Prerequisites:
#   1. bootstrap-hermes.sh has been run (hermes installed, gateway running)
#   2. boot-text-blimp.sh has been run (duke in tmux)
#   3. bootstrap-duke-memory.sh has been run (Postgres + blimp-brain MCP)
#
# What this does:
#   1. Installs systemd units for a2a-broker + orchestrator
#   2. Enables Hermes webhook platform
#   3. Subscribes Hermes to a2a-task webhook
#   4. Starts the broker + orchestrator
#   5. Verifies everything is reachable
#
# Idempotent. Safe to re-run.

set -uo pipefail

log()  { printf '\n\033[1;36m[a2a-infra]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m  err\033[0m %s\n' "$*"; }

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"  # repo root, wherever it is checked out
INFRA_DIR="$REPO_ROOT/blimp-infra"
# Webhook bind: loopback by default (P0.2). Override with BIND_HOST only for a
# Tailscale-interface bind — never 0.0.0.0.
BIND_HOST="${BIND_HOST:-127.0.0.1}"
# Shared secret lives in the gitignored secrets file (created + rotated by
# sessions/session-2-security.sh). No hardcoded fallback — a default written
# into a committed script is a secret in git.
SECRET_FILE="$INFRA_DIR/secrets/a2a-secret.env"
if [ -z "${WEBHOOK_SECRET:-}" ] && [ -f "$SECRET_FILE" ]; then
  WEBHOOK_SECRET="$(sed -n 's/^BROKER_HERMES_SECRET=//p' "$SECRET_FILE" | head -1)"
fi
if [ -z "${WEBHOOK_SECRET:-}" ]; then
  err "No webhook secret. Run blimp-infra/sessions/session-2-security.sh first"
  err "(creates $SECRET_FILE), or export WEBHOOK_SECRET."
  exit 1
fi

# ──────────────────────────────────────────────────────────────────────
log "[1/5] Systemd units"
bash "$INFRA_DIR/systemd/install-systemd-units.sh"

# ──────────────────────────────────────────────────────────────────────
log "[2/5] Hermes webhook platform"
hermes config set platforms.webhook.enabled true 2>&1 | tail -1 || true
hermes config set platforms.webhook.extra.host "$BIND_HOST" 2>&1 | tail -1 || true
hermes config set platforms.webhook.extra.port 8644 2>&1 | tail -1 || true
hermes config set "platforms.webhook.extra.secret" "$WEBHOOK_SECRET" 2>&1 | tail -1 || true
ok "webhook platform enabled in config"

# ──────────────────────────────────────────────────────────────────────
log "[3/5] Hermes webhook subscription"
if hermes webhook list 2>/dev/null | grep -q "a2a-task"; then
  ok "a2a-task webhook already subscribed"
else
  hermes webhook subscribe a2a-task \
    --prompt "You have been notified of a new A2A task. Call mcp__blimp-brain__a2a_inbox with state='submitted' and limit=5 to check for pending tasks. For each task found, read the message, do the work required, and reply via mcp__blimp-brain__a2a_reply with state='completed'. Be concise and efficient." \
    --secret "$WEBHOOK_SECRET" 2>&1 | tail -5
  ok "webhook subscribed"
fi

# ──────────────────────────────────────────────────────────────────────
log "[4/5] Restart gateway (webhook takes effect on restart)"
systemctl --user restart hermes-gateway 2>/dev/null || warn "hermes-gateway not managed by systemd — restart manually"

# ──────────────────────────────────────────────────────────────────────
log "[5/5] Verification"
echo ""

# Wait for services to be up
sleep 5
echo "=== A2A Broker ==="
curl -s --max-time 3 http://localhost:9091/healthz 2>/dev/null && ok "broker healthy" || warn "broker not responding"

echo "=== Orchestrator ==="
curl -s --max-time 3 http://localhost:9092/healthz 2>/dev/null && ok "orchestrator healthy" || warn "orchestrator not responding"

echo "=== Hermes Webhook ==="
# Give gateway time to come back, then check webhook port
for i in 1 2 3 4 5; do
  if lsof -ti:8644 >/dev/null 2>&1; then
    ok "webhook port 8644 listening"
    break
  fi
  sleep 3
done
lsof -ti:8644 >/dev/null 2>&1 || warn "webhook port 8644 not listening — ensure gateway restarted"

echo "=== Duke tmux ==="
tmux ls 2>&1 | grep -q "duke" && ok "duke tmux running" || warn "duke not in tmux — run boot-text-blimp.sh"

cat <<NEXT

============================================================
 A2A infrastructure ready.
============================================================

 Test it:
   curl -X POST http://localhost:9092/orchestrate \\
     -H "Content-Type: application/json" \\
     -d '{"task":"Design a simple health check endpoint.","max_turns":2,"usd_budget":0.50}'

   # Poll result (replace <thread_id> with the returned ID):
   curl http://localhost:9092/orchestrate/<thread_id>

 Services:
   systemctl --user status a2a-broker orchestrator hermes-gateway

 Logs:
   tail -f ~/.hermes/logs/a2a-broker.log
   tail -f ~/.hermes/logs/orchestrator.log

NOTE: The webhook subscription is an ephemeral Hermes state (not in git).
Run 'hermes webhook subscribe a2a-task ...' after bare-metal restores.

============================================================
NEXT