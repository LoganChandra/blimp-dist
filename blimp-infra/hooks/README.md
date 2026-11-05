# Duke v2 P1.2 — enforcement hooks

Deterministic, env-gated Claude Code hooks that turn three CLAUDE.md prose
rules into machine enforcement. All three exit 0 instantly unless
`DUKE_CHANNEL=discord` (exported by `boot-text-blimp.sh` inside the duke tmux
session), so headless Dreams/eval/`claude -p` runs are never gated and a Stop
hook can never wedge an unattended session. Every parse failure fails OPEN.

| File | Hook event | Enforces |
|---|---|---|
| `stop-log-guard.sh` | Stop | "log every chat turn": blocks turn-end (exit 2, max once per turn via `/tmp/.duke-stop-guard-*` marker + `stop_hook_active`) if this turn has no `mcp__blimp-brain__log_message` tool call in the transcript |
| `pretooluse-prod-guard.sh` | PreToolUse (matcher `Bash`) | push-to-default-branch (`main`/`master`/bare/`HEAD`), `git push --force*`, `vercel --prod`, `supabase db push` → exit 2 with the hand-to-the Boss message (memories: git-push-blocked-via-discord, prod-deploy-on-ask) |
| `promptsubmit-context.sh` | UserPromptSubmit | injects one context line per prompt: open-task count from `duke_data` (psql, 2s timeout, degrades to `?`) + the recall-first reminder; never blocks |

`settings-snippet.json` is the exact `hooks` block for the Boss to merge into
`~/.claude/settings.json` (rail #8 — his keyboard, session 3). Paths use the
canonical `$HOME/blimp/repo` symlink.

## Testing

```
bash scratch-test/run-tests.sh
```

Self-contained harness: builds `/tmp/duke-hook-scratch` (own minimal
`.claude/settings.json`, fixture transcripts with/without a `log_message` this
turn, fixture stdin payloads) and asserts exit codes + output for 20 cases,
including turn-scoping (a prior turn's `log_message` doesn't satisfy the
current turn), the once-per-turn marker, and fail-open on a missing
transcript. Idempotent; run it any time.

Optional live override test (the "verify, don't assert" step before the Boss
merges the snippet): after `run-tests.sh` has built the scratch dir,
`cd /tmp/duke-hook-scratch && DUKE_CHANNEL=discord claude`, say anything, and
watch the Stop hook block exactly once. The scratch settings never leave
`/tmp`.

## Assumptions (verified against https://code.claude.com/docs/en/hooks, 2026-06-10)

- Hooks receive JSON on stdin with `session_id`, `transcript_path`,
  `stop_hook_active` (Stop), `tool_name`/`tool_input.command` (PreToolUse).
- Exit 2 blocks; stderr is fed back to Claude. Exit 0 stdout on
  UserPromptSubmit is injected as context.
- `stop_hook_active=true` means Claude is already continuing because of a Stop
  hook (loop-prevention flag). The fetched doc's wording is ambiguous on this;
  the `/tmp` marker makes once-per-turn hold either way.
- Stop and UserPromptSubmit entries take no `matcher` (they always fire);
  PreToolUse matches the tool name.
