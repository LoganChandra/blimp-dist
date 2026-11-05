#!/usr/bin/env python3
"""
Frank Voice — ChatGPT-voice-style talk-to-Frank, zero new keys, zero idle spend.

  Browser (Web Speech API: mic -> text, speechSynthesis: text -> voice)
        |  POST /api/turn {"text": ...}      (same-origin JSON only)
        v
  this server (stdlib, 127.0.0.1 only)
        |  spawns ONE headless `claude -p` per turn, cwd = Frank's repo
        |  (~/nanook/repo -> his CLAUDE.md loads -> he is Frank), --resume
        |  keeps the conversation going across turns
        v
  SSE stream of text deltas back to the browser, which speaks them
  sentence by sentence as they arrive.

Nothing runs between turns: no model process, no websocket to a vendor,
no polling. A new turn (or barge-in) kills any turn still in flight.

v1 is talk-only: Frank gets NO tools in voice mode (misheard speech must
never trigger an action). Set FRANK_VOICE_TOOLS to opt in later.

Run:  ./run.sh           (or: python3 server.py)
Open: http://127.0.0.1:8791   in Chrome or Edge on the same PC.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"  # never 0.0.0.0 — rail 5, not configurable on purpose
PORT = int(os.environ.get("FRANK_VOICE_PORT", "8791"))
FRANK_CWD = os.path.expanduser(os.environ.get("FRANK_VOICE_CWD", "~/nanook/repo"))
MODEL = os.environ.get("FRANK_VOICE_MODEL", "sonnet")
CLAUDE_BIN = os.environ.get("FRANK_VOICE_CLAUDE_BIN", "claude")
TOOLS = os.environ.get("FRANK_VOICE_TOOLS", "")  # "" = no tools at all
TURN_TIMEOUT_S = int(os.environ.get("FRANK_VOICE_TURN_TIMEOUT", "120"))
MAX_TEXT = 2000
BRAIN_LOG = os.environ.get("FRANK_VOICE_BRAIN_LOG", "1") == "1"  # shared-brain turn log
STATE_FILE = Path(os.path.expanduser(
    os.environ.get("FRANK_VOICE_STATE", "~/.cache/frank-voice/session.json")))

VOICE_PROMPT = """VOICE MODE. You are Frank, talking out loud with the Boss through a \
ChatGPT-style voice interface. Everything you write is converted to speech and \
played to him immediately — this voice channel IS the delivery channel for this \
conversation, so do NOT use or mention Discord tools here; the Discord-reply rule \
does not apply to voice turns. Speak naturally like a person on a call: usually \
1-3 short sentences, longer only if he asks for detail. No markdown, no lists, no \
headings, no emojis, no code blocks, no URLs read aloud. His words come from \
speech recognition and may contain mis-hearings — if something is ambiguous, ask. \
In voice mode you have no tools, so never claim you checked, ran or changed \
anything; say you'll do it from Discord instead. Current time for the Boss: {now}."""


def now_str() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%A %d %B %Y, %H:%M (GMT+8)")


class Session:
    """One running conversation + at most one in-flight turn."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        try:
            self.session_id = json.loads(STATE_FILE.read_text()).get("session_id")
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"session_id": self.session_id}))

    def reset(self) -> None:
        self.cancel()
        self.session_id = None
        self.save()

    def cancel(self, only: subprocess.Popen | None = None) -> None:
        """Kill the in-flight turn. With `only`, kill that process and clear it
        only if it is still current — a superseded turn's cleanup (browser hung
        up, timeout) must never kill the turn that replaced it."""
        with self.lock:
            if only is None or self.proc is only:
                p, self.proc = self.proc, None
            else:
                p = only
        _kill(p)

    def start(self, text: str) -> subprocess.Popen:
        cmd = [CLAUDE_BIN, "-p", text,
               "--append-system-prompt", VOICE_PROMPT.format(now=now_str()),
               "--model", MODEL,
               "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--strict-mcp-config", "--tools", TOOLS]
        env = {k: v for k, v in os.environ.items() if k != "DUKE_CHANNEL"}  # never arm Discord hooks
        with self.lock:  # kill-old + spawn-new + track is one atomic step
            old, self.proc = self.proc, None
            _kill(old)  # barge-in: a new turn always wins
            if self.session_id:
                cmd += ["--resume", self.session_id]
            self.proc = subprocess.Popen(cmd, cwd=FRANK_CWD, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
            return self.proc


def _kill(p: subprocess.Popen | None) -> None:
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


SESSION = Session()


def brain_log(you: str, frank: str) -> None:
    """Log the finished turn to the shared brain (channel `nanook`), so Frank's
    Discord session — and Duke/Hermes — can recall what was said by voice.
    Best-effort, off the request path: a brain hiccup never breaks a call."""
    if not BRAIN_LOG:
        return

    def run():
        for speaker, msg in (("logan", you), ("nanook", frank)):
            try:
                subprocess.run(["duke-mem", "log", "--user", "logan", "--channel", "nanook",
                                "--speaker", speaker, "--message", f"[voice] {msg}"],
                               capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"[frank-voice] brain log skipped: {e}", flush=True)
                return
    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "FrankVoice/1"

    def log_message(self, fmt, *args):  # quiet, one line per request
        print(f"[frank-voice] {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    # -- helpers ----------------------------------------------------------
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        # Any web page can POST to localhost; only our own page may drive Frank.
        origin = self.headers.get("Origin")
        allowed = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
        ctype = self.headers.get("Content-Type", "")
        return (origin is None or origin in allowed) and ctype.startswith("application/json")

    def _sse(self, obj: dict) -> None:
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "model": MODEL, "cwd": FRANK_CWD,
                             "session": bool(SESSION.session_id), "tools": TOOLS or "none"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._same_origin():
            return self._json(403, {"error": "same-origin JSON only"})
        if self.path == "/api/reset":
            SESSION.reset()
            return self._json(200, {"ok": True})
        if self.path == "/api/cancel":
            SESSION.cancel()
            return self._json(200, {"ok": True})
        if self.path != "/api/turn":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            text = str(json.loads(self.rfile.read(n) or b"{}").get("text", "")).strip()
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not text or len(text) > MAX_TEXT:
            return self._json(400, {"error": f"text must be 1-{MAX_TEXT} chars"})
        self._turn(text)

    def _turn(self, text: str) -> None:
        t0 = time.time()
        p = SESSION.start(text)
        killer = threading.Timer(TURN_TIMEOUT_S, lambda: SESSION.cancel(p))
        killer.start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        got_text = False
        reply = []
        try:
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init" and not TOOLS \
                        and (ev.get("tools") or ev.get("mcp_servers")):
                    # talk-only rail: if a CLI change ever exposes tools, refuse the turn
                    SESSION.cancel(p)
                    self._sse({"type": "error", "message": "voice rail tripped: tools exposed, turn refused"})
                    return
                if ev.get("type") == "stream_event":
                    e = ev.get("event", {})
                    d = e.get("delta", {})
                    if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                        got_text = True
                        reply.append(d["text"])
                        self._sse({"type": "delta", "text": d["text"]})
                elif ev.get("type") == "result":
                    if ev.get("session_id"):
                        SESSION.session_id = ev["session_id"]
                        SESSION.save()
                    if not got_text and ev.get("result"):  # non-streamed fallback
                        reply.append(ev["result"])
                        self._sse({"type": "delta", "text": ev["result"]})
                    if not ev.get("is_error"):
                        brain_log(text, "".join(reply))
                    self._sse({"type": "done", "ms": int((time.time() - t0) * 1000),
                               "error": bool(ev.get("is_error"))})
                    return
            # stream ended without a result: cancelled, timed out, or crashed
            err = (p.stderr.read() or "").strip()[-300:] if p.poll() is not None else ""
            self._sse({"type": "error", "message": err or "turn ended early (cancelled or timed out)"})
        except (BrokenPipeError, ConnectionResetError):
            SESSION.cancel(p)  # browser hung up (barge-in / closed tab) -> stop spending
        finally:
            killer.cancel()


def main() -> None:
    if not os.path.isdir(FRANK_CWD):
        raise SystemExit(f"FRANK_VOICE_CWD not found: {FRANK_CWD}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[frank-voice] listening on http://{HOST}:{PORT}  model={MODEL}  cwd={FRANK_CWD}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.cancel()


if __name__ == "__main__":
    main()
