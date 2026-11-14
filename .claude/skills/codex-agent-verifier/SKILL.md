---
name: codex-agent-verifier
description: "Codex only: run the verifier helper agent in a fresh Codex session: run the declared checks and return PASS/FAIL with evidence."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh verifier "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
