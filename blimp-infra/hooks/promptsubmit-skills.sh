#!/usr/bin/env bash
# promptsubmit-skills.sh — UserPromptSubmit hook, same for Duke, Frank and Hermes' Claude runtimes.
# the Boss 2026-11-07: skills must be considered for EVERY message, not just "build" tasks
# (mockups, proposals and dashboards slipped past the skills-first rule).
# Fires on any prompt that arrives from the Discord channel. Cheap: it lists skill NAMES
# (read from .claude/skills) and asks for one line; nothing is loaded unless a skill matches.
# Never blocks: always exit 0, any failure is silent.
set -uo pipefail
IN=""; [ -t 0 ] || IN="$(cat 2>/dev/null || true)"
case "$IN" in *'channel source='*) ;; *) [ -n "${BLIMP_AGENT:-}" ] || exit 0 ;; esac
ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
names="$(ls "$ROOT/.claude/skills" 2>/dev/null | tr '\n' ' ')"
[ -n "$names" ] || exit 0
printf '[skills-check] Before replying, scan these skills against this message (any kind: question, mockup, proposal, UI, data, strategy, not only builds): %s\nOpen your reply with one line "Skills: <name(s) loaded, or none + why>". Load any match with the Skill tool first. UI/dashboard work: claude-code-frontend, web-design-mastery, real-user-audit, and "what would Apple do".\n' "$names"
exit 0
