"""`blimp migrate` (spec §18.3, §21.4): apply pending mesh migrations in order.

- Tracks applied files in mesh.schema_migrations (name + sha256).
- A database where 0001 was applied by hand before tracking existed (the live duke_data,
  2026-09-28) is baselined: 0001 is recorded as applied without re-running it.
- Refuses to continue if an applied migration's file has changed since (edit-in-place
  after apply is how schemas drift).
- `--dry-run` prints the plan and changes nothing.

Spec §21.4 asks for a pre-migration snapshot of any schema change on the live database;
`--snapshot-cmd` runs a command (e.g. the Tier-1 backup) first and aborts if it fails.

Usage: python -m blimp.mesh.migrate [--dsn DSN] [--dry-run] [--snapshot-cmd "..."]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def plan(conn: psycopg.Connection) -> tuple[list[Path], list[str], bool]:
    """Returns (pending files, problems, needs_baseline)."""
    files = sorted(MIGRATIONS.glob("*.sql"))
    has_mesh = conn.execute("SELECT to_regclass('mesh.tasks') IS NOT NULL").fetchone()[0]
    has_tracking = conn.execute("SELECT to_regclass('mesh.schema_migrations') IS NOT NULL").fetchone()[0]
    applied: dict[str, str] = {}
    if has_tracking:
        applied = dict(conn.execute("SELECT name, sha256 FROM mesh.schema_migrations").fetchall())
    needs_baseline = has_mesh and not has_tracking
    problems = []
    pending = []
    for f in files:
        if f.name in applied:
            if applied[f.name] != _sha(f):
                problems.append(f"{f.name} changed after it was applied (recorded {applied[f.name][:12]}, now {_sha(f)[:12]})")
            continue
        if needs_baseline and f.name.startswith("0001_"):
            continue  # applied by hand before tracking; recorded as baseline below
        pending.append(f)
    return pending, problems, needs_baseline


def migrate(dsn: str, dry_run: bool = False, snapshot_cmd: str | None = None, out=print) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        pending, problems, needs_baseline = plan(conn)
        for p in problems:
            out(f"REFUSING: {p}")
        if problems:
            return 2
        if needs_baseline:
            out("baseline: 0001 present without tracking; will record it as applied")
        for f in pending:
            out(f"pending: {f.name}")
        if not pending and not needs_baseline:
            out("up to date")
            return 0
        if dry_run:
            out("dry run: nothing changed")
            return 0
        if snapshot_cmd:
            out(f"snapshot: {snapshot_cmd}")
            r = subprocess.run(snapshot_cmd, shell=True)
            if r.returncode != 0:
                out(f"snapshot failed (exit {r.returncode}); nothing migrated")
                return 3
        with conn.transaction():
            for f in pending:
                conn.execute(f.read_text())
                out(f"applied: {f.name}")
            if needs_baseline or pending:
                # 0002 creates the tracking table; record everything present on disk that is now applied.
                for f in sorted(MIGRATIONS.glob("*.sql")):
                    if f in pending or (needs_baseline and f.name.startswith("0001_")):
                        conn.execute(
                            "INSERT INTO mesh.schema_migrations (name, sha256) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                            (f.name, _sha(f)),
                        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", default=os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot-cmd")
    a = ap.parse_args(argv)
    return migrate(a.dsn, a.dry_run, a.snapshot_cmd)


if __name__ == "__main__":
    sys.exit(main())
