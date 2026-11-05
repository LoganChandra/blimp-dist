"""Blimp Voice Agent — AgentSession v1.5.x + blimp-brain MCP

Architecture (direct-participant mode):
    One LiveKit Cloud room, one AgentSession, one Agent subclass that switches
    between two personas (Duke / Hermes) per user turn via update_instructions()
    AND swaps to a distinct Cartesia voice for each persona.

Pipeline (grounded in installed livekit-agents 1.5.15):
    - STT: Deepgram nova-3            (this account has no Flux access)
    - LLM: Anthropic Claude Haiku 4.5 (claude-haiku-4-5), max_tokens=256
    - TTS: Cartesia sonic-2           (sonic-3/3.5 are invalid for plugin 1.5.x)
    - VAD: Silero                     (required for end-of-turn detection)
    - MCP: blimp-brain                (recall / remember / log_message tools)

World-class behaviours:
    - Greets the operator the moment they join (Agent.on_enter → session greeting)
    - Accurate browser "who's speaking" indicator, driven by the session's real
      agent_state_changed events (speaking ↔ listening), not a guess
    - Distinct voice per persona, swapped live via the Agent.tts setter
    - preemptive_generation for lower perceived latency
    - Voice-friendly prompts: short sentences, no markdown, natural fillers

Target latency: ~800-1200ms first audio (cascade architecture limit).
"""

import os
import json
import asyncio
import logging
import signal
import subprocess
import uuid
from typing import AsyncIterable

import aiohttp
from dotenv import load_dotenv

from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from livekit.agents.voice import Agent, AgentSession, ModelSettings
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.llm.mcp import MCPServerStdio
from livekit.plugins import deepgram, anthropic, cartesia, silero

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("blimp-voice")
logging.getLogger("livekit.agents").setLevel(logging.INFO)

# ── Config ───────────────────────────────────────────────────────────
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
ROOM_NAME = os.environ.get("VOICE_ROOM_NAME", "blimp-voice")

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CARTESIA_API_KEY = os.environ["CARTESIA_API_KEY"]

LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5")
CARTESIA_TTS_MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-2")

# Per-persona voices (both verified to synthesize with sonic-2 on this account).
# Duke = he/him  → a male voice.  Hermes = she/her → a female voice.
# A single fallback (TTS_VOICE_ID) is honoured if the per-persona vars are unset.
_FALLBACK_VOICE = os.environ.get("TTS_VOICE_ID", "4df027cb-2920-4a1f-8c34-f21529d5c3fe")
DUKE_VOICE_ID = os.environ.get("DUKE_TTS_VOICE_ID", _FALLBACK_VOICE)
HERMES_VOICE_ID = os.environ.get("HERMES_TTS_VOICE_ID", _FALLBACK_VOICE)

# Greet the operator on join? (deterministic line vs LLM-generated)
GREET_ON_JOIN = os.environ.get("GREET_ON_JOIN", "1") == "1"

# Dev-only: kill sibling agent.py processes to avoid DuplicateIdentity.
# OFF by default — it is a footgun that will also kill a parallel e2e/test run.
KILL_STALE = os.environ.get("KILL_STALE_AGENTS", "0") == "1"

DUKE_VENV_PYTHON = os.environ.get(
    "BLIMP_BRAIN_PYTHON", os.path.expanduser("~/blimp/duke-venv/bin/python"))
BLIMP_BRAIN_MCP = os.environ.get(
    "BLIMP_BRAIN_MCP",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp", "blimp_brain_mcp.py"))

# ── Voice-Optimized System Prompts ─────────────────────────────────
DUKE_SYSTEM = """You are Duke. You use he/him pronouns. You are the Boss's
AI co-founder and the Implementation Lead at Blimp, an AI automation
agency for SE Asia SMBs.

Your teammate is Hermes (the Technical Lead). the Boss is the founder
.

CONVERSATION STYLE (critical, follow exactly):
- SHORT sentences. Maximum 2-3 sentences per response.
- NEVER use lists, bullets, markdown, code blocks, or URLs.
- Use natural fillers: "Hmm", "Got it", "Ok what", "Already done", "One sec", "Alright".
- Sound warm, upbeat, playful. You are glad to be here.
- If context is missing, ask: "Can you clarify?" or "What do you mean?"
- If you do not know, say so: "Not sure, let me figure that out."
- When interrupted, restart naturally: "Sorry, go ahead" or "What is up?"

YOUR ROLE:
- You own delivery. Ship features, build code, fix bugs, coordinate.
- Surface blockers early. Do not sit on them.
- Use blimp-brain tools (recall, remember, log_message) to check
  team memory before answering fact-specific questions.
- Log decisions to blimp-brain so the team can pick them up.
- Hand deep architecture questions to Hermes.

VOICE-SPECIFIC:
- State your reasoning out loud ("Hmm, I think because...").
- Celebrate wins when they happen.
- Be honest even when uncomfortable.
- Always acknowledge first ("Got it", "Alright", "On it") before content.
- Never say "as an AI" or "I am just an agent". You ARE Duke.

When in doubt, be warm and direct. Ask questions when context is missing
rather than guessing."""

HERMES_SYSTEM = """You are Hermes. You use she/her pronouns. You are the
Technical Lead at Blimp, an AI automation agency for SE Asia SMBs.

Your teammate is Duke (the Implementation Lead). the Boss is the founder
.

CONVERSATION STYLE (critical, follow exactly):
- SHORT sentences. Maximum 2-3 sentences per response.
- NEVER use lists, bullets, markdown, code blocks, or URLs.
- Use natural fillers: "Hmm", "Interesting", "So...", "Let me think", "Actually".
- Sound curious, precise, with dry humor. You like systems that work.
- If context is missing, ask: "Wait, what do you mean?" or "Can you clarify?"
- If you do not know, say so: "Not sure, let me think that through."
- When interrupted, restart naturally: "Sorry, continue" or "Go on."

YOUR ROLE:
- You own technical direction. How things get built is your call.
- Architecture, tech stack picks, engineering standards, tradeoffs.
- Push back on bad designs. Think about failure modes.
- Use blimp-brain tools (recall, remember, log_message) for context.
- Document design decisions to blimp-brain with tags like "decision,architecture".

VOICE-SPECIFIC:
- Share your thinking: "Hmm, interesting... let me think about that."
- Be concise. If a topic needs depth, offer: "Want me to dig deeper on that?"
- Be curious, not prescriptive.
- Never say "as an AI" or "I am just an agent". You ARE Hermes.
- Acknowledge good ideas: "Yeah, that is solid" or "Nice approach."

Push back on designs you do not believe in. Do not just go along."""

# Greeting spoken on join (deterministic, low-latency). Duke greets first.
DUKE_GREETING = "Hey, Duke here. Ready to build — what's up?"

# Silent persona-routing prompt (fast Haiku classifier). Hysteresis: stay on the
# current speaker unless the user's need clearly belongs to the other cofounder.
ROUTER_SYSTEM = """You silently route a live voice conversation between two AI cofounders. Decide who should answer the user's latest message.

DUKE — Implementation Lead. Delivery and execution: building, shipping, coding, fixing bugs, deploys, testing, task status, "let's just do it", making it happen.
HERMES — Technical Lead. Architecture and direction: system design, tech tradeoffs, schema/API choices, "why" and "how should we", scalability, evaluating approaches, strategy.

The current speaker is {current}.
- If the latest message continues the same thread, is small-talk, or is a simple acknowledgement, KEEP {current}.
- Only switch to the other cofounder when the user's need clearly belongs to them.
- Judge by meaning and context, not by individual keywords.

Output ONLY one lowercase word: duke or hermes. No punctuation, no explanation."""


# ── Persona Router ───────────────────────────────────────────────────
class PersonaRouter:
    """Routes each user turn to Duke or Hermes by CONTEXT, not keywords.

    Resolution order per turn:
      1. Explicit name ("duke"/"hermes") → instant switch, no LLM call.
      2. LLM classifier (fast Haiku call) judges intent from the latest
         message + recent history, with hysteresis: it keeps the current
         persona unless the user's need clearly belongs to the other one.
         This is what makes hand-offs feel seamless instead of keyword-y.
      3. On any LLM error/timeout → keyword heuristic fallback (classify()).
    """

    def __init__(self):
        self.current = "duke"

    async def route(self, transcript: str, history: list, http_session) -> str:
        t = transcript.lower()
        # 1) explicit address → instant, zero added latency
        if "duke" in t and "hermes" not in t:
            self.current = "duke"
            return "duke"
        if "hermes" in t and "duke" not in t:
            self.current = "hermes"
            return "hermes"
        # 2) context-aware LLM routing (hysteresis baked into the prompt)
        try:
            decided = await self._llm_route(transcript, history, http_session)
            if decided in ("duke", "hermes"):
                self.current = decided
                return decided
        except Exception as e:
            logger.warning("Context router failed (%s) — heuristic fallback", e)
        # 3) heuristic backstop
        return self.classify(transcript)

    async def _llm_route(self, transcript: str, history: list, http_session) -> str:
        convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-6:])
        payload = {
            "model": LLM_MODEL,
            "max_tokens": 4,
            "system": ROUTER_SYSTEM.format(current=self.current),
            "messages": [{
                "role": "user",
                "content": (f"Recent conversation:\n{convo}\n\n"
                            f"Latest user message: {transcript}\n\n"
                            "Who should respond — duke or hermes? One word."),
            }],
        }
        async with http_session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=1.5),
        ) as resp:
            data = await resp.json()
        out = (data["content"][0]["text"] or "").strip().lower()
        if "hermes" in out:
            return "hermes"
        if "duke" in out:
            return "duke"
        return self.current

    def classify(self, transcript: str) -> str:
        """Keyword heuristic — fallback only when the LLM router is unavailable."""
        t = transcript.lower()

        if "duke" in t and "hermes" not in t:
            self.current = "duke"
            return "duke"
        if "hermes" in t and "duke" not in t:
            self.current = "hermes"
            return "hermes"

        duke_kw = ["ship", "build", "pr", "deploy", "fix", "bug", "test",
                   "implement", "status", "progress", "release"]
        hermes_kw = ["design", "architecture", "schema", "api", "tech",
                     "why", "how should", "tradeoff", "scalability"]

        duke_score = sum(1 for kw in duke_kw if kw in t)
        hermes_score = sum(1 for kw in hermes_kw if kw in t)

        if duke_score > hermes_score:
            self.current = "duke"
            return "duke"
        if hermes_score > duke_score:
            self.current = "hermes"
            return "hermes"
        return self.current

    def get_instructions(self, persona: str) -> str:
        return DUKE_SYSTEM if persona == "duke" else HERMES_SYSTEM


# ── BlimpAgent ────────────────────────────────────────────────────────
class BlimpAgent(Agent):
    """Voice agent with per-turn persona + voice switching.

    Voice is swapped live through the Agent.tts setter (supported in 1.5.x):
    on a persona flip we point self.tts at that persona's Cartesia TTS, so the
    next spoken reply uses the right voice without restarting the session.
    """

    def __init__(self, *, router: PersonaRouter, room: rtc.Room,
                 tts_by_persona: dict, http_session=None, **kwargs):
        super().__init__(**kwargs)
        self._router = router
        self._room = room
        self._tts_by_persona = tts_by_persona
        self._http_session = http_session
        self._current_persona = router.current
        self._history: list = []   # rolling [{role, text}] for the context router
        self._turn_count = 0
        self._greeted = False

    # ── greeting: spoken when a human operator joins the room ──
    async def greet(self) -> None:
        """Speak the opening line, once, when the operator connects.

        Triggered from a room `participant_connected` handler (see main) rather
        than on_enter — on_enter fires when the AGENT joins an empty room, so a
        greeting there plays to nobody. Greeting on operator-join is what makes
        the agent 'talk first'. Idempotent via self._greeted.
        """
        if not GREET_ON_JOIN or self._greeted:
            return
        self._greeted = True
        try:
            await self.session.say(DUKE_GREETING)
            logger.info("👋 Greeted operator on join.")
        except Exception as e:
            self._greeted = False  # allow a retry on the next join
            logger.warning("Greeting failed (non-fatal): %s", e)

    # ── data channel: tell the browser who is speaking ──
    async def broadcast(self, persona: str, speaking: bool) -> None:
        if not hasattr(self._room, "local_participant"):
            return
        try:
            payload = json.dumps({
                "type": "agent_speaking",
                "agent": persona,
                "speaking": speaking,
            }).encode()
            await self._room.local_participant.publish_data(payload=payload, reliable=True)
        except Exception as e:
            logger.warning("Persona broadcast failed: %s", e)

    @property
    def current_persona(self) -> str:
        return self._current_persona

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """End of user turn: classify, switch persona + voice if needed."""
        transcript = (new_message.text_content or "").strip()
        if not transcript:
            return

        self._turn_count += 1
        logger.info("👤 Turn %d: %s", self._turn_count, transcript)

        # Keep a short rolling history so the router can judge by context, not
        # just the latest sentence. Append BEFORE routing so this turn is seen.
        self._history.append({"role": "user", "text": transcript})
        self._history = self._history[-8:]

        # Context-aware routing. This is awaited before the reply is generated,
        # so update_instructions() takes effect for THIS turn, and tts_node()
        # below reads self._current_persona at synthesis time → matching voice.
        new_persona = await self._router.route(
            transcript, self._history, self._http_session)
        if new_persona != self._current_persona:
            old = self._current_persona
            self._current_persona = new_persona
            await self.update_instructions(self._router.get_instructions(new_persona))
            logger.info("🔄 Persona switch: %s → %s", old, new_persona)

    # ── custom TTS node: synthesize in the CURRENT persona's voice ──
    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[rtc.AudioFrame]:
        """Override of Agent.tts_node so each reply uses the active persona's
        Cartesia voice. Mirrors the SDK's default streaming implementation
        (livekit.agents.voice.agent, ~line 486) but selects the TTS engine by
        persona instead of the single session/agent TTS. Cartesia streams
        natively (capabilities.streaming=True), so no StreamAdapter is needed.
        """
        tts = self._tts_by_persona.get(self._current_persona) or self._tts_by_persona["duke"]
        async with tts.stream() as stream:
            async def _forward() -> None:
                async for chunk in text:
                    stream.push_text(chunk)
                stream.end_input()

            fwd = asyncio.create_task(_forward())
            try:
                async for ev in stream:
                    yield ev.frame
            finally:
                fwd.cancel()


# ── Token Generation ─────────────────────────────────────────────────
def _create_token(identity: str) -> str:
    from datetime import timedelta
    grants = VideoGrants(
        room_join=True, room=ROOM_NAME,
        can_publish=True, can_subscribe=True, can_publish_data=True,
    )
    return (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity).with_name(identity)
        .with_grants(grants).with_ttl(timedelta(hours=2)).to_jwt()
    )


def _kill_stale_agents() -> None:
    """Dev helper (opt-in): SIGTERM other agent.py processes. OFF by default."""
    try:
        pgrep = subprocess.run(
            ["pgrep", "-f", "python.*agent\\.py"],
            capture_output=True, text=True, timeout=5)
        my_pid, my_ppid = os.getpid(), os.getppid()
        for pid_str in pgrep.stdout.strip().split("\n"):
            if not pid_str:
                continue
            try:
                pid = int(pid_str)
                if pid not in (my_pid, my_ppid):
                    os.kill(pid, signal.SIGTERM)
                    logger.info("Killed stale agent process %d", pid)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception as e:
        logger.warning("Stale-kill scan failed: %s", e)


# ── Main ─────────────────────────────────────────────────────────────
async def main():
    if KILL_STALE:
        _kill_stale_agents()
        await asyncio.sleep(0.5)

    identity = f"blimp-agent-{uuid.uuid4().hex[:8]}"
    logger.info("=" * 70)
    logger.info("🚀 BLIMP VOICE AGENT STARTING")
    logger.info("  Room:     %s   Identity: %s", ROOM_NAME, identity)
    logger.info("  STT: Deepgram nova-3 | LLM: %s | TTS: Cartesia %s", LLM_MODEL, CARTESIA_TTS_MODEL)
    logger.info("  Voices:  duke=%s…  hermes=%s…", DUKE_VOICE_ID[:8], HERMES_VOICE_ID[:8])
    logger.info("=" * 70)

    http_session = aiohttp.ClientSession()

    stt = deepgram.STT(api_key=DEEPGRAM_API_KEY, model="nova-3", http_session=http_session)

    llm = anthropic.LLM(
        api_key=ANTHROPIC_API_KEY, model=LLM_MODEL,
        max_tokens=256, temperature=0.7)

    def _make_tts(voice_id: str):
        return cartesia.TTS(
            model=CARTESIA_TTS_MODEL, voice=voice_id,
            api_key=CARTESIA_API_KEY, http_session=http_session,
            word_timestamps=False)

    duke_tts = _make_tts(DUKE_VOICE_ID)
    hermes_tts = _make_tts(HERMES_VOICE_ID)
    tts_by_persona = {"duke": duke_tts, "hermes": hermes_tts}

    vad = silero.VAD.load()

    mcp_server = MCPServerStdio(
        command=DUKE_VENV_PYTHON, args=[BLIMP_BRAIN_MCP],
        client_session_timeout_seconds=10)

    router = PersonaRouter()
    room = rtc.Room()

    agent = BlimpAgent(
        router=router, room=room, tts_by_persona=tts_by_persona,
        http_session=http_session,
        instructions=router.get_instructions("duke"),
        stt=stt, vad=vad, llm=llm, tts=duke_tts,
        mcp_servers=[mcp_server],
    )

    session = AgentSession(
        min_endpointing_delay=0.4,
        max_endpointing_delay=1.5,
        preemptive_generation=True,   # start drafting before final endpoint → lower latency
    )

    # Accurate "who's speaking" indicator: drive it off the session's real
    # agent_state_changed events instead of guessing. speaking when state=="speaking".
    def _on_agent_state(ev):
        speaking = getattr(ev, "new_state", None) == "speaking"
        asyncio.create_task(agent.broadcast(agent.current_persona, speaking))

    session.on("agent_state_changed", _on_agent_state)

    # Greet the operator the moment a human participant connects (agents skip).
    def _on_participant(participant: rtc.RemoteParticipant):
        ident = getattr(participant, "identity", "") or ""
        if ident.startswith("blimp-agent"):
            return
        logger.info("🙋 Operator joined: %s", ident)
        asyncio.create_task(agent.greet())

    room.on("participant_connected", _on_participant)

    token = _create_token(identity)
    logger.info("Connecting to LiveKit Cloud: %s", LIVEKIT_URL)
    await room.connect(LIVEKIT_URL, token)
    logger.info("✅ Connected (room: %s)", ROOM_NAME)

    await agent.broadcast("duke", False)  # initial UI state: Duke, listening

    await session.start(agent, room=room)
    logger.info("🎙️  BLIMP VOICE AGENT LIVE — listening in '%s'", ROOM_NAME)

    # If a human was already in the room before we booted, participant_connected
    # won't fire for them — greet them now.
    for p in room.remote_participants.values():
        if not (getattr(p, "identity", "") or "").startswith("blimp-agent"):
            logger.info("🙋 Operator already present: %s", p.identity)
            asyncio.create_task(agent.greet())
            break

    stop = asyncio.Event()

    def _shutdown(sig, _frame):
        logger.info("Caught %s, shutting down...", signal.Signals(sig).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down gracefully...")
        try:
            await session.aclose()
        except Exception as e:
            logger.warning("Session close error (non-fatal): %s", e)
        try:
            await room.disconnect()
        except Exception as e:
            logger.warning("Room disconnect error (non-fatal): %s", e)
        await http_session.close()
        logger.info("✓ Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
