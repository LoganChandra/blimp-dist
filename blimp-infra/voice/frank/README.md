---
topic: Frank Voice — ChatGPT-voice-style talk-to-Frank
audience: the Boss, Duke, Frank
summary: Tap-the-orb voice chat with Frank on the Boss's PC; browser speech in/out, one headless claude -p turn per utterance, no new keys, zero idle spend.
last_updated: 2026-10-08
---

# Frank Voice

Talk to Frank out loud, ChatGPT-Advanced-Voice style: tap the orb, talk, he
answers out loud, talk over him to cut in.

```
./run.sh            # then open http://127.0.0.1:8791 in Chrome or Edge on this PC
```

## How it works

| Piece | What | Cost |
|---|---|---|
| Ears | Browser Web Speech API (`SpeechRecognition`), continuous + interim results | free, no key |
| Brain | `server.py` spawns one headless `claude -p` per turn in `~/nanook/repo`, so Frank's CLAUDE.md/persona loads. `--resume` carries the conversation; the reply streams back as SSE | Claude Code turns Frank already uses; nothing between turns |
| Mouth | Browser `speechSynthesis`, spoken sentence by sentence as text streams in | free, no key |

- **Barge-in:** speaking while Frank talks stops him, and the in-flight
  `claude -p` is killed server-side.
- **Echo guard:** on speakers, the mic hears Frank; speech that mostly matches
  his own words is ignored. Headphones are still best.
- **Memory:** each finished turn is logged to the shared brain (channel
  `nanook`, prefixed `[voice]`) so Frank's Discord session can recall it.
  `FRANK_VOICE_BRAIN_LOG=0` turns it off.
- **New conversation** button = fresh session (Frank forgets the call context).
- **Typed fallback** box for when talking isn't practical.

## Rails

- Binds `127.0.0.1` only (hard-coded, no flag). Same-origin JSON POSTs only, so
  other web pages can't drive Frank.
- **Talk-only:** voice-mode Frank gets no tools (`--tools ""`, `--strict-mcp-config`).
  A misheard sentence can never run a command. Opt in later via `FRANK_VOICE_TOOLS`.
- Zero idle spend: no process, socket or poller between turns. Each turn has a
  120s hard timeout.
- No new services or keys. Note: Chrome's recognizer sends mic audio to Google
  (Edge: Microsoft) to transcribe — same as any Chrome voice typing.

## Config (env)

`FRANK_VOICE_PORT` (8791) · `FRANK_VOICE_MODEL` (sonnet; haiku tested slower and
less accurate) · `FRANK_VOICE_CWD` (~/nanook/repo) · `FRANK_VOICE_TOOLS` ("") ·
`FRANK_VOICE_TURN_TIMEOUT` (120) · `FRANK_VOICE_BRAIN_LOG` (1).

## Tests

`./run-e2e.sh` runs 11 offline server tests (fake `claude`: streaming, resume,
reset, origin gate, barge-in kill, stale-cancel race, tools-exposed rail, loopback bind), then a headless-Chromium
E2E against real Frank with scripted fake speech APIs: voice turn → spoken
reply → listening, multi-turn memory, echo ignored, barge-in, typed turn.

Not covered by automation: real microphone recognition and real speaker
output (headless Chromium has neither); needs a human pass in Chrome/Edge.

## Limits (v1)

- Time to first spoken word: about 4-6s typical, up to about 10s (it's
  model time-to-first-token on Frank's full context). Slower than ChatGPT voice.
- Same-PC only (localhost mic permission). Phone access would need a private
  tunnel (e.g. Tailscale) — not done, it's a rail call for the Boss.
- Browser voices vary; pick one in the dropdown (Edge's "Natural" voices sound best).

The older LiveKit Duke+Hermes scrum room in `../` is untouched; it needs
Deepgram/Cartesia/LiveKit keys this one avoids.
