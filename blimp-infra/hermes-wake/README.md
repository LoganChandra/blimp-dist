---
topic: hermes-wake — wake Hermes and hand him a task on the Boss's behalf
audience: Duke, Frank, the Boss
summary: Signed webhook wake for Hermes's always-on gateway (the Hermes analogue of duke-up + tmux typing), D-008-gated and budget-guarded; route activation pending the Boss.
last_updated: 2026-10-08
---

# hermes-wake

The Boss asked for a reliable way for one agent to
wake Hermes and hand him a task, the way Frank woke Duke (`duke-up` + typing into
the `duke` tmux pane).

## Why not tmux

Hermes isn't a tmux session. He runs as `hermes-gateway.service`, always on, with
a loopback webhook listener on `127.0.0.1:8644`. The gateway's native mechanism
is a **webhook route**: a signed POST to `/webhooks/<route>` runs one Hermes turn
on a templated prompt and delivers his reply to a Discord channel.

```
Duke/Frank --hermes_wake.py--> POST 127.0.0.1:8644/webhooks/blimp-wake (HMAC V2, timestamped)
                                   |  gate.py (in gateway): D-008 + sender + caps + budget
                                   v
                          one Hermes turn on the handoff --> reply posted in #hermes
            + brain log (channel `hermes`) so his next full session sees the handoff
```

## Usage

```
blimp-infra/hermes-wake/hermes_wake.py --from frank \
  --logan-when "2026-01-01 09:00 #frank" \
  --task "Design X. Context: ..."
```

If the gateway is down it starts `hermes-gateway.service` first, like `duke-up`.
Exit codes: 0 accepted · 2 refused locally · 3 gateway down · 4 rejected by gateway.
It isn't installed on PATH (rail 7), so call it by path.

## Rules enforced (both in the CLI and again server-side in `gate.py`)

- **D-008:** agents never wake each other on their own. A wake must relay
  the Boss's words verbatim (`--logan-said`, C5). Peer-only asks go via A2A or the board.
- Known senders only: duke, frank/nanook, logan. Hermes can't wake himself.
- **D-009 budget:** Hermes bills OpenRouter, so the gate allows at most 6 wakes per rolling
  hour (`BLIMP_WAKE_MAX_PER_HOUR`). Idle cost is zero: nothing runs between wakes.
- Loopback only, HMAC V2 signature with a 5-min replay window, and gateway
  idempotency on `X-Request-ID`.

## Status: route NOT activated (needs the Boss)

Registering the `blimp-wake` route on Hermes's gateway (secret, gate script in
`~/.hermes/scripts/`, Discord delivery to #hermes) is a standing config
change that gives Hermes a new authenticated trigger. Duke's attempt to script
it was blocked by the permission classifier as an account/standing-rule
change, so it was not done. Until it is, the CLI exits 4 "Unknown route"
(verified against the live gateway).

**Second decision for the Boss:** webhook turns get Hermes's deliberately
constrained `hermes-webhook` toolset (web search/extract, vision, clarify). He
can acknowledge, plan and research, but can't touch the brain, files or shell in
the woken turn. Hermes's own design makes widening that a manual-only
`toolsets` edit, so one agent can't elevate another. Options: keep it
constrained (the woken turn acks and plans, real work happens in his next full
session), or grant his normal `hermes-discord` toolset on this one route.

## Tests

`python3 test_hermes_wake.py` runs 8 offline tests: gate (D-008 drop, sender
allowlist with no self-wake, field caps, extra-field stripping, budget guard), CLI
(D-008 refusal, signature byte-identical to the gateway's V2 verifier,
idempotency header, gateway-down exit). Not tested: a live Hermes turn, because
the route isn't activated.
