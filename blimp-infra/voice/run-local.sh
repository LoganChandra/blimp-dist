#!/usr/bin/env bash
# run-local.sh — launch the Blimp voice stack locally (WSL dev).
#
# Brings up BOTH processes the voice room needs and ties their lifetimes
# together (Ctrl-C stops both):
#   1. token server + mic UI  -> http://127.0.0.1:8790
#   2. agent worker           -> joins the LiveKit Cloud room and listens
#
# Then open http://127.0.0.1:8790 in a browser, click Connect, allow the mic,
# and talk. Say "hey hermes ..." / "duke ..." to switch personas.
#
# The droplet path (systemd + self-hosted SFU) is separate — see deploy/setup.sh.

set -uo pipefail
cd "$(dirname "$0")"

if [ ! -x ./venv/bin/python ]; then
  echo "ERR: ./venv not found. Create it: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [ ! -f ./.env ]; then
  echo "ERR: ./.env not found. Copy .env.example to .env and fill in keys." >&2
  exit 1
fi

PORT="${TOKEN_SERVER_PORT:-8790}"
mkdir -p logs

cleanup() {
  echo
  echo "[run-local] shutting down…"
  [ -n "${TOKEN_PID:-}" ] && kill "$TOKEN_PID" 2>/dev/null
  [ -n "${AGENT_PID:-}" ] && kill "$AGENT_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup INT TERM EXIT

echo "[run-local] starting token server on http://127.0.0.1:${PORT} …"
./venv/bin/uvicorn token_server:app --host 127.0.0.1 --port "$PORT" > logs/token-server.log 2>&1 &
TOKEN_PID=$!

echo "[run-local] starting agent worker (connecting to LiveKit Cloud) …"
./venv/bin/python -u agent.py > logs/agents.log 2>&1 &
AGENT_PID=$!

sleep 6
if grep -q "BLIMP VOICE AGENT LIVE" logs/agents.log 2>/dev/null; then
  echo "[run-local] ✅ agent is live and listening."
else
  echo "[run-local] ⚠️  agent not confirmed live yet — tail logs/agents.log"
fi

cat <<EOF

  ┌──────────────────────────────────────────────┐
  │  Open:  http://127.0.0.1:${PORT}                  │
  │  Click Connect → allow mic → talk.             │
  │  Ctrl-C here stops both processes.             │
  └──────────────────────────────────────────────┘

  Logs:  logs/token-server.log   logs/agents.log
EOF

# Keep running until either process dies or Ctrl-C.
wait -n "$TOKEN_PID" "$AGENT_PID"
