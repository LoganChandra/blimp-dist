#!/usr/bin/env bash
# verify-all.sh — one-shot full verification of the voice agent.
# Exit codes: 0=ALL GOOD, 10=compile fail, 11=import fail, 12=agent not live,
#             13=e2e fail. Writes a human verdict to logs/verify-verdict.txt.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
V=logs/verify-verdict.txt
: > "$V"
say(){ echo "$@" | tee -a "$V"; }

say "=== VERIFY $(date) ==="

# 1) compile
if ./venv/bin/python -m py_compile agent.py e2e_probe.py token_server.py 2>>"$V"; then
  say "1) COMPILE: OK"
else
  say "1) COMPILE: FAIL"; exit 10
fi

# 2) import module top-level (catches bad imports / NameErrors without running main)
if ./venv/bin/python - <<'PY' 2>>"$V"
import importlib.util
spec=importlib.util.spec_from_file_location('agentmod','agent.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert hasattr(m,'BlimpAgent') and hasattr(m,'PersonaRouter')
print("import ok")
PY
then say "2) IMPORT: OK"; else say "2) IMPORT: FAIL"; exit 11; fi

# 3) boot agent, wait for LIVE
pkill -9 -f "[a]gent.py" 2>/dev/null || true
sleep 2
./venv/bin/python -u agent.py > logs/verify-agent.log 2>&1 &
AG=$!
LIVE=0
for i in $(seq 1 45); do
  grep -q "BLIMP VOICE AGENT LIVE" logs/verify-agent.log 2>/dev/null && { LIVE=1; break; }
  kill -0 "$AG" 2>/dev/null || { say "   agent died early"; break; }
  sleep 1
done
if [ "$LIVE" = "1" ]; then say "3) BOOT: LIVE after ${i}s"; else
  say "3) BOOT: FAIL — last lines:"; tail -20 logs/verify-agent.log | sed 's/^/     /' | tee -a "$V"
  kill "$AG" 2>/dev/null; exit 12
fi

# greet-on-join evidence (from the agent side; probe also measures it)
if grep -q "Greeted operator on join" logs/verify-agent.log; then say "   greet-on-join: FIRED"; else say "   greet-on-join: (not yet — probe will confirm)"; fi

# 4) e2e round-trip
./venv/bin/python -u e2e_probe.py > logs/verify-e2e.log 2>&1
PR=$?
RESULT=$(grep -oE "E2E_RESULT=[A-Z]+.*" logs/verify-e2e.log | tail -1)
say "4) E2E: $RESULT (probe_rc=$PR)"
# show key probe lines
grep -aE "frames,|reply bytes|reply latency|data messages|Subscribed to agent|Speaking|Greet" logs/verify-e2e.log | sed 's/^/     /' | tee -a "$V"

kill "$AG" 2>/dev/null || true
sleep 1
pkill -9 -f "[a]gent.py" 2>/dev/null || true

if echo "$RESULT" | grep -q "E2E_RESULT=PASS"; then
  say "=== VERDICT: ALL GOOD ==="
  exit 0
else
  say "=== VERDICT: E2E FAIL ==="
  exit 13
fi
