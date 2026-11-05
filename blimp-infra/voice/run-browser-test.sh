#!/usr/bin/env bash
# run-browser-test.sh — full in-browser verification of the voice UI.
# Starts agent + token server, drives the page in real headless Chromium,
# reports whether the agent's audio actually PLAYS in a browser.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs

CHROME="${CHROME:-$HOME/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome}"
PWNODE="${PWNODE:-/tmp/pwtest/node_modules}"   # where playwright-core is installed
export NODE_PATH="$PWNODE"

echo "=== BROWSER TEST $(date) ==="
[ -x "$CHROME" ] || { echo "FAIL: chrome not found at $CHROME"; exit 2; }

pkill -9 -f "[a]gent.py" 2>/dev/null || true
pkill -9 -f "token_server" 2>/dev/null || true
sleep 2

# 0) speech wav for the fake mic (best-effort; greeting still tests playback)
if ./venv/bin/python gen_test_wav.py > logs/bt-genwav.log 2>&1; then
  WAV="$(pwd)/test_speech.wav"; echo "WAV: $WAV"
else
  WAV=""; echo "WAV: (none — gen failed, greeting still tests playback)"; tail -3 logs/bt-genwav.log
fi

# 1) agent
./venv/bin/python -u agent.py > logs/bt-agent.log 2>&1 &
AG=$!
LIVE=0
for i in $(seq 1 45); do
  grep -q "BLIMP VOICE AGENT LIVE" logs/bt-agent.log 2>/dev/null && { LIVE=1; break; }
  kill -0 "$AG" 2>/dev/null || break
  sleep 1
done
[ "$LIVE" = 1 ] && echo "agent: LIVE after ${i}s" || { echo "FAIL: agent not live"; tail -20 logs/bt-agent.log; kill "$AG" 2>/dev/null; exit 1; }

# 2) token server (serves the page + /token)
./venv/bin/uvicorn token_server:app --host 127.0.0.1 --port 8790 > logs/bt-token.log 2>&1 &
TK=$!
UP=0
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8790/healthz >/dev/null 2>&1 && { UP=1; break; }; sleep 0.5; done
[ "$UP" = 1 ] && echo "token server: UP" || echo "WARN: token server health not confirmed"

# 3) browser
CHROME="$CHROME" URL="http://127.0.0.1:8790" WAV="$WAV" node browser_test.js > logs/bt-browser.log 2>&1
RC=$?
cat logs/bt-browser.log

# teardown
kill "$AG" "$TK" 2>/dev/null || true
sleep 1
pkill -9 -f "[a]gent.py" 2>/dev/null || true
pkill -9 -f "token_server" 2>/dev/null || true

V=$(grep -oE "BROWSER_VERDICT=[A-Z_]+" logs/bt-browser.log | tail -1)
echo "RESULT: ${V:-UNKNOWN} (node_rc=$RC)"
echo "=== done ==="
