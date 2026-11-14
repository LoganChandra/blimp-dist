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

## Brain capture (v2, 2026-11-05)

| File | Hook event | Does |
|---|---|---|
| `stop-brain-capture.py` | Stop | Writes every Discord exchange (what someone sent + the agent's replies) to the team brain as one Hindsight document, `Name (time UTC): text`, `document_id = discord:<agent>:<first message id>`. Names come from `brain-capture.json`. Secrets are stripped first. Forks, frees the hook's pipes and returns 0 at once; failures go to `~/.cache/blimp-brain-capture/capture.log`. |
| `sessionstart-team-context.py` | SessionStart | Blimp agents only: prints the other agents' latest exchanges (DMs excluded) and a one-line alert if the last capture failed. |
| `install-brain-capture.sh` | — | the Boss runs it at his keyboard (rail #8). Adds both hooks, backfills since 2026-09-02. Undo: `cp ~/.claude/settings.json.bak-brain-capture ~/.claude/settings.json`. |

Tests: `~/blimp/mesh-venv/bin/pytest blimp-infra/hooks/test_stop_brain_capture.py` (99 cases: speakers, grouping,
mid-turn messages, fake messages in tool output, clock skew, long turns, idempotency, locking, backfill parity,
40+ secret formats and as many ordinary sentences that must survive). Reviewed by a fresh-context critic over six
passes until no leak remained. Known limits: only Claude Code agents (Duke, Frank) are captured, not Hermes;
a letters-only password with an apostrophe in it is kept.

## Hermes parity and restart-resume (2026-11-06)

All three agents get the same brain hooks (the Boss: "all of you are equal"):

| | Duke, Frank (Claude Code) | Hermes (Hermes Agent) |
|---|---|---|
| Capture every Discord exchange | Stop hook -> `stop-brain-capture.py` | plugin `hermes/blimp-brain` post_llm_call -> `hermes-brain-capture.py` (reads `~/.hermes/state.db` read-only) |
| Team brief at session start | SessionStart hook -> `sessionstart-team-context.py` | same plugin, pre_llm_call on the first turn -> same script |
| Restart keeps the thread | `agent/launch.sh` resumes the last real Discord session | native: hermes-gateway keeps sessions in `state.db` across restarts |

Headless runs (`claude -p`, evals) are skipped by both the capture and the resume picker: some feed in
fake Discord messages. One installer for all of it: `install-brain-capture.sh` (the Boss's paste, rail 8).
Tests: `pytest blimp-infra/hooks blimp-infra/agent`.

## Skills check (all agents)

`promptsubmit-skills.sh` (UserPromptSubmit) injects the skill names on every Discord message and asks for a `Skills: <name or none>` opening line, so skills are considered for any message, not only builds (the Boss, 2026-11-07). Fires on a `<channel source=` prompt or when `BLIMP_AGENT` is set; never blocks. Merge `settings-snippet-skills.json` into `~/.claude/settings.json` (rail #8, terminal only). Unverified: that the live payload's prompt carries the `<channel` wrapper (the `BLIMP_AGENT` fallback covers it); Hermes needs its own runtime hook.

Install for all three agents: `bash blimp-infra/hooks/install-skills-hook.sh` (the Boss, at the keyboard: it edits `~/.claude/settings.json`, rail #8). It also enables the Hermes `skills_check` hook in the blimp-brain plugin.
