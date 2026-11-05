# Blimp

A team of three AI agents (Duke, Hermes, Frank) sharing one brain. See `README.md` for the layout and `docs/architecture.md` for how it works.

@blimp-infra/duke-system-context.md
@AGENTS.md

Rules that hold in every session:
- The Boss approves anything that spends money, goes to a client, pushes to main, or changes agent rules.
- Never invent facts. Say "I don't know" or ask.
- Nothing is done without a runnable check.
- Secrets live in `blimp-infra/secrets/` and never in git.
