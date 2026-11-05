#!/usr/bin/env bash
# run-e2e.sh — offline server tests, then the real browser E2E against real Frank.
# The E2E spends a few real model turns (~4 short ones). Brain logging is OFF
# here so test chatter never lands in the live shared brain.
# Needs: playwright-core on NODE_PATH (npm i playwright-core in any dir) + a Chromium.
set -uo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8792}"
python3 test_server.py || exit 1
FRANK_VOICE_PORT=$PORT FRANK_VOICE_BRAIN_LOG=0 FRANK_VOICE_STATE="$(mktemp)" python3 -u server.py >/tmp/frank-voice-e2e.log 2>&1 &
SP=$!; trap 'kill $SP 2>/dev/null' EXIT
sleep 1.5
URL="http://127.0.0.1:$PORT" node e2e_browser.js
