"""Envelope + anti-chatter validation (spec §8.3, §8.11), adversarial cases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blimp.mesh.deck.envelope import validate_envelope

AGENTS = {"duke", "hermes", "frank", "logan"}
EXT = "https://blimp.my/a2a/ext/speech-act/v1"


def msg(text="PROPOSE: split intake into webhook + worker; see AR-0311.", **sa):
    meta = {"act": "PROPOSE", "from": "hermes", "to": ["duke"], "expects": "decision",
            "reply_by": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            "priority": "P2", "purpose": "Pick the intake architecture"}
    meta.update(sa)
    return {"messageId": "01J9Z6N5Y3K8V1M2Q7R4S0T6UX", "contextId": "ctx-T-0142", "taskId": "T-0142",
            "role": "ROLE_AGENT", "parts": [{"text": text}], "metadata": {EXT: meta}}


def ok(m):
    return validate_envelope(m, known_agents=AGENTS)


def test_valid_propose():
    assert ok(msg()) == (True, [])


@pytest.mark.parametrize("text", ["Got it.", "thanks!", "On it", "ok", "Okay 👍", "noted.", "Sounds good!", "got it, thanks", "Thank you :)"])
def test_pure_acks_rejected(text):
    good, errs = ok(msg(text, act="INFORM", expects="none", reply_by=None))
    assert not good and any("acknowledgement" in e for e in errs), (text, errs)


@pytest.mark.parametrize("text", [
    "OK, two options: A single service, B webhook + worker. I recommend B.",
    "Thanks to the new index, p95 dropped to 40ms [RC-0112].",
    "Sure-fire way to break it: send 2 webhooks in 10ms. Repro in AR-0400.",
    "Noted risk: Meta retries for 24h, so the worker must be idempotent.",
])
def test_real_messages_starting_with_ack_words_pass(text):
    assert ok(msg(text, act="INFORM", expects="none", reply_by=None))[0], text


def test_gratitude_to_logan_allowed_with_flag():
    m = msg("Thank you!", act="INFORM", expects="none", reply_by=None, to=["logan"], gratitude_to_logan=True)
    assert ok(m)[0]


def test_gratitude_flag_not_valid_to_agents():
    m = msg("Thank you!", act="INFORM", expects="none", reply_by=None, gratitude_to_logan=True)
    good, errs = ok(m)
    assert not good and any("only valid on messages to logan" in e for e in errs)


def test_reply_by_required_when_expecting():
    good, errs = ok(msg(reply_by=None))
    assert not good and any("requires reply_by" in e for e in errs)


def test_reply_by_in_past_rejected():
    good, errs = ok(msg(reply_by=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()))
    assert not good and any("past" in e for e in errs)


def test_unknown_recipient():
    good, errs = ok(msg(to=["other-agent"]))
    assert not good and any("does not resolve" in e for e in errs)


def test_oversize_body_goes_to_artifact():
    good, errs = ok(msg("word " * 2000))
    assert not good and any("artifact" in e for e in errs)


def test_purpose_cap():
    good, errs = ok(msg(purpose="x" * 161))
    assert not good and any("purpose" in e for e in errs)


def test_handoff_needs_packet():
    good, errs = ok(msg(act="HANDOFF", expects="accept"))
    assert not good and any("handoff packet" in e for e in errs)


@pytest.mark.parametrize("field", ["messageId", "contextId", "taskId"])
def test_required_ids(field):
    m = msg()
    del m[field]
    assert not ok(m)[0]
