"""Idle-burn SLO: "0 tokens/hour when no work is queued" (spec §4.4), measured, not asserted.

An hour is IDLE when nothing gave the crew a reason to work:
  - no wake-worthy event (a message to an agent, a schedule firing, an alert, an approval,
    a verification result, an unblocked task) and
  - no task in WORKING at any point in the hour.
Idle burn for that hour = tokens of turns that STARTED in it (mesh.turns, the kernel's turn
log) plus provider spend measured by the zero-token probe (mesh.spend_samples), which also
catches anything that ran outside Deck.

`sample` records one provider reading (an HTTP GET of a usage counter, never a model call).
`report` prints the verdict over a window (default 7 days: the phase 1 exit criterion).

Usage:
  python -m blimp.mesh.ops.idle_burn sample              # cron hourly
  python -m blimp.mesh.ops.idle_burn report [--days 7]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

WAKE_EVENTS = ("message.sent", "schedule.fired", "alert.raised", "approval.granted", "verification.failed",
               "verification.passed", "task.unblocked", "system.thaw")


@dataclass
class HourVerdict:
    hour: datetime
    idle: bool
    turn_tokens: int
    spend_usd: float

    @property
    def burned(self) -> bool:
        return self.idle and (self.turn_tokens > 0 or self.spend_usd > 0.0005)


def openrouter_usage(key: str, opener=urllib.request.urlopen) -> tuple[float, float]:
    req = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
    with opener(req, timeout=20) as r:
        d = json.loads(r.read())["data"]
    return float(d["total_usage"]), float(d["total_credits"])


def sample(dsn: str, key: str | None = None, opener=urllib.request.urlopen) -> dict:
    key = key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set; cannot sample (nothing recorded)")
    usage, credits = openrouter_usage(key, opener)
    with psycopg.connect(dsn) as c:
        c.execute("INSERT INTO mesh.spend_samples (provider, usage_usd, credits_usd) VALUES ('openrouter', %s, %s)", (usage, credits))
    return {"provider": "openrouter", "usage_usd": usage, "credits_usd": credits}


def verdicts(dsn: str, start: datetime, end: datetime) -> list[HourVerdict]:
    out = []
    with psycopg.connect(dsn) as c:
        hour = start.replace(minute=0, second=0, microsecond=0)
        while hour < end:
            nxt = hour + timedelta(hours=1)
            wakes = c.execute("SELECT count(*) FROM mesh.events WHERE ts >= %s AND ts < %s AND type = ANY(%s)",
                              (hour, nxt, list(WAKE_EVENTS))).fetchone()[0]
            # a task counts as worked in this hour if it entered WORKING before the hour ended and had not left before it began
            working = c.execute(
                """SELECT count(*) FROM mesh.events e WHERE e.type='task.state_changed' AND e.payload->>'to'='WORKING' AND e.ts < %s
                   AND NOT EXISTS (SELECT 1 FROM mesh.events x WHERE x.subject_id=e.subject_id AND x.type='task.state_changed'
                                   AND x.payload->>'from'='WORKING' AND x.ts > e.ts AND x.ts < %s)""",
                (nxt, hour)).fetchone()[0]
            tokens = c.execute("SELECT COALESCE(sum(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) FROM mesh.turns "
                               "WHERE started_at >= %s AND started_at < %s", (hour, nxt)).fetchone()[0]
            s = c.execute("""SELECT (SELECT usage_usd FROM mesh.spend_samples WHERE provider='openrouter' AND taken_at <= %s ORDER BY taken_at DESC LIMIT 1),
                                    (SELECT usage_usd FROM mesh.spend_samples WHERE provider='openrouter' AND taken_at <= %s ORDER BY taken_at DESC LIMIT 1)""",
                          (hour, nxt)).fetchone()
            spend = float(s[1] - s[0]) if s[0] is not None and s[1] is not None else 0.0
            out.append(HourVerdict(hour, idle=(wakes == 0 and working == 0), turn_tokens=int(tokens), spend_usd=spend))
            hour = nxt
    return out


def report(dsn: str, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    vs = verdicts(dsn, start, now)
    with psycopg.connect(dsn) as c:
        samples = c.execute("SELECT count(*), min(taken_at) FROM mesh.spend_samples WHERE provider='openrouter' AND taken_at >= %s", (start,)).fetchone()
    idle = [v for v in vs if v.idle]
    burned = [v for v in idle if v.burned]
    covered = samples[0] >= max(1, int(len(vs) * 0.9)) and samples[1] is not None and samples[1] <= start + timedelta(hours=1)
    return {
        "window_days": days, "hours": len(vs), "idle_hours": len(idle),
        "idle_hours_with_burn": len(burned),
        "idle_tokens": sum(v.turn_tokens for v in idle),
        "idle_spend_usd": round(sum(v.spend_usd for v in idle), 6),
        "spend_samples": samples[0], "probe_coverage_ok": covered,
        "verdict": ("PASS" if not burned and covered else "FAIL" if burned else "UNVERIFIED (spend probe did not cover the window)"),
        "burn_hours": [v.hour.isoformat() for v in burned[:24]],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "report"])
    ap.add_argument("--dsn", default=os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433"))
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    print(json.dumps(sample(a.dsn) if a.cmd == "sample" else report(a.dsn, a.days), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
