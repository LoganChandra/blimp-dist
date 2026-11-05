"""Idle-burn SLO (spec §4.4): idle hours spend 0; the probe is a usage read, never a model call;
the kernel contains no LLM client at all."""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from blimp.mesh.ops import idle_burn as ib

MESH = Path(__file__).resolve().parents[1]


def _sample(c, when, usage):
    c.execute("INSERT INTO mesh.spend_samples (provider, taken_at, usage_usd, credits_usd) VALUES ('openrouter', %s, %s, 490)", (when, usage))


def test_quiet_week_passes(fresh_db):
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    with psycopg.connect(fresh_db) as c:
        for h in range(7 * 24 + 1):
            _sample(c, now - timedelta(days=7) + timedelta(hours=h) - timedelta(minutes=1), 475.75)
    r = ib.report(fresh_db, days=7, now=now)
    assert r["verdict"] == "PASS" and r["idle_hours"] == 168 and r["idle_spend_usd"] == 0


def test_spend_in_an_idle_hour_fails(fresh_db):
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    with psycopg.connect(fresh_db) as c:
        usage = 475.0
        for h in range(7 * 24 + 1):
            if h == 50:
                usage += 0.02  # e.g. a Hermes cron job fired with nothing queued
            _sample(c, now - timedelta(days=7) + timedelta(hours=h) - timedelta(minutes=1), usage)
    r = ib.report(fresh_db, days=7, now=now)
    assert r["verdict"] == "FAIL" and r["idle_hours_with_burn"] == 1


def test_work_hours_are_not_idle(fresh_db):
    from blimp.mesh.deck.board import Board
    b = Board(fresh_db)
    b.create_task(title="x", type="maintenance", created_by="frank")
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    vs = ib.verdicts(fresh_db, now - timedelta(hours=2), now)
    assert all(v.idle for v in vs)  # a drafted task alone is not queued work
    with psycopg.connect(fresh_db) as c:
        c.execute("INSERT INTO mesh.events (actor, type, payload, hash) VALUES ('logan','message.sent','{}', '\\x00')")
    assert not all(v.idle for v in ib.verdicts(fresh_db, now - timedelta(hours=2), now))


def test_missing_probe_is_unverified_not_pass(fresh_db):
    r = ib.report(fresh_db, days=7)
    assert r["verdict"].startswith("UNVERIFIED")


def test_probe_reads_usage_only():
    seen = []

    def opener(req, timeout=20):
        seen.append(req.full_url)

        class R(io.BytesIO):
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R(json.dumps({"data": {"total_usage": 475.75, "total_credits": 490}}).encode())

    assert ib.openrouter_usage("k", opener) == (475.75, 490.0)
    assert seen == ["https://openrouter.ai/api/v1/credits"]


LLM_MARKERS = re.compile(r"chat/completions|anthropic|openai|claude_agent_sdk|messages\.create|/v1/messages|OPENROUTER_API_KEY", re.I)


def test_kernel_has_no_llm_inside():
    """Spec P1/P8 and 6.2: Deck, the Bridge, the board tools and migrations never call a model."""
    offenders = []
    for d in ("deck", "bridge", "tools"):
        for f in (MESH / d).glob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if LLM_MARKERS.search(line) and not line.lstrip().startswith(("#", '"', "'")):
                    offenders.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert offenders == []
