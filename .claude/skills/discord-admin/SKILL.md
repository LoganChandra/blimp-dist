---
name: discord-admin
description: Use before any Discord server change - channels, roles, permissions, auto-mod, events, webhooks, embeds, rate limits. The shared reference for what the API allows, our guild's real state, and dated changes (channel obfuscation 2026-11-16).
---

# Discord admin

Read, in order, only what you need:
1. `docs/discord/01-api-reference.md` - doc-verified limits, permission bits, auto-mod, events, rate limits, dated changes.
2. `docs/discord/02-product-and-our-server.md` - our live guild state and what we could turn on.

Rules:
- Frank is Administrator. Access is not authorization: permission or access-control changes go to the Boss first.
- Re-check live state with the API before acting; the doc is dated 2026-11-07.
- Anything marked "not doc-verified" is memory: verify with a search before promising it.
- List before you delete. `build_hq.py` removes channels not in its lists.
- On HTTP 429 back off by `Retry-After`; never loop.
- The 2026-11-16 channel-listing change may hide channels from `GET /guilds/{id}/channels`: re-test after it lands.
- No emoji, no italics in anything we post.
