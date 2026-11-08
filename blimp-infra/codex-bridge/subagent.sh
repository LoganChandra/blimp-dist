#!/usr/bin/env bash
# Run one helper agent (.claude/agents/<name>.md) as a fresh Codex session.
# Usage: subagent.sh <name> "<task>"     Prints the agent's final report.
# Read-only agents (tools without Bash/Edit/Write) get a read-only sandbox.
set -euo pipefail
name="${1:?agent name}"; task="${2:?task}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
f="$REPO/.claude/agents/$name.md"; [[ -f "$f" ]] || { echo "no such agent: $name"; ls "$REPO/.claude/agents" | sed 's/\.md$//'; exit 1; }
tools="$(sed -n '1,/^---$/{/^tools:/p}' "$f" | head -1)"
sandbox=read-only; [[ "$tools" =~ (Edit|Write|NotebookEdit) ]] && sandbox=workspace-write
body="$(awk 'BEGIN{n=0} /^---$/{n++; next} n>=2' "$f")"
out="$(mktemp)"; trap 'rm -f "$out"' EXIT
codex exec --skip-git-repo-check -C "$REPO" -s "$sandbox" -o "$out" \
  "You are a separate helper agent with a fresh context. Do only this role and report back.

$body

TASK:
$task" >/dev/null 2>&1 || true
[[ -s "$out" ]] && cat "$out" || echo "(the $name agent returned nothing; run again or run it yourself by reading $f)"
