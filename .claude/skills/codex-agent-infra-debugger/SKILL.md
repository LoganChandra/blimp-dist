---
name: codex-agent-infra-debugger
description: "Codex only: run the infra-debugger helper agent in a fresh Codex session: diagnose a down service, port or timer with read-only commands."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh infra-debugger "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
