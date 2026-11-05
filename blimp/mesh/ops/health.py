"""Deck health checks (spec §9.10 "every 60 s, zero-token probes, replace the heartbeat", §19.4
liveness registry and pulses, §10.1 health module).

Each probe is plain code (SQL, HTTP, TCP, filesystem): no model call. Results go to
mesh.pulses; a probe that turns bad raises ONE alert.raised event (deduplicated: again only
after it recovers and fails anew), which the dispatcher routes to Frank and the Bridge posts.

Probes:
  postgres, hindsight (:8888), hermes_gateway (:8644), disk (free %), backup (newest Tier-1
  file <= 26 h), site:<url> (HTTP 200 + TLS certificate > 14 days), stale pulses (max_age),
  liveness (an enabled producer whose consumer is disabled or silent: L-020).
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from ..deck.events import append_event

SITES = [s for s in os.environ.get("BLIMP_HEALTH_SITES", "https://yourproduct.example,https://blimp.my").split(",") if s]
BACKUP_DIR = Path(os.environ.get("BLIMP_BACKUP_DIR", os.path.expanduser("~/blimp/backups")))
DISK_MIN_FREE_PCT = float(os.environ.get("BLIMP_DISK_MIN_FREE_PCT", "10"))
CERT_MIN_DAYS = 14


@dataclass
class Result:
    job: str
    ok: bool
    detail: dict
    severity: str = "sev3"


def _tcp(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_postgres(dsn: str) -> Result:
    try:
        with psycopg.connect(dsn, connect_timeout=5) as c:
            c.execute("SELECT 1")
        return Result("postgres", True, {})
    except Exception as e:
        return Result("postgres", False, {"error": str(e)[:200]}, "sev2")


def probe_http(job: str, url: str, opener: Callable = urllib.request.urlopen, severity: str = "sev3") -> Result:
    t0 = time.monotonic()
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": "BlimpHealth/2.0"}), timeout=10) as r:
            ok = 200 <= r.status < 400
            return Result(job, ok, {"status": r.status, "ms": int((time.monotonic() - t0) * 1000)}, severity)
    except Exception as e:
        return Result(job, False, {"error": str(e)[:200]}, severity)


def cert_days_left(host: str, port: int = 443) -> float:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=8) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
        exp = ssl.cert_time_to_seconds(ss.getpeercert()["notAfter"])
    return (exp - time.time()) / 86400


def probe_site(url: str, opener: Callable = urllib.request.urlopen, cert: Callable[[str], float] = cert_days_left) -> Result:
    r = probe_http(f"site:{url}", url, opener, severity="sev2")
    if r.ok and url.startswith("https://"):
        try:
            days = cert(urlparse(url).hostname)
            r.detail["cert_days"] = round(days, 1)
            if days < CERT_MIN_DAYS:
                r.ok, r.severity = False, "sev2"
                r.detail["error"] = f"TLS certificate expires in {days:.0f} days"
        except Exception as e:
            r.detail["cert_error"] = str(e)[:200]
    return r


def probe_disk(path: str = "/") -> Result:
    u = shutil.disk_usage(path)
    free = 100 * u.free / u.total
    return Result("disk", free >= DISK_MIN_FREE_PCT, {"free_pct": round(free, 1)}, "sev2" if free < 5 else "sev3")


def probe_backup(now: float | None = None) -> Result:
    now = now or time.time()
    files = [p for p in BACKUP_DIR.rglob("*") if p.is_file()] if BACKUP_DIR.exists() else []
    if not files:
        return Result("backup", False, {"error": f"no backups under {BACKUP_DIR}"})
    newest = max(p.stat().st_mtime for p in files)
    age_h = (now - newest) / 3600
    return Result("backup", age_h <= 26, {"age_h": round(age_h, 1)})


def probes(dsn: str, opener: Callable = urllib.request.urlopen, cert: Callable = cert_days_left) -> list[Result]:
    out = [probe_postgres(dsn),
           Result("hindsight", _tcp("127.0.0.1", 8888), {"port": 8888}),
           Result("hermes_gateway", _tcp("127.0.0.1", 8644), {"port": 8644}),
           probe_disk(), probe_backup()]
    out += [probe_site(u, opener, cert) for u in SITES]
    return out


def record(dsn: str, results: list[Result]) -> dict:
    """Write pulses; alert once per transition to bad; check stale pulses and the liveness registry."""
    raised, recovered = [], []
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        for r in results:
            cur.execute("SELECT last_status FROM mesh.pulses WHERE job=%s", (r.job,))
            prev = cur.fetchone()
            prev_status = prev[0] if prev else None
            status = "ok" if r.ok else "failed"
            cur.execute("""INSERT INTO mesh.pulses (job, last_ok_at, last_status, detail) VALUES (%s, CASE WHEN %s THEN now() END, %s, %s)
                           ON CONFLICT (job) DO UPDATE SET last_ok_at = CASE WHEN %s THEN now() ELSE mesh.pulses.last_ok_at END,
                           last_status = EXCLUDED.last_status, detail = EXCLUDED.detail""",
                        (r.job, r.ok, status, Jsonb(r.detail), r.ok))
            if not r.ok and prev_status not in ("failed", "stale"):
                append_event(cur, "frank", "alert.raised", {"severity": r.severity, "kind": "health", "job": r.job, "detail": r.detail},
                             "pulse", r.job)
                raised.append(r.job)
            elif r.ok and prev_status in ("failed", "stale"):
                append_event(cur, "frank", "pulse.ok", {"job": r.job, "recovered": True, "detail": r.detail}, "pulse", r.job)
                recovered.append(r.job)
        # stale pulses (jobs that report in on their own schedule: backup, dreams, evals, ...)
        # only jobs that are otherwise OK go stale (a failing probe already alerted once)
        cur.execute("SELECT job FROM mesh.pulses WHERE last_ok_at < now() - max_age AND last_status = 'ok'")
        for (job,) in cur.fetchall():
            cur.execute("UPDATE mesh.pulses SET last_status='stale' WHERE job=%s", (job,))
            append_event(cur, "frank", "pulse.missed", {"job": job}, "pulse", job)
            raised.append(f"{job} (stale)")
        # L-020: an enabled producer whose consumer is disabled or has gone silent
        cur.execute("""SELECT p.component, d FROM mesh.liveness p, unnest(p.depends_on) d
                       LEFT JOIN mesh.liveness c ON c.component = d
                       WHERE p.enabled AND (c.component IS NULL OR NOT c.enabled OR c.last_seen_at < now() - interval '15 minutes')
                       AND d LIKE 'agent:%'""")
        broken = {f"liveness:{comp}->{dep}" for comp, dep in cur.fetchall()}
        cur.execute("SELECT job, last_status FROM mesh.pulses WHERE job LIKE 'liveness:%'")
        known = dict(cur.fetchall())
        for job in broken:
            if known.get(job) != "failed":  # alert once per breakage
                cur.execute("""INSERT INTO mesh.pulses (job, last_status, detail, max_age) VALUES (%s,'failed','{}', interval '100 years')
                               ON CONFLICT (job) DO UPDATE SET last_status='failed'""", (job,))
                comp, dep = job.removeprefix("liveness:").split("->")
                append_event(cur, "frank", "liveness.broken", {"producer": comp, "consumer": dep, "severity": "sev3"}, "liveness", comp)
                raised.append(job)
        for job, st in known.items():
            if st == "failed" and job not in broken:
                cur.execute("UPDATE mesh.pulses SET last_status='ok', last_ok_at=now() WHERE job=%s", (job,))
                recovered.append(job)
    return {"raised": raised, "recovered": recovered}
