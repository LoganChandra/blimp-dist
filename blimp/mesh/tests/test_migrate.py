"""`blimp migrate` (spec §18.3): baseline, apply, idempotent, refuses edited migrations, snapshot gate."""
from __future__ import annotations

import psycopg

from blimp.mesh import migrate as mig

from .conftest import MESH, TEST_DSN


def _only_0001():
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS mesh CASCADE")
        c.execute((MESH / "migrations" / "0001_mesh_schema.sql").read_text())


def test_baseline_then_apply_then_up_to_date(fresh_db):
    _only_0001()  # the live duke_data state on 2026-09-28: 0001 by hand, no tracking
    out = []
    assert mig.migrate(TEST_DSN, dry_run=True, out=out.append) == 0
    assert any("baseline" in o for o in out) and any("0002" in o for o in out) and "dry run: nothing changed" in out
    with psycopg.connect(TEST_DSN) as c:
        assert c.execute("SELECT to_regclass('mesh.id_counters')").fetchone()[0] is None  # dry run changed nothing
    assert mig.migrate(TEST_DSN, out=out.append) == 0
    with psycopg.connect(TEST_DSN) as c:
        names = [r[0] for r in c.execute("SELECT name FROM mesh.schema_migrations ORDER BY name")]
    assert names[0].startswith("0001_") and names[1].startswith("0002_")
    out2 = []
    assert mig.migrate(TEST_DSN, out=out2.append) == 0 and out2 == ["up to date"]


def test_refuses_edited_migration(fresh_db, monkeypatch):
    _only_0001()
    mig.migrate(TEST_DSN, out=lambda *_: None)
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("UPDATE mesh.schema_migrations SET sha256='deadbeef' WHERE name LIKE '0002_%'")
    out = []
    assert mig.migrate(TEST_DSN, out=out.append) == 2
    assert any(o.startswith("REFUSING") for o in out)


def test_failed_snapshot_aborts(fresh_db):
    _only_0001()
    out = []
    assert mig.migrate(TEST_DSN, snapshot_cmd="exit 7", out=out.append) == 3
    with psycopg.connect(TEST_DSN) as c:
        assert c.execute("SELECT to_regclass('mesh.id_counters')").fetchone()[0] is None
