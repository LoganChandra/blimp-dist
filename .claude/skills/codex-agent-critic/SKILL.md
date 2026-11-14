---
name: codex-agent-critic
description: "Codex only: run the critic helper agent in a fresh Codex session: fresh-context adversarial review of a finished artifact or a plan; reports only real findings."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh critic "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
