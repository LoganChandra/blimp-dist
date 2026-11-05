#!/usr/bin/env bash
# run-e2e.sh — orchestrate the end-to-end voice test.
# Starts agent.py, waits for LIVE, runs e2e_probe.py, tears down, prints VERDICT.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs

echo "=== E2E START $(date) ==="
pkill -9 -f "[a]gent.py" 2>/dev/null || true
sleep 2

echo "--- starting agent.py ---"
./venv/bin/python -u agent.py > logs/e2e-agent.log 2>&1 &
AGENT_PID=$!

LIVE=0
for i in $(seq 1 45); do
  if grep -q "BLIMP VOICE AGENT LIVE" logs/e2e-agent.log 2>/dev/null; then LIVE=1; break; fi
  if ! kill -0 "$AGENT_PID" 2>/dev/null; then echo "AGENT DIED EARLY"; break; fi
  sleep 1
done
echo "--- agent LIVE=$LIVE after ${i}s ---"

if [ "$LIVE" != "1" ]; then
  echo "=== AGENT FAILED TO GO LIVE — last 30 log lines ==="
  tail -30 logs/e2e-agent.log
  kill "$AGENT_PID" 2>/dev/null || true
  echo "E2E_VERDICT=FAIL_AGENT_NOT_LIVE"
  exit 1
fi

echo "--- running e2e_probe.py ---"
./venv/bin/python -u e2e_probe.py > logs/e2e-probe.log 2>&1
PROBE_RC=$?
echo "--- probe exit=$PROBE_RC ---"

echo "=== PROBE LOG ==="
cat logs/e2e-probe.log

echo "=== AGENT LOG (turn/response evidence) ==="
grep -nE "Turn |Persona|speaking|generate|reply|response|error|Error|Traceback|ListTools|MCP|user_|agent_state" logs/e2e-agent.log | tail -40

echo "--- tearing down agent ---"
kill "$AGENT_PID" 2>/dev/null || true
sleep 2
pkill -9 -f "[a]gent.py" 2>/dev/null || true

RESULT_LINE=$(grep -oE "E2E_RESULT=[A-Z]+.*" logs/e2e-probe.log | tail -1)
echo "E2E_VERDICT=${RESULT_LINE:-UNKNOWN} (probe_rc=$PROBE_RC)"
echo "=== E2E DONE $(date) ==="
