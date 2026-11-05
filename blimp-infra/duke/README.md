---
topic: duke-claude-code-config
audience: the Boss + Cowork + Duke + restore script
last_updated: 2026-05-27
---

# Duke — Claude Code config templates

Files in this directory are templates of Duke's `~/.claude/` setup, captured
into the repo so they survive a PC loss. The `restore-from-repo.sh` master
bootstrap copies them into place after Claude Code is installed.

## Why these matter

Without them, on a fresh install Duke:
- Doesn't know "Discord is the Boss's only readable channel" → would try to reply
  in the terminal and the Boss would never see his responses
- Doesn't have `bypassPermissions` mode → would prompt for every tool call, which
  hangs the Discord-driven flow (the prompts go to terminal the Boss can't see)
- Doesn't have the right MCP plugins enabled

Captured 2026-05-27 after a Duke + Hermes audit flagged these as gaps in
the disaster-recovery flow. See `LESSONS-LEARNED.md` for the full story.

## What each file is

| Template | Installs to | What it controls |
|---|---|---|
| `CLAUDE.md.template` | `~/.claude/CLAUDE.md` | Machine-wide global instructions — Discord-as-only-channel rule, ACCESS_TIERS reference, the 8 hard rails |
| `settings.json.template` | `~/.claude/settings.json` | Global settings — enabled plugins (discord, supabase, vercel, github, zapier), effort level, theme, skipAutoPermissionPrompt |
| `settings.local.json.template` | `~/.claude/settings.local.json` | Local overrides — `bypassPermissions` default mode + explicit tool allowlist including MCP methods |

## Install during bootstrap

The master `scripts/restore-from-repo.sh` runs this automatically after Claude
Code OAuth completes. Manual install if needed:

```bash
mkdir -p ~/.claude
cp blimp-infra/duke/CLAUDE.md.template            ~/.claude/CLAUDE.md
cp blimp-infra/duke/settings.json.template        ~/.claude/settings.json
cp blimp-infra/duke/settings.local.json.template  ~/.claude/settings.local.json
```

## What this doesn't capture (still per-machine, must be redone)

- **Claude Code OAuth login** — first launch needs `claude` and a browser
- **`~/.claude/ACCESS_TIERS.md`** — the Boss's personal access-tier doc (referenced
  from CLAUDE.md but not captured here yet). If you have it on the old machine,
  copy it manually. Otherwise build it fresh.
- **`~/.claude/projects/<sanitized-cwd>/memory/`** — Duke's auto-memory (per-project
  files like `user_*.md`, `project_*.md`). Manual carry-over if migrating PCs.
- **`~/.claude/channels/discord/`** — Discord channels-plugin state (channel pairings,
  approval records). Re-pair Duke with `#duke` via the plugin's `!pair` flow.

## If Duke's behavior seems off after a fresh install

Quick checks:

```bash
# 1) Is the global CLAUDE.md installed?
head -3 ~/.claude/CLAUDE.md
# Should print: # Machine-wide CLAUDE.md (the Boss's PC)

# 2) Is bypassPermissions on?
grep defaultMode ~/.claude/settings.local.json
# Should print: "defaultMode": "bypassPermissions",

# 3) Are the MCP plugins enabled?
grep -c "@claude-plugins-official" ~/.claude/settings.json
# Should print: 5

# 4) Is blimp-brain MCP registered?
claude mcp list | grep blimp-brain
# Should print: blimp-brain: ... ✓ Connected
```

If any of those fails, re-run the relevant section of `restore-from-repo.sh`
or just re-copy the template.
