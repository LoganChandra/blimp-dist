#!/usr/bin/env bash
# gate.sh [base]: the FREE merge gate for any change to Blimp's agents (see .claude/skills/self-evolve).
# Zero tokens: unit tests, syntax, the memory exam if brain code changed, and the list of behaviour
# cases the diff touches (run those yourself with evals/pass-k.sh; they cost tokens).
# Exit 0 = green.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$REPO"
BASE="${1:-main}"
PYT="$HOME/blimp/mesh-venv/bin/pytest"
fail=0; step() { printf '\n== %s\n' "$*"; }
mapfile -t CHANGED < <({ git diff --name-only "$BASE"...HEAD; git diff --name-only; git ls-files -o --exclude-standard; } | sort -u)
echo "changed vs $BASE: ${#CHANGED[@]} file(s)"

step "syntax"
for f in "${CHANGED[@]}"; do
  [ -f "$f" ] || continue
  case "$f" in
    *.sh) bash -n "$f" || { echo "  bash syntax: $f"; fail=1; } ;;
    *.py) python3 -m py_compile "$f" 2>/dev/null || { echo "  python syntax: $f"; fail=1; } ;;
    *.json) python3 -m json.tool "$f" >/dev/null || { echo "  json: $f"; fail=1; } ;;
    *.yaml|*.yml) "$HOME/blimp/mesh-venv/bin/python" -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]))" "$f" || { echo "  yaml: $f"; fail=1; } ;;
  esac
done
echo "  ok"

step "unit tests (hooks, agent, mcp)"
$PYT -q blimp-infra/hooks blimp-infra/agent blimp-infra/mcp 2>&1 | tail -1
[ "${PIPESTATUS[0]}" -eq 0 ] || fail=1

if printf '%s\n' "${CHANGED[@]}" | grep -qE '^blimp/mesh/'; then
  step "mesh tests"
  $PYT -q blimp/mesh 2>&1 | tail -1; [ "${PIPESTATUS[0]}" -eq 0 ] || fail=1
fi

step "eval cases parse"
bash evals/run-evals.sh --dry-run >/dev/null 2>&1 && echo "  ok" || { echo "  run-evals --dry-run failed"; fail=1; }

if printf '%s\n' "${CHANGED[@]}" | grep -qE '^blimp-infra/(postgres|mcp|brain-eval|brain-maint)/|^blimp-infra/hooks/.*capture'; then
  step "memory exam (brain code changed): run it on a clone and compare per type with the last summary"
  echo "  cd blimp-infra/brain-eval/exam && DUMP=~/.cache/blimp-exam/8899/brain.dump bash clone-brain.sh 8897 && python3 exam.py run http://127.0.0.1:8897 --tag <change>"
  echo "  (not run automatically: it needs a clone container; it costs no tokens)"
fi

step "behaviour cases this diff touches (tokens: run on purpose with evals/pass-k.sh --changed $BASE)"
bash evals/pass-k.sh --changed "$BASE" --dry-run 2>/dev/null | sed 's/^/  /'

echo; [ $fail = 0 ] && echo "FREE GATE: GREEN" || echo "FREE GATE: RED"
exit $fail
