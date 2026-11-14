#!/usr/bin/env bash
# set-identity.sh duke|frank: write this clone's untracked CLAUDE.local.md, which imports the agent's
# identity file. The repo is shared by Duke's and Frank's clones, so identity can't live in CLAUDE.md.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
case "${1:-}" in
  duke) f=duke-system-context.md ;;
  frank|nanook) f=frank-system-context.md ;;
  *) echo "usage: set-identity.sh duke|frank"; exit 2 ;;
esac
printf '@blimp-infra/%s\n' "$f" > "$ROOT/CLAUDE.local.md"
echo "$ROOT/CLAUDE.local.md -> $f"
