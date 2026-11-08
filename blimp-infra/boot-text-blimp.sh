#!/usr/bin/env bash
# Boot Blimp Duke in WSL tmux session 'duke'. Called by boot-blimp.bat.
#
# Uses DISCORD_STATE_DIR to isolate Duke's channels-plugin config from other-agent's (<your-product>).
# <your-product> other-agent uses the default ~/.claude/channels/discord/.
# Blimp Duke uses ~/blimp/discord-state/ with its own .env, access.json, etc.
set -e

echo "[duke boot] killing stale tmux session"
tmux kill-session -t duke 2>/dev/null || true
tmux kill-session -t blimp 2>/dev/null || true  # legacy name cleanup
sleep 2

echo "[duke boot] ensuring Blimp state dir exists"
mkdir -p ~/blimp/discord-state/approved ~/blimp/discord-state/inbox

# If Blimp's state dir doesn't have a .env yet, seed it from ~/blimp/.env (which has the token)
if [[ ! -f ~/blimp/discord-state/.env ]]; then
  grep -E '^DISCORD_BOT_TOKEN=' ~/blimp/.env > ~/blimp/discord-state/.env 2>/dev/null || true
  chmod 600 ~/blimp/discord-state/.env 2>/dev/null || true
fi

# Seed access.json if missing
if [[ ! -f ~/blimp/discord-state/access.json ]]; then
  cat > ~/blimp/discord-state/access.json <<'JSON'
{
  "dmPolicy": "open",
  "allowFrom": ["<ID>"],
  "groups": {},
  "pending": {}
}
JSON
fi

echo "[duke boot] launching in tmux (DISCORD_STATE_DIR=~/blimp/discord-state, DUKE_CHANNEL=discord)"
cd ~/blimp/repo 2>/dev/null || cd ~

# DUKE_CHANNEL=discord arms the P1.2 enforcement hooks (blimp-infra/hooks/).
# Headless runs (Dreams, evals, claude -p) never export it, so the hooks
# exit 0 there and can never wedge an unattended session.
# Identity comes from the clone's untracked CLAUDE.local.md (see CLAUDE.md); make sure it is there.
[ -f ~/blimp/repo/CLAUDE.local.md ] || bash ~/blimp/repo/blimp-infra/agent/set-identity.sh duke >/dev/null
# Threads compact at ~200k tokens (CLAUDE_CODE_AUTO_COMPACT_WINDOW, Claude Code 2.1.318), so a restart that
# resumes one re-sends at most ~200k, not 500k+ (2026-11-06: ~7% of the 5-hour limit per restart at 536k).
# The brain keeps the detail compaction drops. Same for Duke and Frank; Hermes compresses on his own.
# Resumes the last real Discord session (agent/launch.sh); BLIMP_FRESH=1 starts blank.
tmux new-session -d -s duke "export CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 BLIMP_FRESH=${BLIMP_FRESH:-} BLIMP_AGENT=duke; export PATH=\$HOME/.bun/bin:\$HOME/.npm-global/bin:\$HOME/.local/bin:\$PATH; export DISCORD_STATE_DIR=\$HOME/blimp/discord-state; export DUKE_CHANNEL=discord; set -a; source ~/blimp/.env 2>/dev/null; set +a; cd ~/blimp/repo && bash \$HOME/blimp/repo/blimp-infra/agent/launch.sh --model claude-sonnet-5-5 --effort medium --channels plugin:discord@claude-plugins-official"
sleep 6

echo "[duke boot] tmux session check:"
tmux ls 2>&1 || echo "no tmux sessions"
echo
echo "[duke boot] last 10 lines from pane:"
tmux capture-pane -t duke -p -S -10 2>&1 || echo "capture failed"
