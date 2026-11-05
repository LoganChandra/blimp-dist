---
name: context-engineering
description: Use when a task runs long or tool-heavy, before the context window fills, or when deciding whether to compact / checkpoint / delegate — treat context as a scarce, curated budget so a long turn doesn't drop the thread.
---

# context-engineering — the context window is a budget, not a bucket

The estate's highest-risk failure class: a long, tool-heavy turn fills the
window, buries the goal under tool spew, and drops the ending. Frank logged it
three times (2026-06-18 / -23 / -24) — each a long turn that lost the thread
and never shipped the Discord reply. Curate context on the way IN so the turn
survives to its last action. This is the upstream fix; `checkpoint` is the
rescue once you're already deep.

## Budget the window
- Context is scarce and curated — every token you pull in competes with the goal for attention. It's a budget, not a bucket to dump into.
- Pull only what THIS step needs: read the relevant function with `offset`/`limit`, not the whole file; summarize long tool output to the one line that mattered instead of leaving the raw dump inline.

## COMPACT vs CHECKPOINT vs DELEGATE
Three different moves for "context is getting heavy" — pick by what you need:
- **COMPACT** — same task, still coherent, just token-heavy → shed weight and keep going. Externalize state to a file FIRST; compaction is lossy and drops specifics you didn't write down.
- **CHECKPOINT** — pausing, handing off, about to do something risky, or the session may end → write resume-from-cold state to a file. Load `checkpoint`.
- **DELEGATE** — a sub-task is long but separable (deep research, a big file dig) → spawn a subagent so the digging never enters your main window. Load `delegate`.
- Unsure? Checkpoint (cheap, a file) before compact (lossy).

## Practices
1. **Externalize state EARLY** — write the progress file BEFORE you're deep, not after you're lost. "Files are the memory, context is not" (`foundry-problem-solving` #7). A checkpoint at 20% context is free; the one you needed at 90% is already too late.
2. **Recall before reload** — before re-reading a file or decision you've already seen, `mcp__blimp-brain__recall(...)` it from the team brain, and `remember(...)` load-bearing facts as you go. The brain is external memory; use it instead of re-spending the window on the same bytes.

## Failure modes — name them, then guard
- **Context poisoning** — a wrong fact anchored early (bad assumption, stale recall) silently contaminates everything after it. Guard: verify load-bearing facts against the primary source before building on them (L-002); when an answer smells off, re-derive from source — don't trust the anchored copy.
- **Context rot** — real signal buried under dumped logs and file bodies. Guard: don't paste what you won't re-read; keep the summary, drop the dump.
- **Lost-in-the-middle** — the model attends to the START and END of a long context, not the middle. Guard: restate the goal and the next action at the END of a long turn; never trust an instruction given 30 tool calls back to still be steering you.

## Rules
- (Frank:) A long tool-heavy turn is NOT done until the Discord reply ships — it's the last action and the first casualty of a full window. Composing ≠ delivering: send it before you end the turn.
- Checkpoint before compact when in doubt — a file survives, a compaction summary may not.
- Read narrow, not whole, when you already know what you need.

Cross-refs: `checkpoint` (resume-from-cold state), `delegate` (offload research to a subagent), `session-close` (persist state + lessons at session end).
