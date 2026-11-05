"""Health probes (spec §9.10, §19.4): zero-token, pulses, alert once per transition, liveness."""
from __future__ import annotations

import io
import os
import time

import psycopg

from blimp.mesh.deck.dispatch import Dispatcher
from blimp.mesh.ops import health as h


def resp(status):
    class R(io.BytesIO):
        def __enter__(s):
            return s

        def __exit__(s, *a):
            return False
    r = R(b"")
    r.status = status
    return r


def alerts(dsn):
    with psycopg.connect(dsn) as c:
        return [r[0] for r in c.execute("SELECT payload->>'job' FROM mesh.events WHERE type='alert.raised' ORDER BY id")]


def test_site_down_alerts_once_and_recovers(fresh_db):
    down = [h.probe_site("https://yourproduct.example", opener=lambda r, timeout=10: (_ for _ in ()).throw(OSError("connection refused")))]
    assert h.record(fresh_db, down)["raised"] == ["site:https://yourproduct.example"]
    assert h.record(fresh_db, down)["raised"] == []  # no alert storm
    up = [h.probe_site("https://yourproduct.example", opener=lambda r, timeout=10: resp(200), cert=lambda host: 80.0)]
    assert h.record(fresh_db, up)["recovered"] == ["site:https://yourproduct.example"]
    assert alerts(fresh_db) == ["site:https://yourproduct.example"]


def test_expiring_certificate_is_an_alert(fresh_db):
    r = h.probe_site("https://blimp.my", opener=lambda r, timeout=10: resp(200), cert=lambda host: 5.0)
    assert not r.ok and r.severity == "sev2" and "expires in 5 days" in r.detail["error"]


def test_site_alert_wakes_frank(fresh_db, monkeypatch):
    h.record(fresh_db, [h.Result("site:https://yourproduct.example", False, {"status": 500}, "sev2")])
    Dispatcher(fresh_db, mode="spec").route()
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT agent, priority FROM mesh.turn_requests").fetchall() == [("frank", "P0")]


def test_backup_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "BACKUP_DIR", tmp_path)
    assert not h.probe_backup().ok
    f = tmp_path / "duke_data.dump"
    f.write_text("x")
    assert h.probe_backup().ok
    old = time.time() - 30 * 3600
    os.utime(f, (old, old))
    r = h.probe_backup()
    assert not r.ok and r.detail["age_h"] >= 30


def test_liveness_broken_consumer(fresh_db):
    with psycopg.connect(fresh_db) as c:
        c.execute("INSERT INTO mesh.liveness (component, role, depends_on, enabled, last_seen_at) VALUES "
                  "('deck','producer', ARRAY['agent:duke'], true, now()), ('agent:duke','consumer','{}', false, now())")
    assert h.record(fresh_db, [])["raised"] == ["liveness:deck->agent:duke"]
    assert h.record(fresh_db, [])["raised"] == []
    with psycopg.connect(fresh_db) as c:
        c.execute("UPDATE mesh.liveness SET enabled=true WHERE component='agent:duke'")
    assert h.record(fresh_db, [])["recovered"] == ["liveness:deck->agent:duke"]


def test_postgres_probe(fresh_db):
    assert h.probe_postgres(fresh_db).ok
    assert not h.probe_postgres("postgresql://nobody@127.0.0.1:1/x").ok


def test_daemon_once(fresh_db, monkeypatch, capsys):
    import json

    from blimp.mesh.deck import daemon
    monkeypatch.setenv("BLIMPG_DSN", fresh_db)
    monkeypatch.setattr(h, "SITES", [])
    daemon.main(["--once"])
    out = json.loads(capsys.readouterr().out)
    assert "routed" in out and "health" in out and out["recovered"] == {"requeued": [], "poisoned": []}
