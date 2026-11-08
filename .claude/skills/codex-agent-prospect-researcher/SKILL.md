---
name: codex-agent-prospect-researcher
description: "Codex only: run the prospect-researcher helper agent in a fresh Codex session: research and score prospects on the web."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh prospect-researcher "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
