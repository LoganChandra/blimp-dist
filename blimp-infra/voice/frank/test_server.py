#!/usr/bin/env python3
"""Offline tests for server.py — a fake `claude` binary, so no model spend.

Covers: SSE delta streaming, --resume session continuity, reset, same-origin
gate, barge-in cancellation (a new turn kills the old process), 127.0.0.1 bind.
Run: python3 test_server.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="frank-voice-test-"))
FAKE = TMP / "fake-claude"
ARGV_LOG = TMP / "argv.jsonl"
FAKE.write_text(f"""#!/usr/bin/env python3
import json, sys, time
open({str(ARGV_LOG)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
text = sys.argv[sys.argv.index("-p") + 1]
if text == "tools":
    print(json.dumps({{"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": []}}), flush=True)
    time.sleep(30)
print(json.dumps({{"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}}), flush=True)
if text == "slow":
    time.sleep(30)
for chunk in ["Hello the Boss. ", "You said: ", text, "."]:
    ev = {{"type": "stream_event", "event": {{"type": "content_block_delta",
          "delta": {{"type": "text_delta", "text": chunk}}}}}}
    print(json.dumps(ev), flush=True)
print(json.dumps({{"type": "result", "result": "x", "session_id": "sess-123", "is_error": False}}), flush=True)
""")
FAKE.chmod(0o755)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body=None, origin=None, ctype="application/json", timeout=10):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST")
    if ctype:
        req.add_header("Content-Type", ctype)
    if origin:
        req.add_header("Origin", origin)
    return urllib.request.urlopen(req, timeout=timeout)


def events(resp):
    return [json.loads(l[6:]) for l in resp.read().decode().split("\n\n") if l.startswith("data: ")]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, FRANK_VOICE_PORT=str(PORT), FRANK_VOICE_CLAUDE_BIN=str(FAKE),
                   FRANK_VOICE_CWD=str(TMP), FRANK_VOICE_BRAIN_LOG="0", FRANK_VOICE_STATE=str(TMP / "session.json"))
        cls.srv = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                urllib.request.urlopen(BASE + "/api/health", timeout=1); break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate(); cls.srv.wait(5)

    def setUp(self):
        post("/api/reset")
        ARGV_LOG.write_text("")

    def argvs(self):
        return [json.loads(l) for l in ARGV_LOG.read_text().splitlines() if l]

    def test_page_served(self):
        html = urllib.request.urlopen(BASE + "/").read().decode()
        self.assertIn("Talk to Frank", html)

    def test_turn_streams_deltas_and_done(self):
        ev = events(post("/api/turn", {"text": "status check"}))
        text = "".join(e["text"] for e in ev if e["type"] == "delta")
        self.assertEqual(text, "Hello the Boss. You said: status check.")
        self.assertEqual(ev[-1]["type"], "done")
        argv = self.argvs()[0]
        for flag in ("--strict-mcp-config", "--include-partial-messages", "--append-system-prompt"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # talk-only, no tools
        self.assertNotIn("--resume", argv)

    def test_second_turn_resumes_session(self):
        events(post("/api/turn", {"text": "one"}))
        events(post("/api/turn", {"text": "two"}))
        a2 = self.argvs()[1]
        self.assertEqual(a2[a2.index("--resume") + 1], "sess-123")

    def test_reset_forgets_session(self):
        events(post("/api/turn", {"text": "one"}))
        post("/api/reset")
        events(post("/api/turn", {"text": "two"}))
        self.assertNotIn("--resume", self.argvs()[1])

    def test_cross_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            post("/api/turn", {"text": "hi"}, origin="https://evil.example")
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            post("/api/turn", {"text": "hi"}, ctype="text/plain")
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.argvs(), [])  # nothing spawned

    def test_same_origin_accepted(self):
        ev = events(post("/api/turn", {"text": "hi"}, origin=BASE))
        self.assertEqual(ev[-1]["type"], "done")

    def test_empty_and_oversize_rejected(self):
        for body in ({"text": "  "}, {"text": "x" * 2001}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/api/turn", body)
            self.assertEqual(cm.exception.code, 400)

    def test_new_turn_kills_inflight_turn(self):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("ev", events(post("/api/turn", {"text": "slow"}, timeout=40))))
        t0 = time.time(); t.start(); time.sleep(1.0)
        ev2 = events(post("/api/turn", {"text": "fast"}))
        t.join(10)
        self.assertFalse(t.is_alive(), "slow turn was not cancelled")
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(out["ev"][-1]["type"], "error")
        self.assertEqual(ev2[-1]["type"], "done")

    def test_tools_exposed_trips_rail(self):
        t0 = time.time()
        ev = events(post("/api/turn", {"text": "tools"}))
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(ev[-1]["type"], "error")
        self.assertIn("rail", ev[-1]["message"])
        self.assertFalse(any(e["type"] == "delta" for e in ev))

    def test_binds_loopback_only(self):
        out = subprocess.run(["ss", "-ltnH", f"sport = :{PORT}"], capture_output=True, text=True).stdout
        self.assertIn(f"127.0.0.1:{PORT}", out)
        self.assertNotIn(f"0.0.0.0:{PORT}", out)


class CancelSemanticsTests(unittest.TestCase):
    """Reviewer P1: a superseded turn's cleanup must never kill its successor."""

    def test_stale_cancel_spares_current_turn(self):
        os.environ.update(FRANK_VOICE_CLAUDE_BIN=str(FAKE), FRANK_VOICE_CWD=str(TMP),
                          FRANK_VOICE_STATE=str(TMP / "unit-session.json"))
        sys.path.insert(0, str(HERE))
        import importlib
        server = importlib.import_module("server")
        s = server.Session()
        a = s.start("slow")
        b = s.start("slow")          # B supersedes A: A killed, B current
        a.wait(5)
        self.assertIsNotNone(a.poll(), "A should be dead")
        s.cancel(a)                  # A's late BrokenPipe / timeout cleanup
        time.sleep(0.3)
        self.assertIsNone(b.poll(), "stale cancel killed the current turn")
        self.assertIs(s.proc, b)
        s.cancel(b)
        b.wait(5)
        self.assertIsNone(s.proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
