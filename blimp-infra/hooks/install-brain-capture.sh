#!/usr/bin/env bash
# install-brain-capture.sh: the Boss runs this at his keyboard (rail #8: hooks in ~/.claude/settings.json).
#
# It adds two hooks for every Claude Code agent on this PC (Duke and Frank share the user settings):
#   Stop         -> stop-brain-capture.py         every Discord turn is written to the team brain automatically
#   SessionStart -> sessionstart-team-context.py  each session starts knowing what the other agents did last
# It backs up settings.json first, leaves the existing Discord reply guard in place, and is safe to re-run.
# Undo: cp ~/.claude/settings.json.bak-brain-capture ~/.claude/settings.json
set -euo pipefail
S="$HOME/.claude/settings.json"
PY=$HOME/blimp/duke-venv/bin/python
H=$HOME/blimp/repo/blimp-infra/hooks
command -v jq >/dev/null || { echo "jq is missing: sudo apt-get install -y jq"; exit 1; }
[ -f "$H/stop-brain-capture.py" ] && [ -f "$H/sessionstart-team-context.py" ] || { echo "hook scripts not found in $H"; exit 1; }
cp "$S" "$S.bak-brain-capture"
CAP="$PY $H/stop-brain-capture.py"
CTX="$PY $H/sessionstart-team-context.py"
jq --arg cap "$CAP" --arg ctx "$CTX" '
  .hooks //= {}
  | .hooks.Stop //= []
  | .hooks.SessionStart //= []
  | if ([.hooks.Stop[].hooks[]?.command] | index($cap)) then . else .hooks.Stop += [{"hooks":[{"type":"command","command":$cap,"timeout":10}]}] end
  | if ([.hooks.SessionStart[].hooks[]?.command] | index($ctx)) then . else .hooks.SessionStart += [{"hooks":[{"type":"command","command":$ctx,"timeout":15}]}] end
' "$S.bak-brain-capture" > "$S.tmp" && mv "$S.tmp" "$S"
echo "Installed. Hooks now:"
jq -r '.hooks | to_entries[] | "\(.key): \([.value[].hooks[].command] | join(" | "))"' "$S"
echo "Restart Duke and Frank (duke-down && duke-up; frank-down && frank-up) so their brain tools load the fixes."
