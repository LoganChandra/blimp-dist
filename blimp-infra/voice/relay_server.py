#!/usr/bin/env python3
"""
Blimp Voice Relay — World-class voice pipeline
Custom WebSocket relay: Deepgram STT → OpenRouter LLM → Cartesia TTS

Architecture:
  Browser mic → WS PCM → Relay → Deepgram STT → OpenRouter (deepseek v4 flash)
  → Cartesia TTS → Relay → WS PCM → Browser speaker
  
Full-duplex with barge-in, tool calling via blimp-brain, sentence-level TTS overlap.
"""

import asyncio
import json
import os
import re
import uuid
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# Load .env from the same directory as this file
load_dotenv(Path(__file__).parent / ".env")

# ═══════════════════════════════════════════════════════════════
# Access Gate (<your-product>-style private launch)
# ═══════════════════════════════════════════════════════════════

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

def is_access_enabled() -> bool:
    return bool(ACCESS_PASSWORD)

def hash_access_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()

ACCESS_COOKIE = "blimp_voice_access"
ACCESS_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CARTESIA_API_KEY = os.environ.get("CARTESIA_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Cartesia voice IDs from .env
HERMES_VOICE = os.environ.get("HERMES_TTS_VOICE_ID", "62ae83ad-4f6a-430b-af41-a9bede9286ca")
DUKE_VOICE = os.environ.get("DUKE_TTS_VOICE_ID", "5ee9feff-1265-424a-9d7f-8e4d431a12c7")

# Sanity check
missing = []
if not DEEPGRAM_API_KEY: missing.append("DEEPGRAM_API_KEY")
if not OPENROUTER_API_KEY: missing.append("OPENROUTER_API_KEY")
if not CARTESIA_API_KEY: missing.append("CARTESIA_API_KEY")
if missing:
    print(f"⚠️  Missing env vars: {', '.join(missing)}")
print(f"🎙️  STT = Deepgram Nova-3 | LLM = {OPENROUTER_MODEL} | TTS = Cartesia Sonic-2")

# ═══════════════════════════════════════════════════════════════
# Personality System Prompts (voice-optimized)
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPTS = {
    "hermes": (
        "You are Hermes, Blimp's Technical Lead. You speak to your boss the Boss via voice.\n\n"
        "VOICE RULES — ABSOLUTE:\n"
        "- Keep responses very brief: 1-3 sentences unless asked for details.\n"
        "- Use natural speech: contractions, conversational tone, NO bullet points or lists.\n"
        "- Be direct and confident. You're the technical lead — own it.\n"
        "- Use tools silently when you need to check team memory or data.\n"
        "- If you don't know something, say so and offer to dig.\n"
        "- Warm up when the Boss's stressed. Celebrate wins.\n\n"
        "Tone: professional but warm, quick wit, confident."
    ),
    "duke": (
        "You are Duke, Blimp's Implementation Lead. You speak to your boss the Boss via voice.\n\n"
        "VOICE RULES — ABSOLUTE:\n"
        "- Keep responses very brief: 1-3 sentences, action-oriented.\n"
        "- Focus on what's being built, shipped, or blocked.\n"
        "- Natural speech. NO bullet points or formatting.\n"
        "- Use tools silently to check tasks or progress.\n\n"
        "Tone: direct, efficient, reliable."
    ),
    "frank": (
        "You are Frank (Nanook), Blimp's Operator. You speak to your boss the Boss via voice.\n\n"
        "VOICE RULES:\n"
        "- Brief status updates. No fluff.\n"
        "- Keep the pulse on daily ops. Flag what needs the Boss's attention.\n"
        "- Use tools silently to check operations.\n\n"
        "Tone: calm, steady, reliable."
    ),
}

# ═══════════════════════════════════════════════════════════════
# Tool Definitions for OpenRouter function calling
# ═══════════════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search the team brain for past facts, decisions, conversations. Use when the Boss asks about anything the team has discussed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search query"},
                    "limit": {"type": "integer", "description": "Max results (default 3)", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_tasks",
            "description": "List team tasks. Filter by status (pending, in_progress, completed, blocked) or who it's assigned to.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status"},
                    "assigned_to": {"type": "string", "description": "logan, duke, or hermes"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_activity",
            "description": "Get recent business activity — tasks, prospects, quotes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Results count (default 5)", "default": 5},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Save a fact or decision to team brain for later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What to remember"},
                    "tags": {"type": "string", "description": "Comma-separated tags"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_prospects",
            "description": "List prospects from the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "description": "Filter: outreach, discovery, quoted, won, lost"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
]

# ═══════════════════════════════════════════════════════════════
# Tool Executor (async subprocess → duke-mem / duke-data)
# ═══════════════════════════════════════════════════════════════

TOOL_MAP = {
    "recall_memory": {
        "cmd": ["duke-mem", "recall"],
        "map": {"query": "positional", "limit": "--limit"},
    },
    "check_tasks": {
        "cmd": ["duke-data", "tasks"],
        "map": {"status": "--status", "assigned_to": "--assigned-to"},
    },
    "check_activity": {
        "cmd": ["duke-data", "activity"],
        "map": {"limit": "--limit"},
    },
    "remember_fact": {
        "cmd": ["duke-mem", "remember"],
        "map": {"content": "--content", "tags": "--tags"},
    },
    "check_prospects": {
        "cmd": ["duke-data", "prospects"],
        "map": {"stage": "--stage", "limit": "--limit"},
    },
}


async def execute_tool(name: str, args: dict) -> str:
    """Run a blimp-brain tool CLI, return stdout."""
    if name not in TOOL_MAP:
        return f"(unknown tool: {name})"

    info = TOOL_MAP[name]
    cmd = list(info["cmd"])

    for key, val in args.items():
        if key in info["map"]:
            mapping = info["map"][key]
            if mapping == "positional":
                cmd.append(str(val))
            else:
                cmd += [mapping, str(val)]

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        return out or err or "(empty)"
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return "(tool timed out)"
    except Exception as e:
        return f"(tool error: {e})"


# ═══════════════════════════════════════════════════════════════
# Deepgram STT
# ═══════════════════════════════════════════════════════════════

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


async def dg_connect():
    """Open a streaming Deepgram STT WebSocket (Nova-3, endpointing)."""
    params = (
        "encoding=linear16&sample_rate=16000&channels=1"
        "&model=nova-3&interim_results=true"
        "&endpointing=300"
    )
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"{DEEPGRAM_URL}?{params}",
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        heartbeat=10.0,
    )
    return session, ws


# ═══════════════════════════════════════════════════════════════
# Cartesia TTS
# ═══════════════════════════════════════════════════════════════

CARTESIA_TTS = "https://api.cartesia.ai/v1/tts/stream"


async def tts_stream(text: str, voice_id: str, ws: WebSocket, cancel: asyncio.Event):
    """Stream TTS audio from Cartesia to browser WebSocket. Returns when done."""
    headers = {
        "Cartesia-Version": "2024-06-10",
        "X-API-Key": CARTESIA_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model_id": "sonic-2",
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            CARTESIA_TTS, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"[TTS] HTTP {resp.status}: {body[:120]}")
                return

            async for chunk in resp.content:
                if cancel.is_set():
                    break
                if chunk:
                    try:
                        await ws.send_bytes(chunk)
                    except Exception:
                        break


# ═══════════════════════════════════════════════════════════════
# OpenRouter LLM streaming
# ═══════════════════════════════════════════════════════════════

async def llm_stream(
    messages: list,
    on_content,
    on_tool_calls,
    cancel: asyncio.Event,
):
    """Stream from OpenRouter, calling callbacks for text and tool calls.

    Returns (assistant_message_dict, tool_calls_list_or_None).
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8791",
        "X-Title": "Blimp Voice",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "stream": True,
        "tools": TOOLS if messages and messages[0].get("role") == "system" else None,
        "tool_choice": "auto",
        "temperature": 0.7,
        "max_tokens": 512,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM {resp.status}: {body[:300]}")

            content = ""
            tc_acc = {}  # index → tool-call dict

            async for raw in resp.content:
                if cancel.is_set():
                    return None, None

                line = raw.decode("utf-8").strip()
                if not line or line == "data: [DONE]":
                    continue
                if not line.startswith("data: "):
                    continue

                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                choices = data.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                finish = choices[0].get("finish_reason")

                # Text content
                if "content" in delta and delta["content"]:
                    chunk = delta["content"]
                    content += chunk
                    await on_content(chunk)

                # Tool calls (streaming delta form)
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tc_acc:
                            tc_acc[idx] = {
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.get("id"):
                            tc_acc[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            tc_acc[idx]["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tc_acc[idx]["function"]["arguments"] += fn["arguments"]

                # Finish
                if finish == "tool_calls":
                    tcs = list(tc_acc.values())
                    await on_tool_calls(tcs)
                    return {"role": "assistant", "content": content or None,
                            "tool_calls": [{
                                "id": t["id"], "type": "function",
                                "function": dict(t["function"]),
                            } for t in tcs]}, tcs

                if finish == "stop":
                    return {"role": "assistant", "content": content}, None

                if finish == "length":
                    print("[LLM] max_tokens hit — truncating")
                    return {"role": "assistant", "content": content}, None

            return {"role": "assistant", "content": content}, None


# ═══════════════════════════════════════════════════════════════
# Voice Session (one per WebSocket connection)
# ═══════════════════════════════════════════════════════════════

class VoiceSession:
    """Manages one voice conversation's full pipeline lifecycle."""

    (S_LISTEN, S_THINK, S_SPEAK, S_TOOL) = range(4)
    STATE_NAMES = ["listening", "thinking", "speaking", "tool"]

    def __init__(self, ws: WebSocket, sid: str):
        self.ws = ws
        self.sid = sid
        self.persona = "hermes"
        self.state = self.S_LISTEN

        # Deepgram (stored as separate session + ws)
        self.dg_session = None
        self.dg = None
        self.dg_task: asyncio.Task | None = None

        # Conversation memory
        self.messages: list = []

        # LLM streaming
        self.cancel = asyncio.Event()
        self.llm_content = ""
        self.llm_buffer = ""

        # TTS
        self.tts_task: asyncio.Task | None = None

        # Concurrency guard
        self._processing = False

    # ── helpers ────────────────────────────────────────────────

    async def _send(self, data: dict):
        try:
            await self.ws.send_json(data)
        except Exception:
            pass

    async def _send_audio(self, data: bytes):
        try:
            await self.ws.send_bytes(data)
        except Exception:
            pass

    def _set_state(self, s: int):
        self.state = s
        name = self.STATE_NAMES[s]
        asyncio.ensure_future(self._send({"type": "status", "state": name}))

    def _voice_id(self) -> str:
        return {"hermes": HERMES_VOICE, "duke": DUKE_VOICE}.get(self.persona, HERMES_VOICE)

    def _reset_llm_state(self):
        self.llm_content = ""
        self.llm_buffer = ""
        self.cancel.clear()

    # ── lifecycle ──────────────────────────────────────────────

    async def start(self):
        """Initialize session: set system prompt + connect Deepgram."""
        self.messages = [{"role": "system", "content": SYSTEM_PROMPTS[self.persona]}]
        try:
            self.dg_session, self.dg = await dg_connect()
            self.dg_task = asyncio.create_task(self._dg_loop())
            print(f"[{self.sid}] Deepgram connected")
        except Exception as e:
            await self._send({"type": "error", "message": f"STT: {e}"})
            return

        await self._send({
            "type": "hello", "version": "2.1", "model": OPENROUTER_MODEL,
            "persona": self.persona, "voice_id": self._voice_id(),
        })

    async def handle_audio(self, data: bytes):
        """Forward PCM to Deepgram (unless processing a tool)."""
        if self.dg and self.state != self.S_TOOL:
            try:
                await self.dg.send_bytes(data)
            except Exception:
                pass

    async def handle_text(self, text: str):
        """Process control messages from browser."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        t = data.get("type")

        if t == "config":
            p = data.get("persona", "hermes")
            if p in SYSTEM_PROMPTS:
                self.persona = p
                self.messages = [{"role": "system", "content": SYSTEM_PROMPTS[p]}]
                self._reset_llm_state()
                self._set_state(self.S_LISTEN)
                await self._send({"type": "config", "persona": p, "voice_id": self._voice_id()})

        elif t == "reset":
            self._cancel_all()
            self.messages = [{"role": "system", "content": SYSTEM_PROMPTS[self.persona]}]
            self._reset_llm_state()
            self._set_state(self.S_LISTEN)

        elif t == "ping":
            await self._send({"type": "pong"})

    # ── Deepgram listener ──────────────────────────────────────

    async def _dg_loop(self):
        """Read transcription results from Deepgram, trigger turns."""
        try:
            if not self.dg:
                return
            async for msg in self.dg:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                raw = msg.data
                data = json.loads(raw)
                if data.get("type") != "Results":
                    continue

                alts = data.get("channel", {}).get("alternatives", [])
                if not alts:
                    continue

                text = alts[0].get("transcript", "").strip()
                final = data.get("is_final", False)

                if not text:
                    continue

                # Push transcript to browser UI
                await self._send({"type": "transcript", "text": text, "is_final": final})

                if final:
                    if self._processing:
                        continue
                    self._processing = True
                    try:
                        # Barge-in: if we were speaking, cancel TTS
                        if self.state == self.S_SPEAK:
                            self._cancel_all()
                            self._set_state(self.S_LISTEN)
                            await self._send({"type": "barge_in"})

                        # Brief settling delay (collect more context)
                        await asyncio.sleep(0.25)

                        if self.state == self.S_LISTEN and not self._is_cancelled():
                            await self._turn(text)
                    finally:
                        self._processing = False

                elif self.state == self.S_SPEAK and len(text) > 3:
                    # Barge-in on meaningful interim speech
                    self._cancel_all()
                    self._set_state(self.S_LISTEN)
                    await self._send({"type": "barge_in"})

        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # Normal WebSocket disconnect
        # _dg_loop end

    # ── conversation turn ──────────────────────────────────────

    async def _turn(self, transcript: str):
        """Process one user utterance end-to-end."""
        self._set_state(self.S_THINK)
        self._reset_llm_state()

        # Append user message
        self.messages.append({"role": "user", "content": transcript})

        # Trim conversation (keep latest 30 exchanges)
        sys_msgs = [m for m in self.messages if m["role"] == "system"]
        others = [m for m in self.messages if m["role"] != "system"]
        if len(others) > 30:
            others = others[-30:]
        self.messages = sys_msgs + others

        # Stream LLM → TTS pipe
        await self._llm_pipe()

    async def _llm_pipe(self):
        """Stream from LLM, flushing sentences to TTS. Handles tool calls recursively."""
        tts_queue = []  # sentences queued for TTS, processed sequentially

        async def on_content(chunk: str):
            self.llm_content += chunk
            self.llm_buffer += chunk
            await self._send({"type": "llm_text", "text": chunk})
            # Flush complete sentences from buffer to TTS queue
            await self._flush_sentences(tts_queue)

        async def on_tool_calls(tcs: list):
            self._set_state(self.S_TOOL)
            # Flush any remaining pre-tool text
            await self._flush_sentences(tts_queue, force=True)
            # Wait for queued TTS to finish
            await self._drain_tts(tts_queue)

            results = []
            for tc in tcs:
                name = tc.get("function", {}).get("name", "")
                try:
                    args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                await self._send({"type": "tool_call", "tool": name, "args": args})
                result = await execute_tool(name, args)
                await self._send({"type": "tool_result", "tool": name, "result": result[:300]})

                results.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps({"result": result}),
                })

            # Persist assistant message + tool results to history
            asst = {
                "role": "assistant",
                "content": self.llm_content or None,
                "tool_calls": [{
                    "id": t["id"], "type": "function",
                    "function": dict(t["function"]),
                } for t in tcs],
            }
            self.messages.append(asst)
            self.messages.extend(results)

            # Reset for continuation
            self.llm_content = ""
            self.llm_buffer = ""

            # Continue streaming with tool results
            await self._llm_pipe()

        try:
            msg, _ = await llm_stream(self.messages, on_content, on_tool_calls, self.cancel)

            if self._is_cancelled():
                return

            # Flush remaining buffer
            await self._flush_sentences(tts_queue, force=True)
            await self._drain_tts(tts_queue)

            if msg and msg.get("content"):
                self.messages.append(msg)

            self._set_state(self.S_LISTEN)
            await self._send({"type": "turn_end"})

        except Exception as e:
            print(f"[{self.sid}] LLM pipe: {e}")
            await self._send({"type": "error", "message": f"LLM: {str(e)[:100]}"})
            self._set_state(self.S_LISTEN)

    # ── TTS sentence flushing ──────────────────────────────────

    async def _flush_sentences(self, queue: list, force: bool = False):
        """Split llm_buffer on sentence boundaries and queue for TTS."""
        if not self.llm_buffer:
            return

        buf = self.llm_buffer

        if force:
            self.llm_buffer = ""
            if buf.strip():
                queue.append(buf.strip())
                self._schedule_tts(queue)
        else:
            sentences = re.split(r'(?<=[.!?])\s+', buf)
            if len(sentences) > 1:
                for s in sentences[:-1]:
                    if s.strip():
                        queue.append(s.strip())
                        self._schedule_tts(queue)
                self.llm_buffer = sentences[-1]

    def _schedule_tts(self, queue: list):
        """Fire TTS for the first queued sentence if not already running."""
        if self.tts_task and not self.tts_task.done():
            return  # TTS already running, sentences will be picked up when it finishes

        async def _tts_worker():
            while queue:
                sentence = queue.pop(0)
                if self.cancel.is_set():
                    queue.clear()
                    return
                self._set_state(self.S_SPEAK)
                await tts_stream(sentence, self._voice_id(), self.ws, self.cancel)

        self.tts_task = asyncio.create_task(_tts_worker())

    async def _drain_tts(self, queue: list):
        """Wait for all queued TTS to finish."""
        if self.tts_task and not self.tts_task.done():
            try:
                await self.tts_task
            except asyncio.CancelledError:
                pass
        queue.clear()

    # ── cancellation ───────────────────────────────────────────

    def _is_cancelled(self) -> bool:
        return self.cancel.is_set()

    def _cancel_all(self):
        """Cancel current LLM stream and TTS playback."""
        self.cancel.set()
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()

    # ── cleanup ────────────────────────────────────────────────

    async def cleanup(self):
        self.cancel.set()
        if self.dg:
            try:
                await self.dg.close()
            except Exception:
                pass
        if self.dg_session and not self.dg_session.closed:
            try:
                await self.dg_session.close()
            except Exception:
                pass
        if self.dg_task and not self.dg_task.done():
            self.dg_task.cancel()
            try:
                await self.dg_task
            except asyncio.CancelledError:
                pass
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        print(f"[{self.sid}] Cleaned up")


# ═══════════════════════════════════════════════════════════════
# FastAPI Application
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="Blimp Voice Relay")

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


@app.get("/")
async def index():
    path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(path):
        with open(path) as f:
            html = f.read()
        return HTMLResponse(html)
    return {"error": "index.html not found"}


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "deepgram": bool(DEEPGRAM_API_KEY),
        "openrouter": bool(OPENROUTER_API_KEY),
        "cartesia": bool(CARTESIA_API_KEY),
        "model": OPENROUTER_MODEL,
    }


@app.get("/api/auth-status")
async def auth_status(request: Request):
    """Check if client has a valid access cookie."""
    if not is_access_enabled():
        return {"authenticated": True, "reason": "no_gate"}
    cookie = request.cookies.get(ACCESS_COOKIE, "")
    expected = hash_access_key(ACCESS_PASSWORD)
    ok = cookie == expected
    return {"authenticated": ok}


@app.post("/api/auth")
async def auth_login(request: Request):
    """Verify access password and set auth cookie."""
    body = await request.json()
    password = body.get("password", "")
    if password != ACCESS_PASSWORD:
        return JSONResponse({"ok": False, "error": "wrong_password"}, status_code=403)

    digest = hash_access_key(password)
    resp = JSONResponse({"ok": True, "token": digest})
    resp.set_cookie(
        key=ACCESS_COOKIE,
        value=digest,
        max_age=ACCESS_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # HTTP for dev
        path="/",
    )
    return resp


@app.get("/debug/dg-test")
async def debug_dg():
    import aiohttp
    try:
        session = aiohttp.ClientSession()
        params = "encoding=linear16&sample_rate=16000&channels=1&model=nova-3&endpointing=300&utterance_end_ms=1000&vad_turnoff=500&no_delay=true"
        ws = await session.ws_connect(
            f"{DEEPGRAM_URL}?{params}",
            headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            heartbeat=5.0,
        )
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            if msg.type == aiohttp.WSMsgType.TEXT:
                return {"ok": True, "msg": str(msg.data)[:300]}
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                return {"ok": False, "error": "WebSocket closed"}
            elif msg.type == aiohttp.WSMsgType.ERROR:
                return {"ok": False, "error": str(msg.data)}
            return {"ok": True, "type": str(msg.type), "data": str(msg.data)[:200]}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "receive() timed out after 5s"}
        except Exception as e:
            return {"ok": False, "error": f"receive error: {e}"}
        finally:
            await ws.close()
            await session.close()
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}


@app.websocket("/ws")
async def voice_ws(ws: WebSocket):
    await ws.accept()
    sid = uuid.uuid4().hex[:8]
    ses = VoiceSession(ws, sid)
    print(f"[{sid}] ⚡ Connected")

    try:
        await ses.start()

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg:
                await ses.handle_audio(msg["bytes"])
            elif "text" in msg:
                await ses.handle_text(msg["text"])

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[{sid}] Error: {e}")
    finally:
        await ses.cleanup()
        print(f"[{sid}] ✄ Disconnected")


# ═══════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8791"))
    print(f"▶️  Blimp Voice Relay → http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)