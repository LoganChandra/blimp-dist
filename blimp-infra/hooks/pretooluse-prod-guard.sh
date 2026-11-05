#!/usr/bin/env bash
# pretooluse-prod-guard.sh — Duke v2 P1.2, PreToolUse hook (matcher: Bash).
#
# Deterministic enforcement of the prod-deploy-on-ask + push-to-main rules
# (memories: git-push-blocked-via-discord, prod-deploy-on-ask). Blocks Bash
# commands that match:
#   - git push to the default branch (main/master), bare `git push`, or
#     `git push ... HEAD` (current branch is usually main in Duke sessions),
#   - any git push --force / --force-with-lease / -f,
#   - vercel --prod / --production,
#   - supabase db push.
# Exit 2 + stderr = tool call blocked, message fed back to Claude with the
# hand-to-the Boss instruction. Everything else exits 0 (no decision).
#
# Designed to not wedge: exits 0 instantly unless DUKE_CHANNEL=discord
# (boot-text-blimp.sh exports it; headless runs are never gated), and every
# parse failure fails OPEN. `set -e` deliberately absent — a guard that
# crashes must not block.
#
# Stdin (hooks API): {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}
set -uo pipefail

# Gate: only live Discord-served Duke sessions are enforced.
[ "${DUKE_CHANNEL:-}" = "discord" ] || exit 0

[ -t 0 ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload="$(cat 2>/dev/null)" || exit 0
[ -n "$payload" ] || exit 0

tool="$(jq -r '.tool_name // ""' <<<"$payload" 2>/dev/null)" || exit 0
[ "$tool" = "Bash" ] || exit 0

cmd="$(jq -r '.tool_input.command // ""' <<<"$payload" 2>/dev/null)" || exit 0
[ -n "$cmd" ] || exit 0

# ERE patterns, matched per-line so multi-line/compound commands are covered.
# [^|;&]* keeps a match inside one shell command, not across && / ; / |.
re_force='git[[:space:]]+push[^|;&]*[[:space:]](--force(-with-lease)?([[:space:]=]|$)|-f([[:space:]]|$))'
re_default='git[[:space:]]+push[^|;&]*[^[:alnum:]_/.-](main|master)($|[^[:alnum:]_.-])'
re_bare='git[[:space:]]+push[[:space:]]*(-u[[:space:]]+)?(origin)?[[:space:]]*($|[;&|>)])'
re_head='git[[:space:]]+push[^|;&]*[[:space:]]HEAD[[:space:]]*($|[;&|>)])'
re_vercel='vercel[^|;&]*[[:space:]]--prod(uction)?([[:space:]]|$|[;&|>)])'
re_supabase='supabase([[:space:]]+[^[:space:]|;&]+)*[[:space:]]+db[[:space:]]+push'

matched=""
if   printf '%s\n' "$cmd" | grep -qE "$re_force";    then matched="git push --force"
elif printf '%s\n' "$cmd" | grep -qE "$re_vercel";   then matched="vercel --prod"
elif printf '%s\n' "$cmd" | grep -qE "$re_supabase"; then matched="supabase db push"
elif printf '%s\n' "$cmd" | grep -qE "$re_default";  then matched="git push to default branch (main/master)"
elif printf '%s\n' "$cmd" | grep -qE "$re_bare";     then matched="bare git push (current branch may be main)"
elif printf '%s\n' "$cmd" | grep -qE "$re_head";     then matched="git push HEAD (current branch may be main)"
fi

[ -n "$matched" ] || exit 0

echo "Blocked by prod-guard: ${matched}. This is the Boss-terminal-only from a Discord session (rails: push-to-default / prod deploy). Send the Boss the exact command on Discord to run at his keyboard, then verify after he confirms. To push work in the meantime, use an explicitly named feature branch: git push origin <branch>." >&2
exit 2
