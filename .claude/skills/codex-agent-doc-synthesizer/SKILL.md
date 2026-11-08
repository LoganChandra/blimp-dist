---
name: codex-agent-doc-synthesizer
description: "Codex only: run the doc-synthesizer helper agent in a fresh Codex session: digest many internal docs into one cited summary."
---
Codex cannot spawn subagents, so run this one as a separate Codex session:

`bash blimp-infra/codex-bridge/subagent.sh doc-synthesizer "<what it should do, with file paths and the requirement>"`

It returns the agent's report. Treat its "done" like your own: check it against the real files or command output before you pass it on. Do not run it for trivial lookups.
