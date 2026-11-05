#!/usr/bin/env python3
"""Offline tests: gate.py (D-008, senders, caps, budget) and hermes_wake.py
(CLI refusals, V2 signature the real gateway accepts). No Hermes turn, no spend.
Run: python3 test_hermes_wake.py
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="hermes-wake-test-"))
GOOD = {"from": "frank", "logan_said": "ask him to figure it out", "logan_when": "00:31", "task": "do X", "wake_id": "W-1"}


def gate(payload, ledger=None, max_per_hour=6):
    env = dict(os.environ, BLIMP_WAKE_LEDGER=str(ledger or TMP / "ledger.json"), BLIMP_WAKE_MAX_PER_HOUR=str(max_per_hour))
    return subprocess.run([sys.executable, str(HERE / "gate.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


class GateTests(unittest.TestCase):
    def setUp(self):
        (TMP / "ledger.json").unlink(missing_ok=True)

    def test_passes_logan_initiated_wake(self):
        r = gate(GOOD)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["logan_said"], "ask him to figure it out")
        self.assertEqual(out["from"], "frank")

    def test_d008_drops_peer_only_wake(self):
        for said in ("", "  ", "ok"):
            r = gate({**GOOD, "logan_said": said})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("D-008", r.stderr)

    def test_unknown_sender_dropped(self):
        self.assertNotEqual(gate({**GOOD, "from": "hermes"}).returncode, 0)   # no self-wake
        self.assertNotEqual(gate({**GOOD, "from": "random"}).returncode, 0)

    def test_extra_fields_stripped_and_caps(self):
        r = gate({**GOOD, "evil": "x"})
        self.assertNotIn("evil", json.loads(r.stdout))
        self.assertNotEqual(gate({**GOOD, "task": "x" * 4001}).returncode, 0)

    def test_budget_guard(self):
        ledger = TMP / "budget.json"
        ledger.unlink(missing_ok=True)
        for _ in range(3):
            self.assertEqual(gate(GOOD, ledger, 3).returncode, 0)
        r = gate(GOOD, ledger, 3)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("budget", r.stderr)


class Capture(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Capture.seen.append((self.path, self.headers, body))  # case-insensitive
        self.send_response(202); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Capture)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.secret = TMP / "secret"
        cls.secret.write_text("s3cret\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_cli(self, *args):
        env = dict(os.environ, HERMES_GATEWAY_URL=f"http://127.0.0.1:{self.srv.server_port}",
                   HERMES_WAKE_SECRET_FILE=str(self.secret))
        return subprocess.run([sys.executable, str(HERE / "hermes_wake.py"), "--no-brain-log", *args],
                              capture_output=True, text=True, env=env)

    def test_refuses_without_logan_words(self):
        r = self.run_cli("--from", "frank", "--logan-said", "", "--task", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("D-008", r.stderr)

    def test_signed_post_matches_gateway_v2_scheme(self):
        Capture.seen.clear()
        r = self.run_cli("--from", "duke", "--logan-said", "figure it out for them", "--task", "wake design")
        self.assertEqual(r.returncode, 0, r.stderr)
        path, h, body = Capture.seen[-1]
        self.assertEqual(path, "/webhooks/blimp-wake")
        # exactly what gateway/platforms/webhook.py verifies for generic V2
        expected = hmac.new(b"s3cret", h["X-Webhook-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(h["X-Webhook-Signature-V2"], expected))
        p = json.loads(body)
        self.assertEqual(p["event_type"], "blimp.wake")
        self.assertEqual(h["X-Request-ID"], p["wake_id"])  # gateway idempotency key

    def test_gateway_rejection_is_reported(self):
        env = dict(os.environ, HERMES_GATEWAY_URL="http://127.0.0.1:9", HERMES_WAKE_SECRET_FILE=str(self.secret),
                   PATH="/nonexistent")  # can't start the service either
        r = subprocess.run([sys.executable, str(HERE / "hermes_wake.py"), "--no-brain-log", "--from", "duke",
                            "--logan-said", "do it", "--task", "x"], capture_output=True, text=True, env=env, timeout=90)
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
