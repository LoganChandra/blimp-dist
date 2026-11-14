#!/usr/bin/env bash
# pass-k.sh: run behaviour-eval cases k times each and report pass^k, on demand only.
#
# One passing run says the agent CAN behave; pass^k (passes all k runs) says it RELIABLY does.
# tau-bench (Yao et al. 2024, arxiv.org/abs/2406.12045) found agents at ~60% pass@1 fall to ~25% at
# pass^8, so "it passed once" is not evidence. k=3 is the cheap middle.
#
# Each case run is a real `claude -p` session, i.e. tokens. So this is NEVER a timer or an automatic
# merge gate (the Boss 2026-11-06: no tokens on merge). Run it when a change touches behaviour, and only
# on the cases the change touches:
#
#   evals/pass-k.sh --cases never-broad-pkill,myr-to-sg-client      # named cases
#   evals/pass-k.sh --changed main                                  # cases the diff vs main touches
#   evals/pass-k.sh --changed main --dry-run                        # just list what it would run
#   options: --k 3 (default)  --agent duke|frank (default duke)
#
# --changed picks a case when its yaml changed, or when its text names a changed skill/agent/file
# (e.g. a diff to .claude/skills/simplify-first/ selects the cases that mention "simplify-first").
# Exit 0 = every selected case passed all k runs.
set -uo pipefail
EVALS="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$EVALS/.." && pwd)"
K=3; AGENT=duke; CASES=""; CHANGED=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --k) K="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    --cases) CASES="$2"; shift 2 ;;
    --changed) CHANGED="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done

if [ -n "$CHANGED" ]; then
  mapfile -t files < <(git -C "$REPO" diff --name-only "$CHANGED"...HEAD; git -C "$REPO" diff --name-only)
  sel=()
  for f in "${files[@]}"; do
    case "$f" in
      evals/cases/*.yaml) sel+=("$(basename "$f" .yaml)"); continue ;;
    esac
    # the most specific name in the path: a skill/agent dir, else the file stem
    tok="$(printf '%s' "$f" | sed -E 's#^\.claude/(skills|agents)/([^/]+).*#\2#; t; s#.*/##; s#\.[A-Za-z0-9]+$##')"
    [ ${#tok} -ge 4 ] || continue
    while IFS= read -r c; do sel+=("$(basename "$c" .yaml)"); done < <(grep -l -F -- "$tok" "$EVALS"/cases/*.yaml 2>/dev/null)
  done
  CASES="$(printf '%s\n' "${sel[@]}" | sort -u | paste -sd, -)"
fi
[ -n "$CASES" ] || { echo "No cases selected (nothing in the diff touches a case). Nothing to run."; exit 0; }
IFS=, read -ra LIST <<< "$CASES"
for c in "${LIST[@]}"; do [ -f "$EVALS/cases/$c.yaml" ] || { echo "no such case: $c"; exit 2; }; done
echo "pass^$K for ${#LIST[@]} case(s), agent $AGENT: ${LIST[*]}"
echo "That is $(( ${#LIST[@]} * K )) claude -p sessions."
[ $DRY = 1 ] && exit 0

declare -A PASSES
for c in "${LIST[@]}"; do
  PASSES[$c]=0
  for i in $(seq 1 "$K"); do
    bash "$EVALS/run-evals.sh" --agent "$AGENT" --case "$c" >/dev/null 2>&1
    run="$(cut -d' ' -f1 "$EVALS/results/last-run-stamp-$AGENT" 2>/dev/null)"
    p="$(jq -r --arg id "$c" 'select(.id == $id) | .pass' "$EVALS/results/$run/results.jsonl" 2>/dev/null | head -1)"
    [ "$p" = "true" ] && PASSES[$c]=$(( PASSES[$c] + 1 ))
    echo "  $c run $i/$K: ${p:-error} ($run)"
  done
done
all=0; once=0
echo; echo "case                                   passes  pass^$K"
for c in "${LIST[@]}"; do
  n=${PASSES[$c]}; ok=no; [ "$n" -eq "$K" ] && { ok=yes; all=$((all+1)); }; [ "$n" -gt 0 ] && once=$((once+1))
  printf '%-38s %d/%d     %s\n' "$c" "$n" "$K" "$ok"
done
echo; echo "pass^$K: $all/${#LIST[@]}   (passed at least once: $once/${#LIST[@]})"
[ "$all" -eq "${#LIST[@]}" ]
