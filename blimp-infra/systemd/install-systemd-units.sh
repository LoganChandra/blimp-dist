#!/usr/bin/env bash
# install-systemd-units.sh — install Blimp systemd user services.
#
# Currently installs:
#   - hermes-gateway.service (Hermes Agent gateway, auto-restart on failure)
#   - a2a-broker.service      (A2A agent-to-agent message routing)
#   - orchestrator.service    (LangGraph supervisor for Hermes+Duke)
#
# VOICE INFRASTRUCTURE SERVICES (livekit-server, livekit-token-server,
# livekit-agents) are NOT installed here — they run on the Singapore DigitalOcean
# droplet and are deployed via blimp-infra/voice/deploy/setup.sh. The .service
# files in this directory are reference copies for documentation.
#

set -uo pipefail

log()  { printf '\n\033[1;36m[systemd]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m  err\033[0m %s\n' "$*"; }

HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$HOME/.hermes/logs"

# ------------------------------------------------------------------
# 1) systemd availability check
# ------------------------------------------------------------------
log "[1/4] systemd check"
if ! command -v systemctl >/dev/null 2>&1; then
  err "systemctl not found. Ensure WSL2 has systemd enabled."
  err "Check /etc/wsl.conf has:"
  err "  [boot]"
  err "  systemd=true"
  err "Then run: wsl --shutdown (from PowerShell) and reopen WSL."
  exit 1
fi
ok "systemctl present"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  err "systemd --user session not available. Enable systemd in /etc/wsl.conf and restart WSL."
  exit 1
fi
ok "systemd --user session active"

# ------------------------------------------------------------------
# 2) Enable linger so services keep running when no shell is open
# ------------------------------------------------------------------
log "[2/4] linger (so services survive shell exit + WSL idle)"
if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
  ok "linger already enabled for $USER"
else
  if sudo -n true 2>/dev/null; then
    sudo loginctl enable-linger "$USER" && ok "linger enabled" || warn "linger enable failed"
  else
    warn "sudo password required — run this once manually:"
    warn "  sudo loginctl enable-linger $USER"
  fi
fi

# ------------------------------------------------------------------
# 3) Install unit files
# ------------------------------------------------------------------
log "[3/4] Installing unit files"
for src in "$HERE"/*.service; do
  [ -f "$src" ] || continue
  name="$(basename "$src")"
  dst="$UNIT_DIR/$name"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    ok "$name already up-to-date"
  else
    cp "$src" "$dst"
    ok "installed $name"
  fi
done

# ------------------------------------------------------------------
# 4) Reload + enable + start
# ------------------------------------------------------------------
log "[4/4] Enable + start"
systemctl --user daemon-reload
ok "daemon reloaded"

for svc in hermes-gateway a2a-broker orchestrator; do
  systemctl --user enable "${svc}.service" 2>&1 | tail -3
  systemctl --user restart "${svc}.service"
  sleep 3
  if systemctl --user is-active --quiet "${svc}.service"; then
    ok "$svc is ACTIVE"
  else
    warn "$svc failed to start — check 'systemctl --user status ${svc}.service' + ~/.hermes/logs/gateway.log"
  fi
done

cat <<NEXT

============================================================
 systemd services installed.
============================================================

  Useful commands:
    systemctl --user status hermes-gateway     # current state
    systemctl --user restart hermes-gateway    # restart
    systemctl --user stop hermes-gateway       # stop
    journalctl --user -u hermes-gateway -f     # follow logs (systemd side)
    tail -f ~/.hermes/logs/gateway.log          # follow logs (app side)

  Linger status:
    loginctl show-user $USER | grep Linger

  Disable a service (won't auto-start anymore):
    systemctl --user disable hermes-gateway

============================================================
NEXT
