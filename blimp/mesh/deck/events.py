"""Blimp Crew v2 — Deck kernel: event store with hash chain (spec §10.1 events, §15 mesh.events).

Zero LLM. Deterministic. Append-only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

GENESIS_HASH = b"\x00" * 32
# pg_advisory_xact_lock key that serializes appends to mesh.events (arbitrary, fixed).
CHAIN_LOCK_KEY = 7_406_120_001


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canon_ts(ts: datetime) -> str:
    """Timestamp as hashed: UTC, microseconds (Postgres precision), independent of session TZ."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def append_event(
    cur: psycopg.Cursor,
    actor: str,
    type: str,
    payload: dict[str, Any],
    subject_type: str | None = None,
    subject_id: str | None = None,
    causation_id: int | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Append one event using the caller's cursor, so a state change and its event commit
    (or roll back) together: "projections update read models in the same transaction"
    (spec 10.1)."""
    # One writer at a time for the whole chain. A row lock on the latest event does not
    # stop two writers forking the chain from the same predecessor (and locks nothing on
    # an empty table); a transaction-scoped advisory lock does.
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))
    cur.execute("SELECT hash FROM mesh.events ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    prev_hash = bytes(row[0]) if row else GENESIS_HASH

    # Hash exactly what Postgres will store: JSONB normalizes numbers (1e16 -> <ID>,
    # -0.0 -> 0) and key order, so hashing the Python value would make verify_chain fail later.
    cur.execute("SELECT %s::jsonb", (Jsonb(payload),))
    payload = cur.fetchone()[0]

    ts = _now()
    body = {
        "ts": _canon_ts(ts),
        "actor": actor,
        "type": type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "payload": payload,
        "causation_id": causation_id,
        "correlation_id": correlation_id,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    event_hash = hashlib.sha256(prev_hash + canonical.encode()).digest()
    cur.execute(
        """
        INSERT INTO mesh.events
          (ts, actor, type, subject_type, subject_id, payload,
           causation_id, correlation_id, prev_hash, hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, ts
        """,
        (ts, actor, type, subject_type, subject_id, Jsonb(payload), causation_id, correlation_id, prev_hash, event_hash),
    )
    event_id, ts = cur.fetchone()
    return {"id": event_id, **body, "ts": ts, "hash": event_hash.hex()}


class EventStore:
    """Hash-chained append-only event log. `hash = sha256(prev_hash ‖ canonical_json(event))`.

    The canonical event covers ts, actor, type, subject, payload, causation and correlation,
    so editing any stored field (including the timestamp) breaks the chain.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn)

    def append(
        self,
        actor: str,
        type: str,
        payload: dict[str, Any],
        subject_type: str | None = None,
        subject_id: str | None = None,
        causation_id: int | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one event in its own transaction."""
        with self._conn() as conn, conn.cursor() as cur:
            return append_event(cur, actor, type, payload, subject_type, subject_id, causation_id, correlation_id)

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute the full hash chain. Returns (ok, first_broken_event_id)."""
        expected_prev = GENESIS_HASH
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, actor, type, subject_type, subject_id, payload, "
                "causation_id, correlation_id, prev_hash, hash FROM mesh.events ORDER BY id"
            )
            for row in cur.fetchall():
                (
                    event_id,
                    ts,
                    actor,
                    type,
                    subject_type,
                    subject_id,
                    payload,
                    causation_id,
                    correlation_id,
                    prev_hash,
                    event_hash,
                ) = row
                if bytes(prev_hash or GENESIS_HASH) != expected_prev:
                    return False, event_id
                body = {
                    "ts": _canon_ts(ts),
                    "actor": actor,
                    "type": type,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "payload": payload if isinstance(payload, dict) else json.loads(payload),
                    "causation_id": causation_id,
                    "correlation_id": correlation_id,
                }
                canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
                if hashlib.sha256(expected_prev + canonical.encode()).digest() != bytes(event_hash):
                    return False, event_id
                expected_prev = bytes(event_hash)
        return True, None

    def events_for_subject(self, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, actor, type, payload FROM mesh.events "
                "WHERE subject_type=%s AND subject_id=%s ORDER BY id",
                (subject_type, subject_id),
            )
            return [
                {"id": r[0], "ts": r[1], "actor": r[2], "type": r[3], "payload": r[4]}
                for r in cur.fetchall()
            ]
