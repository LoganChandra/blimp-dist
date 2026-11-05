#!/usr/bin/env bash
# run.sh — start Frank Voice on http://127.0.0.1:8791 (Ctrl-C stops it).
# Nothing is spent while it sits idle: each spoken turn spawns one headless
# `claude -p` as Frank and exits when the reply is done.
set -euo pipefail
cd "$(dirname "$0")"
command -v claude >/dev/null || { echo "ERR: claude CLI not on PATH" >&2; exit 1; }
echo "Open http://127.0.0.1:${FRANK_VOICE_PORT:-8791} in Chrome or Edge, tap the orb, talk."
exec python3 -u server.py
