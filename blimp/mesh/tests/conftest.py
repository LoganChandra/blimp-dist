"""Test harness for the mesh (spec §20 Proving Ground, unit layer).

Every DB-backed test runs against an ISOLATED database with a freshly applied `mesh`
schema. The live `duke_data` event log is append-only and hash-chained; tests must
never write to it (they did before 2026-09-30, see BUILD-LOG.md).

Set BLIMPG_TEST_DSN to override. The guard refuses any DSN whose database name does
not end in `_test`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

MESH = Path(__file__).resolve().parents[1]
ROOT = MESH.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DSN = os.environ.get("BLIMPG_TEST_DSN", "postgresql://logan@/blimp_mesh_test?port=5433")


def _dbname(dsn: str) -> str:
    m = re.search(r"/([A-Za-z0-9_]+)(\?|$)", dsn)
    return m.group(1) if m else ""


if not _dbname(TEST_DSN).endswith("_test"):
    raise RuntimeError(f"refusing to run mesh tests against a non-test database: {_dbname(TEST_DSN)!r}")

# Modules read BLIMPG_DSN at import time, so this must be set before any mesh import.
os.environ["BLIMPG_DSN"] = TEST_DSN
# Tests run command checks on the host inside tmp dirs; production refuses that (sandbox required).
os.environ.setdefault("BLIMP_SANDBOX", "none")
# The default test worktree root; tests that need a real directory create <root>/<task id>.
os.environ.setdefault("BLIMP_WORKTREE_ROOTS", "/home/user/blimp/worktrees")


def apply_fresh_schema(dsn: str = TEST_DSN) -> None:
    """Drop and re-create the mesh schema in the TEST database, then apply every migration in order."""
    import psycopg

    assert _dbname(dsn).endswith("_test"), dsn
    migrations = sorted((MESH / "migrations").glob("*.sql"))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS mesh CASCADE")
        for m in migrations:
            conn.execute(m.read_text())


def _db_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(TEST_DSN, connect_timeout=3):
            return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture()
def fresh_db():
    """A clean mesh schema for one test. Skips (loudly) if Postgres is unreachable."""
    if not DB_AVAILABLE:
        pytest.skip(f"test database unavailable: {TEST_DSN}")
    apply_fresh_schema()
    return TEST_DSN


def mkwt(task_id: str) -> str:
    """Create <root>/<task_id> under the current BLIMP_WORKTREE_ROOTS (the one worktree rule), with
    a `flag` symlink to <root>/../flag so tests can flip a check's outcome from one place."""
    root = Path(os.environ["BLIMP_WORKTREE_ROOTS"].split(":")[0])
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    link = d / "flag"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(root.parent / "flag")
    return str(d)


@pytest.fixture(autouse=True)
def _worktree_root(tmp_path, monkeypatch):
    """Every test gets its own worktree root (tmp_path/wt) and a flag file at tmp_path/flag."""
    (tmp_path / "wt").mkdir(exist_ok=True)
    monkeypatch.setenv("BLIMP_WORKTREE_ROOTS", str(tmp_path / "wt"))
    return tmp_path / "wt"
