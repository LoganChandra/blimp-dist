---
name: codex-agent-data-analyst
description: "Codex only: run the data-analyst helper agent in a fresh Codex session: read-only SQL analysis over the structured business data."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh data-analyst "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
