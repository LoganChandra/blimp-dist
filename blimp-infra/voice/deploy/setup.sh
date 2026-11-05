#!/usr/bin/env bash
# ── setup.sh — Bootstrap the Blimp Voice MVP on a fresh SG VM ────────────────
#
# Run this ONCE on the DigitalOcean SGP1 droplet after initial SSH:
#   curl -fsSL https://raw.githubusercontent.com/<your-github-user>/blimp-repo/main/blimp-infra/voice/deploy/setup.sh | bash
#
# Or copy the deploy/ directory and run:
#   sudo ./setup.sh
#
# What it does:
#   1. Installs system packages (python3, redis, caddy, etc.)
#   2. Creates /opt/blimp/voice/ with configs + code
#   3. Downloads LiveKit SFU binary from GitHub releases
#   4. Creates Python venv + installs agent + token-server deps
#   5. Writes systemd service files
#   6. Configures Caddy for automatic TLS
#   7. Enables + starts all services in order:
#      livekit-server → livekit-token-server → livekit-agents
#
# Assumptions:
#   - Ubuntu 24.04 LTS (or similar Debian-based)
#   - Systemd available
#   - DNS record pointing to this droplet (for Caddy TLS)
#
# Log: /var/log/blimp-voice-setup.log
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

LOG_FILE="/var/log/blimp-voice-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { printf '\n\033[1;36m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  warn\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m  err\033[0m %s\n' "$*"; exit 1; }

# ── Configuration ──────────────────────────────────────────────────────────────

BLIMP_DIR="/opt/blimp"
VOICE_DIR="$BLIMP_DIR/voice"
REPO_BRANCH="${REPO_BRANCH:-main}"
LIVEKIT_VERSION="${LIVEKIT_VERSION:-1.8.5}"           # pin latest stable
CADDY_VERSION="${CADDY_VERSION:-2.8.4}"
DOMAIN="${DOMAIN:-voice.blimp.sh}"                     # CHANGE ME

# ── Step 0: Root check ────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
  err "This script must be run as root (sudo)."
fi

log "[0/7] System info"
hostnamectl
ok "running as root"

# ── Step 1: System packages ───────────────────────────────────────────────────

log "[1/7] Installing system packages"
apt-get update -qq
apt-get install -y -qq \
  curl wget gnupg \
  python3 python3-pip python3-venv \
  redis-server \
  uidmap \
  git

# Install Caddy
if ! command -v caddy &>/dev/null; then
  apt-get install -y -qq debian-keyring debian-archive-keyring 2>/dev/null || true
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

ok "system packages installed"

# ── Step 2: Create directory structure ────────────────────────────────────────

log "[2/7] Creating directory structure"
mkdir -p "$VOICE_DIR"/{deploy,web,venv,logs}
ok "directories created at $VOICE_DIR"

# ── Step 3: Download LiveKit SFU binary ───────────────────────────────────────

log "[3/7] Downloading LiveKit SFU v$LIVEKIT_VERSION"
LIVEKIT_BIN="/usr/local/bin/livekit-server"

if [ ! -f "$LIVEKIT_BIN" ]; then
  LK_URL="https://github.com/livekit/livekit/releases/download/v${LIVEKIT_VERSION}/livekit_${LIVEKIT_VERSION}_linux_amd64.tar.gz"
  curl -fsSL "$LK_URL" -o /tmp/livekit.tar.gz
  tar -xzf /tmp/livekit.tar.gz -C /tmp/
  mv /tmp/livekit-server "$LIVEKIT_BIN"
  chmod +x "$LIVEKIT_BIN"
  rm -f /tmp/livekit.tar.gz /tmp/livekit-server
  ok "LiveKit SFU v$LIVEKIT_VERSION installed"
else
  ok "livekit-server already present"
fi

# Verify
livekit-server --version || warn "livekit-server version check failed"

# ── Step 4: Copy deployment files ─────────────────────────────────────────────

log "[4/7] Deploying voice files"
# NOTE: The repo files should already be copied or you're running from the repo.
# If running from the repo checkout, these are already in place.
if [ -d "$(dirname "$0")/../web" ]; then
  SRC="$(cd "$(dirname "$0")/.." && pwd)"
  log "Copying files from $SRC to $VOICE_DIR"
  cp "$SRC"/agent.py "$VOICE_DIR/" 2>/dev/null || warn "agent.py not found at src"
  cp "$SRC"/token_server.py "$VOICE_DIR/" 2>/dev/null || warn "token_server.py not found at src"
  cp "$SRC"/requirements.txt "$VOICE_DIR/" 2>/dev/null || warn "requirements.txt not found at src"
  cp "$SRC"/.env.example "$VOICE_DIR/.env" 2>/dev/null || warn ".env not copied"
  cp -r "$SRC"/web "$VOICE_DIR/" 2>/dev/null || warn "web/ not copied"
  chown -R root:root "$VOICE_DIR"
  ok "files deployed"
else
  log "Running from within the repo at $VOICE_DIR — assuming files are already in place."
fi

# Copy livekit.yaml if present
if [ -f "$(dirname "$0")/livekit.yaml" ]; then
  cp "$(dirname "$0")/livekit.yaml" "$VOICE_DIR/livekit.yaml"
  ok "livekit.yaml deployed"
fi

# ── Step 5: Python virtual environment ────────────────────────────────────────

log "[5/7] Setting up Python virtual environment"
python3 -m venv "$VOICE_DIR/venv"
source "$VOICE_DIR/venv/bin/activate"

if [ -f "$VOICE_DIR/requirements.txt" ]; then
  pip install --quiet --upgrade pip setuptools wheel
  pip install --quiet -r "$VOICE_DIR/requirements.txt"
  ok "Python deps installed"
else
  warn "requirements.txt not found — install deps manually"
fi

deactivate

# ── Step 6: Systemd service files ─────────────────────────────────────────────

log "[6/7] Installing systemd service files"

# 6a) livekit-server.service
cat > /etc/systemd/system/livekit-server.service << 'SERVICE'
[Unit]
Description=LiveKit SFU Server (Blimp Voice)
After=network-online.target redis-server.service
Wants=network-online.target
Requires=redis-server.service

[Service]
Type=simple
ExecStart=/usr/local/bin/livekit-server --config /opt/blimp/voice/livekit.yaml
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
LimitNPROC=infinity
StandardOutput=append:/opt/blimp/voice/logs/livekit-server.log
StandardError=append:/opt/blimp/voice/logs/livekit-server.log

[Install]
WantedBy=multi-user.target
SERVICE

# 6b) livekit-token-server.service
cat > /etc/systemd/system/livekit-token-server.service << 'SERVICE'
[Unit]
Description=LiveKit Token Server + Mic UI (Blimp Voice)
After=livekit-server.service
Requires=livekit-server.service

[Service]
Type=simple
EnvironmentFile=/opt/blimp/voice/.env
WorkingDirectory=/opt/blimp/voice
ExecStart=/opt/blimp/voice/venv/bin/uvicorn token_server:app --host 127.0.0.1 --port 8790
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/blimp/voice/logs/token-server.log
StandardError=append:/opt/blimp/voice/logs/token-server.log

[Install]
WantedBy=multi-user.target
SERVICE

# 6c) livekit-agents.service
cat > /etc/systemd/system/livekit-agents.service << 'SERVICE'
[Unit]
Description=LiveKit Agents Voice Worker (Blimp Voice)
After=livekit-server.service livekit-token-server.service
Requires=livekit-server.service

[Service]
Type=simple
EnvironmentFile=/opt/blimp/voice/.env
WorkingDirectory=/opt/blimp/voice
ExecStart=/opt/blimp/voice/venv/bin/python agent.py
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/blimp/voice/logs/agents.log
StandardError=append:/opt/blimp/voice/logs/agents.log

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
ok "systemd service files installed"

# ── Step 7: Caddy TLS proxy ───────────────────────────────────────────────────

log "[7/7] Configuring Caddy reverse proxy"

cat > /etc/caddy/Caddyfile << CADDY
# Blimp Voice — Caddy config for automatic TLS.
# Replace voice.blimp.sh with your actual domain.
# DNS must point to this droplet's IP before Caddy will provision certs.

voice.blimp.sh {
    # Reverse proxy to the token server (serves mic UI + /token endpoint)
    reverse_proxy localhost:8790

    # WebSocket support for LiveKit (if browser connects via wss://)
    @lk-ws {
        header Connection *Upgrade*
        header Upgrade    websocket
    }
    reverse_proxy @lk-ws localhost:7880

    # Security headers
    header {
        X-Content-Type-Options  nosniff
        X-Frame-Options         DENY
        Referrer-Policy         strict-origin-when-cross-origin
    }
}

# LiveKit WebRTC endpoint — direct wss:// connection for the browser
# The browser uses wss://voice.blimp.sh:443 for WebRTC (Caddy proxies it)
CADDY

# Replace placeholder domain — if $DOMAIN is set, use it
if [ -n "${DOMAIN:-}" ] && [ "$DOMAIN" != "voice.blimp.sh" ]; then
  sed -i "s/voice\.blimp\.sh/$DOMAIN/g" /etc/caddy/Caddyfile
  ok "Caddy configured for $DOMAIN"
else
  warn "Using default domain voice.blimp.sh — UPDATE /etc/caddy/Caddyfile with your real domain!"
fi

systemctl enable --now caddy 2>/dev/null || systemctl restart caddy
ok "Caddy configured and restarted"

# ── Enable + start services ───────────────────────────────────────────────────

log "[final] Enabling and starting services"

systemctl enable redis-server
systemctl start redis-server
ok "redis-server running"

systemctl enable livekit-server
systemctl start livekit-server
sleep 3
if systemctl is-active --quiet livekit-server; then
  ok "livekit-server is ACTIVE"
else
  warn "livekit-server not active — check: journalctl -u livekit-server -n 30"
fi

systemctl enable livekit-token-server
systemctl start livekit-token-server
sleep 2

systemctl enable livekit-agents
systemctl start livekit-agents
sleep 2

# Final status check
for svc in redis-server livekit-server livekit-token-server livekit-agents caddy; do
  if systemctl is-active --quiet "$svc"; then
    ok "$svc is ACTIVE"
  else
    warn "$svc is NOT active — check: journalctl -u $svc -n 20"
  fi
done

# ── Done ──────────────────────────────────────────────────────────────────────

DOMAIN_FINAL="${DOMAIN:-voice.blimp.sh}"
cat <<SUMMARY

╔══════════════════════════════════════════════════════════════════╗
║                Blimp Voice MVP — Setup Complete                 ║
╚══════════════════════════════════════════════════════════════════╝

  Web UI:    https://$DOMAIN_FINAL
  Token API: https://$DOMAIN_FINAL/token
  Health:    https://$DOMAIN_FINAL/healthz

  LiveKit internal:      ws://localhost:7880
  LiveKit browser:       wss://$DOMAIN_FINAL

  Config directory:      $VOICE_DIR
  Logs:                  $VOICE_DIR/logs/

NEXT STEPS:
  1. Edit /opt/blimp/voice/.env  →  fill in API keys (LIVEKIT_API_KEY,
     LIVEKIT_API_SECRET, DEEPGRAM_API_KEY, ANTHROPIC_API_KEY, CARTESIA_API_KEY)
  2. Edit /opt/blimp/voice/livekit.yaml  →  set real API keys
  3. Restart services after editing .env:
       systemctl restart livekit-token-server livekit-agents

  4. For your DigitalOcean droplet firewall, open:
       TCP 443   (HTTPS — Caddy)
       UDP 50000-60000 (WebRTC media)

  Troubleshooting:
    journalctl -u livekit-server -n 50 --no-pager
    journalctl -u livekit-agents -n 50 --no-pager
    journalctl -u livekit-token-server -n 50 --no-pager
    tail -f $VOICE_DIR/logs/*.log

  To update the agent code after a git pull:
    cp /path/to/agent.py $VOICE_DIR/agent.py
    systemctl restart livekit-agents

SUMMARY