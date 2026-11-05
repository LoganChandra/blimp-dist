---
name: foundry-deterministic-core
description: >-
  Use when building ANY AI-powered product or feature. The trust architecture:
  the LLM never decides anything numerical, legal, or binding — it extracts,
  classifies, explains; all authoritative outputs come from deterministic code
  over versioned data. The single most important product decision in the estate.
---

# Deterministic core — the trust architecture for AI products

**THE LLM NEVER DECIDES ANYTHING NUMERICAL, LEGAL, OR BINDING.** It extracts,
classifies, and explains. All authoritative outputs come from deterministic code
over versioned data.

Why: one hallucinated number/citation shipped to a user is an extinction-level
trust event for an indie product; it's the FTC-safe posture; deterministic paths
are free while LLM calls are metered; the rulebook + outcome data compound into a
moat that prompts can't copy.

## The pattern
```
input ──> normalize + guard           deterministic, cheap, runs first
      ──> retrieve candidates          index/search over versioned data
      ──> LLM judgment                  ONLY chooses among retrieved candidates
                                        (whitelist), schema/zod-validated,
                                        confidence-scored, 1 retry then fallback
      ──> deterministic compute         the numbers: pure functions over versioned
                                        tables, injected clock, golden-tested
      ──> render + stamp                citations, as-of date, data version,
                                        dataset flag, disclaimer on EVERY output
```

## Build rules (each earned by a real failure)
- **Zero-key fallback mode** — the product works with NO API keys (deterministic path only, honestly labeled). Survivability + demo-ability.
- **Hermetic builds** — no network at build time; fonts/assets runtime-loaded with fallbacks (L-003).
- **Optional files via `fs` at runtime, never `import`/`require`** — bundlers resolve eagerly (L-005).
- **Versioned data + public changelog** — every table change = changelog entry + primary-source citation. Sample/seed data FLAGGED on every output until verified-full.
- **Golden tests grow monotonically** — every production miss becomes a regression case; suites only ratchet. Tests written WITH the engine, not after.
- **Injected clock** — no `Date.now()` inside pure functions. Determinism = testability.
- **Confidence shown = confidence measured** — HIGH must mean ≥95% on real outcomes or the gate tightens.
- **A guard layer refuses bad-faith / evasion inputs** with calm, principled copy (ethics + brand + ads-policy in one).

## Stack defaults (indie-shaped, swap-friendly)
Next.js App Router + Tailwind (tokens-first) · zod at every boundary · vitest
golden suites · provider-agnostic LLM seam (one `callModel()` indirection) ·
Supabase when state outgrows cookies · MoR checkout env-gated · fixed infra
<$60/mo until revenue says otherwise.
