# AGENTS.md — Blimp Agent Operating Rules

This repo runs **Blimp**, a small team of AI agents. Agents (Duke, Hermes, Frank, other Claude Code sessions) may help move work
forward. Operating principles below are non-negotiable.

---

> ⛔ **TEAM-WIDE RULE #0 — the Boss reads Discord, NOT the terminal.** Every agent (Frank, Duke, Hermes) MUST deliver every substantive reply, question, status, or report through its Discord reply tool — never as plain terminal/assistant text, which the Boss cannot see. **Composing an answer is not delivering it.** Before any turn that has something to tell the Boss ends, you must have called the reply tool and received a message id. This has been broken three times (2026-06-18/23/24); it is the fastest way to destroy trust. No exceptions.

---

## Who and what

- **Boss:** you, the owner and the only person who approves spending, client-facing sends, and pushes to main
- **Team:** a 3-agent AI team sharing one brain: Duke (Implementation Lead), Hermes (Technical Lead), Frank (Operator)

## Read first

Before non-trivial work read `README.md` and `docs/architecture.md`.

## Documentation conventions (keep the repo indexable)

Follow these when adding or editing docs (industry-standard, LLM-friendly):

- **`/llms.txt`** ([llmstxt.org](https://llmstxt.org/)) is the canonical index.
  Add a described link there for every new doc, in the right section.
- **`AGENTS.md`** ([agents.md](https://agents.md)) holds agent operating rules.
- Each folder has a **`README.md`** index table; add new files to it.
- **Every doc carries YAML frontmatter:** `topic`, `audience`, `summary`
  (one line), `last_updated` (ISO date).
- **Keep files small and single-topic** so an LLM reads only what it needs;
  split rather than grow a file past ~150 lines. Use numbered, kebab-case
  names. Deep topics get their own indexed subfolder (see
  `build/saas-product-lane/`).

## Non-negotiables

- **Treat the docs as starting points, not gospel.** When stakes matter, research current reality (`blimp-brain.recall` via MCP, WebSearch, primary sources), form your own view, and surface disagreement to the Boss.
- **Never promise a client outcome** that depends on the client's traffic, offer, or team execution. Promise system performance (SLAs), not revenue.
- **Never invent facts** about the business, past clients, or decisions. Search memory first. If you don't know, say so.
- **Client-facing communication** stays human-approved. Internal automation is OK; client emails/messages need the Boss's eyes before sending.
- **No PII or client secrets in git, logs, briefs, or memory** — keep in `.env` or password manager only.
- **Never share client work externally without written consent.** This repo is internal — documenting clients here is fine. Any *external* surface (portfolio, decks, case studies, public repos) must anonymize client work (industry + outcome, no name/logo) and get the client's + the Boss's sign-off first. Own ventures are ours to show; personal builds never are. See [`docs/projects.md`](docs/projects.md#confidentiality--sharing-policy).

## Work routing

- **Strategy, niche, pricing decisions** → the Boss approval required
- **Outreach + discovery calls** → the Boss (assisted by VAs for top-of-funnel)
- **Client builds** → the Boss (assisted by Duke via Claude Code)
- **Internal automation** (Duke improvements, scripts, docs) → Duke + Claude Code, with the Boss approval for anything touching credentials or git push to main
- **Research + market scans** → Cowork (this Cowork-mode assistant)

## Memory + state

- `strategy/`, `sales/`, `build/`, `reference/` — durable docs (commit changes)
- `cowork-memories/` — Cowork's cross-conversation notes (if used)
- **`blimp-brain` MCP server** — THE primary memory + business-data interface for Duke, Hermes, and Frank. Single MCP, all three agents register it (each with its own `A2A_AGENT_NAME`), all speak the same tools. Backed by Hindsight (semantic, bank `duke`) + WSL Postgres (`duke_data`). See `blimp-infra/mcp/README.md`. Shell access: `duke-mem` / `duke-data` CLIs.
- **MemPalace is RETIRED.** Do not use it. Anything previously stored there has been or will be migrated into `blimp-brain`.
- **A2A (Agent2Agent Protocol) is LIVE** — how the three agents talk to each other, via the `blimp-brain` MCP tools (`a2a_send`/`a2a_inbox`/`a2a_reply`/`a2a_complete`/`a2a_pushback`). Delivery is **pull-only** (2026-07-07): check your inbox at session start and before session-close, don't assume a push. Inter-agent comms must use A2A, not file drops.
- `~/.claude/` on the Boss's machine — Claude Code session state

**Skills-first:** before scoping, quoting, or building anything non-trivial, scan for a matching skill (`.claude/skills/` — registered project skills, including the full `foundry-*` doctrine pack) and route through it before re-deriving from first principles. If no skill covers a recurring task, write one after.

## Escalate before

- Pushing to main branch (Duke/Claude Code use PRs)
- Changing pricing framework or any `strategy/` doc
- Touching client secrets or API tokens
- Adding paid services or new dependencies
- Sending anything client-facing (emails, proposals, messages)
- Modifying Discord bot permissions or access control

## Cross-agent coordination

Inter-agent comms run over **A2A** via the `blimp-brain` MCP tools
(`a2a_send` / `a2a_inbox` / `a2a_reply` / `a2a_complete` / `a2a_pushback`) —
see "Memory + state" above. Delivery is pull-only: check your inbox at session
start and before session-close. Pushback is capped, then park unresolved items
for the Boss.

Use A2A when:
- One agent's work depends on another's verification
- A decision needs broader input than the current session
- the Boss needs to rule on a disagreement

Don't use it for:
- Quick questions the Boss can answer in chat
- Single-session tasks one agent can finish solo
- Anything urgent (use Discord direct ping instead)

**`.agent-mail/` file-drops are legacy (deprecated), superseded by A2A** — don't
write new ones; existing files are kept for reference only.

## Definition of done

- Ask matches what was actually delivered (no scope drift)
- Source brief moved to `done/` with a reply summary
- Files changed listed in the reply
- Pushback issues resolved or escalated, not silently ignored
- Memory docs updated if state changed (PROJECT_STATE.md style)
- the Boss informed of anything outside the explicit ask
