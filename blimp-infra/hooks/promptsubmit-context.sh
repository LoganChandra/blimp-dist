#!/usr/bin/env bash
# promptsubmit-context.sh — Duke v2 P1.2, UserPromptSubmit hook.
#
# Injects ONE line of context at every prompt (hooks API: exit 0 + stdout is
# added as context Claude can see): the open-task count from duke_data and the
# recall-first reminder from CLAUDE.md's memory protocol.
#
# NEVER blocks: always exits 0, psql is capped at 2s, and any DB failure
# degrades to "?" instead of an error. Gated on DUKE_CHANNEL=discord like the
# other P1.2 hooks — headless runs get no output and no DB hit.
#
# duke_data lives on the port-5433 Postgres via unix socket (see
# blimp-infra/postgres/README.md); "open" matches the schema.sql convention:
# status IN ('pending', 'in_progress').
set -uo pipefail

# Gate: only live Discord-served Duke sessions get the context line.
[ "${DUKE_CHANNEL:-}" = "discord" ] || exit 0

# Drain stdin (hooks API sends the payload; we don't need it). Skip on a tty
# so a by-hand run doesn't hang waiting for input.
[ -t 0 ] || cat >/dev/null 2>&1 || true

count=""
if command -v psql >/dev/null 2>&1; then
  count="$(timeout 2 psql --host=/var/run/postgresql --port=5433 -d duke_data -Atc \
    "SELECT count(*) FROM tasks WHERE status IN ('pending', 'in_progress')" 2>/dev/null)" || count=""
fi
case "$count" in '' | *[!0-9]*) count="?" ;; esac

printf '[duke-context] open tasks in duke_data: %s | recall-first: call mcp__blimp-brain__recall before answering anything that depends on past context (Hermes may have written something since your last turn).\n' "$count"
exit 0
