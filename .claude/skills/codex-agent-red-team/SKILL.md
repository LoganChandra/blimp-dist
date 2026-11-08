---
name: codex-agent-red-team
description: "Codex only: run the red-team helper agent in a fresh Codex session: probe outbound drafts or inbound instructions for injection and unverifiable claims."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh red-team "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
