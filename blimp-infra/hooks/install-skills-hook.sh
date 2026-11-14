#!/usr/bin/env bash
# install-skills-hook.sh: the Boss runs this at his keyboard (rail #8: hooks in ~/.claude/settings.json).
# Adds the skills-check UserPromptSubmit hook for Duke and Frank (shared user settings) and
# enables the same check for Hermes via the blimp-brain plugin. Backs up settings, idempotent.
# Undo: cp ~/.claude/settings.json.bak-skills ~/.claude/settings.json
set -euo pipefail
S="$HOME/.claude/settings.json"
H=$HOME/blimp/repo/blimp-infra/hooks
command -v jq >/dev/null || { echo "jq is missing: sudo apt-get install -y jq"; exit 1; }
[ -f "$H/promptsubmit-skills.sh" ] && grep -q skills-check "$H/hermes/blimp-brain/__init__.py" 2>/dev/null \
  || { echo "$H is missing the skills hook. Merge the skills-check PR and run: git -C $HOME/blimp/repo pull"; exit 1; }
[ -e "$S.bak-skills" ] || cp "$S" "$S.bak-skills"
CMD="bash $H/promptsubmit-skills.sh"
jq --arg c "$CMD" '
  .hooks //= {} | .hooks.UserPromptSubmit //= []
  | if ([.hooks.UserPromptSubmit[].hooks[]?.command] | index($c)) then . else .hooks.UserPromptSubmit += [{"hooks":[{"type":"command","command":$c,"timeout":5}]}] end
' "$S" > "$S.tmp" && mv "$S.tmp" "$S"
echo "Claude Code hook installed:"; jq -r '.hooks.UserPromptSubmit[].hooks[].command' "$S"
mkdir -p "$HOME/.hermes/plugins"
ln -sfn "$H/hermes/blimp-brain" "$HOME/.hermes/plugins/blimp-brain"
hermes plugins enable blimp-brain || echo "Run: hermes plugins enable blimp-brain"
echo "Restart so it loads: duke-down && duke-up; frank-down && frank-up; systemctl --user restart hermes-gateway"
