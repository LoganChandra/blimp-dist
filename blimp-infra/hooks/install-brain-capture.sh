#!/usr/bin/env bash
# install-brain-capture.sh: the Boss runs this at his keyboard (rail #8: hooks in ~/.claude/settings.json).
#
# It adds two hooks for every Claude Code agent on this PC (Duke and Frank share the user settings):
#   Stop         -> stop-brain-capture.py         every Discord exchange is written to the team brain automatically
#   SessionStart -> sessionstart-team-context.py  each session starts knowing what the other agents did last,
#                                                 and hears about it if capture is failing
# And the same two for Hermes, as a Hermes plugin (~/.hermes/plugins/blimp-brain -> hooks/hermes/blimp-brain):
#   post_llm_call -> hermes-brain-capture.py        pre_llm_call (first turn) -> sessionstart-team-context.py
# Then it backfills everything since 2026-09-02 (when Duke's logging stopped): Duke, Frank and Hermes.
# It backs up settings.json first, leaves the existing hooks in place, and is safe to re-run.
# Undo: cp ~/.claude/settings.json.bak-brain-capture ~/.claude/settings.json
set -euo pipefail
S="$HOME/.claude/settings.json"
PY=$HOME/blimp/duke-venv/bin/python
H=$HOME/blimp/repo/blimp-infra/hooks
P="$HOME/.claude/projects"
command -v jq >/dev/null || { echo "jq is missing: sudo apt-get install -y jq"; exit 1; }
[ -f "$H/stop-brain-capture.py" ] && [ -f "$H/sessionstart-team-context.py" ] && [ -f "$H/brain-capture.json" ] \
  && grep -q "def redact" "$H/stop-brain-capture.py" && grep -q "_ENTRY" "$H/stop-brain-capture.py" \
  && [ -f "$H/hermes-brain-capture.py" ] && [ -f "$H/hermes/blimp-brain/__init__.py" ] \
  || { echo "$H has an old copy of the hooks. Merge the brain-capture PR and run: git -C $HOME/blimp/repo pull"; exit 1; }
curl -fs http://127.0.0.1:8888/health >/dev/null || { echo "The brain (Hindsight) isn't answering on :8888. Start it first."; exit 1; }
[ -e "$S.bak-brain-capture" ] || cp "$S" "$S.bak-brain-capture"   # first run only, so undo always means "before"
CAP="$PY $H/stop-brain-capture.py"
CTX="$PY $H/sessionstart-team-context.py"
jq --arg cap "$CAP" --arg ctx "$CTX" '
  .hooks //= {}
  | .hooks.Stop //= []
  | .hooks.SessionStart //= []
  | if ([.hooks.Stop[].hooks[]?.command] | index($cap)) then . else .hooks.Stop += [{"hooks":[{"type":"command","command":$cap,"timeout":10}]}] end
  | if ([.hooks.SessionStart[].hooks[]?.command] | index($ctx)) then . else .hooks.SessionStart += [{"hooks":[{"type":"command","command":$ctx,"timeout":15}]}] end
' "$S" > "$S.tmp" && mv "$S.tmp" "$S"
echo "Hooks installed:"
jq -r '.hooks | to_entries[] | "  \(.key): \([.value[].hooks[].command] | join(" | "))"' "$S"

echo "Hermes: linking the blimp-brain plugin and enabling it..."
mkdir -p "$HOME/.hermes/plugins"
ln -sfn "$H/hermes/blimp-brain" "$HOME/.hermes/plugins/blimp-brain"
hermes plugins enable blimp-brain || echo "Could not enable the Hermes plugin; run: hermes plugins enable blimp-brain"

echo "Backfilling what the brain missed since 2026-09-02 (a few cents, runs in the brain's background queue)..."
BF_OK=1
$PY "$H/stop-brain-capture.py" --backfill duke 2026-09-02 "$P"/-mnt-c-Blimp/*.jsonl "$P"/-home-USER-blimp-repo/*.jsonl || BF_OK=0
$PY "$H/stop-brain-capture.py" --backfill nanook 2026-09-02 "$P"/-home-logan-nanook-repo/*.jsonl || BF_OK=0
$PY "$H/hermes-brain-capture.py" --backfill 2026-09-02 || BF_OK=0
[ $BF_OK = 1 ] || echo "A backfill hit an error (the hooks are installed fine). Re-run this script later; it only re-sends what's missing."

echo "Done. Duke, Frank and Hermes now save every Discord exchange by themselves."
echo "Restart them so it loads: duke-down && duke-up; frank-down && frank-up; systemctl --user restart hermes-gateway"
echo "(Restarts resume each agent's last conversation; BLIMP_FRESH=1 duke-up starts blank.)"
