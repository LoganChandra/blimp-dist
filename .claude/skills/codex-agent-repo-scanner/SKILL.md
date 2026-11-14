---
name: codex-agent-repo-scanner
description: "Codex only: run the repo-scanner helper agent in a fresh Codex session: read-only map of a codebase: where things live."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh repo-scanner "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
