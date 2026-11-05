#!/usr/bin/env python3
"""
discord_mirror.py — Blimp Mailroom visibility plane.

Mirrors A2A message state transitions into the per-agent Discord channels the Boss
reads, with real-Discord-message-ID proof-of-delivery, deduped one-post-per
(task_id, state) via the a2a_discord_posts outbox table.

ROUTING (matches the Boss's literal requirement): a message TO agent B and its
lifecycle receipts appear in B's Discord channel — "your message to Duke shows
up in his DM; his reply to Nanook shows up in Nanook's DM." Routed by the
RECIPIENT (to_agent) of each row.

THE LADDER the Boss sees for one message (task_id is the visible correlation id):
  submitted      -> "From {from} -> {to}: <message text>"   (he sees the message)
  working        -> "{to} received & started"               (delivery receipt)
  input-required -> "{to} needs input"
  completed      -> "{to} completed: <summary>"
  failed         -> "{to} failed: <reason>"

SAFE + ADDITIVE: only READS a2a_messages, WRITES a2a_discord_posts, and POSTS to
Discord via `hermes send --json`. It does NOT touch message delivery — the
broker's notify path is independent — so it cannot break inter-agent comms.

Proof-of-delivery: hermes send --json returns {success, message_id}; the
message_id is stored as the only honest proof a receipt was delivered. No id =>
not delivered => left pending (retried next pass).

Run:
  python discord_mirror.py --once     # drain pending receipts and exit (cron/test)
  python discord_mirror.py --backfill # mark all CURRENT states as already-posted
                                       #   (run ONCE before first use so historical
                                       #    messages are not retroactively posted)
  python discord_mirror.py            # poll loop
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ.get(
    "DUKE_DB_URL",
    "postgresql:///duke_data?host=/var/run/postgresql&port=5433",
)
HERMES = os.environ.get("HERMES_BIN", "hermes")
POLL_SECS = int(os.environ.get("MIRROR_POLL_SECS", "5"))

# Single source of truth for per-agent channels.
#   - nanook was MISSING from the old mcp _DISCORD_CHANNELS map.
#   - hermes ...849 is the CHANNEL (...552 in CLAUDE.md is the BOT/application id).
CHANNELS = {
    "duke":   "<ID>",
    "nanook": "<ID>",
    "hermes": "<ID>",
}

MIRRORED_STATES = ("submitted", "working", "input-required", "completed", "failed", "canceled")
MAX_BODY = 1500

# SAFETY: the mirror NEVER posts to a real Discord channel unless explicitly
# enabled with MIRROR_LIVE=1. Default = dry-run (log intended posts, record
# nothing). This makes accidental live-channel spam impossible during testing.
LIVE = os.environ.get("MIRROR_LIVE") == "1"


def _conn():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def _text_of(body) -> str:
    try:
        parts = body["params"]["message"]["parts"]
        return "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()
    except Exception:
        return ""


def _summary(result) -> str:
    if not result:
        return ""
    if isinstance(result, dict):
        for k in ("status", "summary", "text"):
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(result)[:MAX_BODY]
    return str(result)[:MAX_BODY]


def render(row) -> str | None:
    """The Discord text for this row's CURRENT state, or None to skip."""
    st, tid = row["state"], row["task_id"]
    frm, to = row["from_agent"], row["to_agent"]
    if st == "submitted":
        msg = _text_of(row["body"])
        if len(msg) > MAX_BODY:
            msg = msg[:MAX_BODY] + f"\n…(truncated · task {tid})"
        return f"📨 **{frm} → {to}** · task `{tid}`\n{msg}"
    if st == "working":
        return f"✅ **{to} received & started** · task `{tid}`"
    if st == "input-required":
        return f"⏳ **{to} needs input** · task `{tid}`"
    if st == "completed":
        s = _summary(row["result"])
        return f"☑️ **{to} completed** · task `{tid}`" + (f": {s[:MAX_BODY]}" if s else "")
    if st == "failed":
        why = row.get("dlq_reason") or row.get("fail_reason") or ""
        return f"⚠️ **{to} failed** · task `{tid}`" + (f": {why[:500]}" if why else "")
    if st == "canceled":
        return f"🚫 **task canceled** · task `{tid}`"
    return None


def post_discord(channel_id: str, text: str) -> str | None:
    """Post via `hermes send --json`; return the Discord message_id or None."""
    if not LIVE:
        print(f"[mirror][DRY-RUN] would post -> {channel_id}: {text[:90]!r}", file=sys.stderr)
        return None
    try:
        r = subprocess.run(
            [HERMES, "send", "--to", f"discord:{channel_id}", "--json", text],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode != 0:
            print(f"[mirror] hermes send rc={r.returncode}: {r.stderr.strip()[:200]}", file=sys.stderr)
            return None
        s = r.stdout.strip()
        i = s.find("{")  # hermes emits pretty-printed multi-line JSON; parse the whole object
        if i < 0:
            print(f"[mirror] no JSON in hermes output: {s[:200]}", file=sys.stderr)
            return None
        out = json.loads(s[i:])
        return out.get("message_id") if out.get("success") else None
    except Exception as e:
        print(f"[mirror] post error: {e}", file=sys.stderr)
        return None


def _record(conn, task_id, state, channel_id, message_id, body):
    posted = message_id is not None
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO a2a_discord_posts
                 (task_id, state, channel_id, discord_message_id, body, attempt_count, posted_at)
               VALUES (%s,%s,%s,%s,%s,1, CASE WHEN %s THEN NOW() ELSE NULL END)
               ON CONFLICT (task_id, state) DO UPDATE
                 SET discord_message_id = COALESCE(a2a_discord_posts.discord_message_id, EXCLUDED.discord_message_id),
                     attempt_count = a2a_discord_posts.attempt_count + 1,
                     posted_at     = COALESCE(a2a_discord_posts.posted_at, EXCLUDED.posted_at)""",
            [task_id, state, channel_id, message_id, body, posted],
        )
    conn.commit()


def drain_once(conn) -> int:
    """Post a receipt for every (task_id, current state) not yet delivered. Returns count posted."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.id, m.task_id, m.parent_task_id, m.from_agent, m.to_agent,
                      m.state, m.body, m.result, m.fail_reason, m.dlq_reason
               FROM a2a_messages m
               LEFT JOIN a2a_discord_posts p
                 ON p.task_id = m.task_id AND p.state = m.state
                 AND p.discord_message_id IS NOT NULL
               WHERE p.task_id IS NULL
                 AND m.parent_task_id IS NULL
                 AND m.state = ANY(%s)
               ORDER BY m.updated_at ASC
               LIMIT 50""",
            [list(MIRRORED_STATES)],
        )
        roots = cur.fetchall()
        # Replies (child rows) also surface — to the REPLIER's recipient channel.
        cur.execute(
            """SELECT m.id, m.task_id, m.parent_task_id, m.from_agent, m.to_agent,
                      m.state, m.body, m.result, m.fail_reason, m.dlq_reason
               FROM a2a_messages m
               LEFT JOIN a2a_discord_posts p
                 ON p.task_id = m.task_id AND p.state = ('reply:' || m.id::text)
                 AND p.discord_message_id IS NOT NULL
               WHERE p.task_id IS NULL
                 AND m.parent_task_id IS NOT NULL
                 AND m.body ? 'params'
               ORDER BY m.updated_at ASC
               LIMIT 50""",
        )
        replies = cur.fetchall()

    posted = 0
    for row in roots:
        text = render(row)
        channel = CHANNELS.get(row["to_agent"])
        if not text or not channel:
            _record(conn, row["task_id"], row["state"], channel or "-", None, "(skip: no channel/text)")
            continue
        mid = post_discord(channel, text)
        _record(conn, row["task_id"], row["state"], channel, mid, text[:500])
        posted += 1 if mid else 0

    for row in replies:
        msg = _text_of(row["body"])
        if not msg:
            continue
        channel = CHANNELS.get(row["to_agent"])
        state_key = "reply:" + str(row["id"])
        if not channel:
            _record(conn, row["task_id"], state_key, "-", None, "(skip: no channel)")
            continue
        text = f"💬 **{row['from_agent']} → {row['to_agent']}** (reply) · task `{row['task_id']}`\n{msg[:MAX_BODY]}"
        mid = post_discord(channel, text)
        _record(conn, row["task_id"], state_key, channel, mid, text[:500])
        posted += 1 if mid else 0
    return posted


def backfill(conn) -> int:
    """Mark every CURRENT (task_id, state) as already-posted so historical messages
    are not retroactively spammed. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO a2a_discord_posts (task_id, state, channel_id, discord_message_id, body, posted_at)
               SELECT task_id, state, '-', '(baseline)', '(backfill: pre-mirror baseline)', NULL
               FROM a2a_messages
               WHERE parent_task_id IS NULL
               ON CONFLICT (task_id, state) DO NOTHING"""
        )
        n1 = cur.rowcount
        cur.execute(
            """INSERT INTO a2a_discord_posts (task_id, state, channel_id, discord_message_id, body, posted_at)
               SELECT task_id, 'reply:' || id::text, '-', '(baseline)', '(backfill: pre-mirror baseline)', NULL
               FROM a2a_messages
               WHERE parent_task_id IS NOT NULL
               ON CONFLICT (task_id, state) DO NOTHING"""
        )
        n2 = cur.rowcount
    conn.commit()
    return (n1 or 0) + (n2 or 0)


def main():
    ap = argparse.ArgumentParser(description="Blimp Mailroom Discord receipt mirror")
    ap.add_argument("--once", action="store_true", help="drain pending receipts once and exit")
    ap.add_argument("--backfill", action="store_true", help="mark current states as already-posted (run once before first use)")
    args = ap.parse_args()

    with _conn() as conn:
        if args.backfill:
            n = backfill(conn)
            print(f"[mirror] backfilled {n} (task_id,state) baseline rows")
            return
        if args.once:
            n = drain_once(conn)
            print(f"[mirror] posted {n} receipt(s)")
            return
        print(f"[mirror] poll loop every {POLL_SECS}s (Ctrl-C to stop)")
        while True:
            try:
                drain_once(conn)
            except Exception as e:
                print(f"[mirror] drain error: {e}", file=sys.stderr)
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
