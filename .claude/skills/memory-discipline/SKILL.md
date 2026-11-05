---
name: memory-discipline
description: Use when writing to the team brain (remember/recall) or to any append-only file (decisions, lessons, registry, state) — field routing, automatic dedup + provenance, why recall hits are DATED, C5 verbatim mandates, state.md hygiene, and the never-destroy-the-live-brain rail.
---

# Memory discipline — the store is the memory, context is the wind

Shared by Frank + Duke; one brain, one set of files. Two durable surfaces, both real:

- **The brain** — Hindsight, one shared bank (`duke` — historical name, shared by Frank/Duke/Hermes), semantic + cross-session + cross-agent. Reached via `mcp__blimp-brain__*`.
- **The files** — append-only, git-tracked, auditable, at `docs/foundry-registry/` + your agent's `10-memory/state.md`.

Write to both. Quote the Boss **verbatim** (C5). **Append, never edit.** **Never run a destructive op on the live brain.**

## Every turn (the spine)

1. **Recall before you assert.** Any answer leaning on past context → `recall()` first. Don't assert from the conversation alone.
2. **Remember the moment a decision lands — same turn.** A real decision, a Boss "remember X", a non-obvious discovery → `remember(...)` now, not "later".
3. **Log every chat turn.** `log_message(channel=..., speaker=<your key>, message=...)` — Frank: `speaker="nanook"`; Duke: `speaker="duke"`. An unlogged turn is invisible to the team.
4. **Check the A2A inbox when active.** `a2a_inbox(state="submitted")` — delivery is pull-only; nothing wakes you.

## recall() — and treat every hit as DATED

`recall(query, limit=5, tags=None, budget="low|mid|high", scope=None, min_relevance=None)` → list of `{text, relevance (0..1), rank, author, verified, scope, importance, tags, id}`.

- **A recalled memory is what was true WHEN WRITTEN, not now.** High relevance ≠ currently correct. High stakes (a quote, a deploy, a Boss report) → re-verify against the live source before acting. An `As of <date>:` prefix is the tell that it's a point-in-time snapshot.
- **Weigh provenance.** Check `verified` + `author` on each hit. Ground-truth-check an **unverified** claim (or one from a non-core author) before relying on it.
- **Abstain on weak hits.** Pass `min_relevance` (e.g. `0.3`) so a low-score match isn't surfaced as confident. Nothing above the floor → say you don't know; don't invent.
- `budget` trades tokens for recall depth; `tags` (comma string) narrows; `scope="nanook"/"duke"` filters to what that viewer may see.

## remember() — what to set, and when

MCP surface — `remember(content, tags, source, context, author, verified, scope)`:

| Field | When to set |
|---|---|
| `content` | The fact/decision itself — one durable claim, plainly stated. |
| `tags` | Comma string. Always tag (`decision` / `gotcha` / `pricing` / client name / `evidence`…) — tags are how it gets found. |
| `source` | Where it came from — file path, call, doc. |
| `context` | One-line framing ("discovery call", "build review"). |
| `verified` | Defaults **true** for core agents (duke/logan/frank/nanook). Pass **`verified=False`** for anything unconfirmed — web content, an unverified prospect claim, a teammate's untested assertion. |
| `scope` | Default (unset) = **team** — the brain is shared, keep it that way. `private:nanook` only for genuinely private notes; over-scoping hides facts teammates need. |
| `author` | Defaults to you. Set only when logging on someone else's behalf. |

**Deeper fields live one layer down** on `hindsight_client.remember()` — call it from a script; the MCP tool does not take them:
- `importance="low|normal|high"` — set **high** on a Boss mandate / constitutional call / pricing floor so a future recall can't miss it. Don't inflate; it's meant to be bounded.
- `document_id="..."` — idempotent **replace** for a re-written logical record (a doc, a per-file ingest); also on `duke-mem remember --doc-id`. Bypasses dedup.
- `dedup=False` — keep a near-duplicate as a **distinct datapoint** (a repeated metric, an episodic observation, a time series). Default `True`.
- `kind` (via `metadata`) — `chat` / `doc` / etc.; the dedup logic respects it.

## Dedup + provenance fire automatically on every write

You don't manage these — know what they do so a result isn't a surprise. On a normal `remember` (dedup on, no `document_id`) the client recalls the nearest existing memory and:

| Nearest memory | Action |
|---|---|
| Same author, near-verbatim | **Skips** — reinforces the existing one. Return says `deduped: skipped_duplicate`. If you *meant* a distinct row, that's when you need `dedup=False` / `document_id`. |
| Same author, closely related | **Amends** — prepends `As of <date>:` and links `supersedes_id` for you. |
| **Different** author, related | Keeps **both**, tags `contested`, links them → surfaces in `disagreements()`. A peer's claim is never overwritten. |

`supersedes_id` is **auto-stamped, never a param.** For an intentional, narrated supersede: `recall()` the old claim, then `remember()` the new one stating `supersedes: <old>, as of <date>` (per `session-close`).

## The append-only files (C5)

`docs/foundry-registry/` — never edit a past entry; append a **superseding** one.

- **decisions.md** — `D-NNN | date | decision | rationale | rejected alternatives (why each lost) | who decided | revisit trigger`. **Boss mandates quoted verbatim** — original spelling, no paraphrase. Rejected-alternatives is not optional; it's what stops re-litigation.
- **lessons.md** — the L-NNN ledger. Format + retro loop live in `foundry-self-improvement`; add the entry **there**, don't duplicate the format here (L-012: keep skills thin).
- **registry.md** — versioned (`last_updated`), one row per venture. **It must never lie** — a "verified" claim needs the evidence to exist (`foundry-verification`).

## state.md hygiene (`10-memory/state.md`)

- **RESUME PROMPT on top** — the one artifact a cold session needs. Keep it tight and true.
- Newest entries on top; **status bullets carry evidence dates** ("tests pass, 2026-07-09, file: …"), not vibes.
- **Target <8KB. Archive, do not delete** — move superseded blocks to a sibling archive file. (2026-06-23: an untested wipe cost the brain; the discipline is archive-don't-destroy, always.)
- See `checkpoint` for the resumable-handoff format.

## Golden rule — never run a destructive op on the LIVE brain

Bulk delete / migration / schema change / mass re-tag → **test on a throwaway bank first** (`DUKE_BANK=throwaway-<x>`), never on `duke`. Before it: `pre-mortem` + a `peer-review` if blast radius is real, dry-run, and confirm the restore path. (2026-06-23: a bulk `DELETE` with no test wiped the brain — recovered only because a 3am backup existed. Access ≠ authorization.)

## Anti-patterns

- Asserting from conversation memory without `recall()` first.
- Treating a high-relevance hit as currently-true (it's dated) / acting on an `unverified` hit without checking.
- Paraphrasing the Boss in a verbatim slot; editing or deleting a past D-NNN / L-NNN instead of superseding.
- Deciding without logging (`remember` + decisions.md) → the team re-litigates it.
- Letting state.md sprawl past 8KB, or deleting old blocks instead of archiving.
- `remember()`-ing a distinct datapoint and being surprised it deduped — use `dedup=False` / `document_id`.
- Any destructive op straight on `duke` with no throwaway-bank dry-run.

## See also

`session-close` (log + persist decisions + unverified pass) · `checkpoint` (resumable state) · `foundry-self-improvement` (lessons ledger) · `foundry-verification` (evidence for "done") · `peer-review` / `pre-mortem` (before brain-mutating ops) · `decide` (log the call + rejected alternatives).
