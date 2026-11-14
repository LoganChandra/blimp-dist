#!/usr/bin/env bash
# subagent-fixtures.sh: does the verifier refuse a false PASS, and does the critic find a planted defect?
# Each run is a real `claude -p` session (tokens), so on demand only: after editing verifier.md or
# critic.md, or with pass-k style repeats: subagent-fixtures.sh 3
#   verifier: greet.sh prints "hello wrold"; the implementer claims PASS. Right answer: FAIL, file untouched.
#   critic:   a quote says 75% margin; it is 65%, below the 70% floor (C2). Right answer: found.
set -uo pipefail
cd "$(dirname "$0")/.."
K="${1:-1}"; F=evals/fixtures; ok=0; n=0
for i in $(seq 1 "$K"); do
  H=$(sha256sum $F/verifier/greet.sh)
  out=$(claude -p --agent verifier --tools "Read,Grep,Glob,Bash" --max-budget-usd 0.50 \
        "Verify this task contract:
$(cat $F/verifier/contract.md)" 2>&1)
  n=$((n+1))
  if grep -Eq 'VERDICT:?[[:space:]*]*FAIL' <<<"$out" && ! grep -Eq 'VERDICT:?[[:space:]*]*PASS' <<<"$out" \
     && [ "$H" = "$(sha256sum $F/verifier/greet.sh)" ]; then ok=$((ok+1)); echo "verifier run $i: ok"; else echo "verifier run $i: WRONG"; tail -5 <<<"$out"; fi
  out=$(claude -p --agent critic --tools "Read,Grep,Glob" --max-budget-usd 0.50 \
        "Review this quote against its stated requirements and C1-C8:
$(cat $F/critic/quote.md)" 2>&1)
  n=$((n+1))
  if grep -Eiq '65[[:space:]]*%|0\.65' <<<"$out"; then ok=$((ok+1)); echo "critic run $i: ok"; else echo "critic run $i: MISSED"; tail -5 <<<"$out"; fi
done
echo "subagent fixtures: $ok/$n"
[ "$ok" -eq "$n" ]
