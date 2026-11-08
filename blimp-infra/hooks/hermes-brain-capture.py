#!/usr/bin/env python3
"""hermes-brain-capture.py: Hermes's Discord conversations land in the team brain, same as Duke's and Frank's.

Why: stop-brain-capture.py is a Claude Code Stop hook, and Hermes doesn't run on Claude Code, so he was
left out (the Boss 2026-11-06: "all of you are equal"). Hermes keeps every turn in ~/.hermes/state.db, so
this reads that (read-only) and hands the messages to stop-brain-capture.py's writer: same grouping,
same names, same secret stripping, same idempotent documents (discord:hermes:<first message id>).

  hermes-brain-capture.py --session <id>              one session (the gateway hook calls this)
  hermes-brain-capture.py --backfill <since-ISO> [--dry]   every Discord session since a date

Inbound = user rows carrying a Discord message id (platform_message_id). In shared channels the gateway
prefixes "[Name]", a display name anyone can set, so those speakers are stored as unverified names; in
a DM the sender is the session's user id, mapped like Duke's and Frank's.
Outbound = assistant rows that ended the turn (finish_reason "stop"): what Hermes actually sent back.
Stdlib only; fails open, logging to ~/.cache/blimp-brain-capture/capture.log.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("cap", HERE / "stop-brain-capture.py")
cap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cap)

DB = Path(os.getenv("HERMES_STATE_DB", str(Path.home() / ".hermes" / "state.db")))
NAME = re.compile(r"\A\[([^\]\n]{1,64})\] ?")  # "[the Boss] text": shared sessions only, a display name
HEADER = re.compile(r"\A\[(?:[^\[\]]|\[[^\[\]]*\])*\]\Z", re.S)
SILENT = re.compile(r"\A\s*(\[SILENT\]|NO_REPLY)", re.I)


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)


def _body(content: str) -> str:
    """Drop the gateway's header blocks (trigger id, "Replying to", channel context, attachment notes),
    each a paragraph that is one whole [...] group, and keep what the person wrote."""
    # On a reply or in a thread the gateway puts recent channel history first, then "[New message]".
    if "[New message]\n" in content:
        content = content.rsplit("[New message]\n", 1)[1]
    blocks = content.strip().split("\n\n")
    while len(blocks) > 1 and HEADER.match(blocks[0].strip()):
        blocks.pop(0)
    return "\n\n".join(blocks).strip()


def session_messages(con: sqlite3.Connection, sid: str, aliases: dict) -> list[dict]:
    row = con.execute("select chat_id, chat_type, user_id from sessions where id=? and source='discord'", (sid,)).fetchone()
    if not row:
        return []
    chat, chat_type, owner = str(row[0] or ""), row[1] or "", str(row[2] or "")
    msgs, seen = [], set()
    # Every row, archived ones too: compaction archives the originals and re-inserts copies, so ids and
    # text are de-duplicated instead of trusting `active`.
    for rid, role, content, ts, finish, summ, pmid in con.execute(
            "select id, role, content, timestamp, finish_reason, _compressed_summary, platform_message_id "
            "from messages where session_id=? and role in ('user','assistant') order by id", (sid,)):
        text = (content or "").strip()
        if not text or summ:
            continue
        if role == "user":
            mid = str(pmid or "")
            if not mid.isdigit() or mid in seen:
                continue  # no Discord message id: a system notice or summary, not something a person sent
            body = _body(text)
            m = NAME.match(body)
            if m:
                # A display name, which anyone can set: kept as a name, never treated as an identity.
                name, body = m.group(1).strip(), body[m.end():].strip()
                who_id, who, unverified = "", aliases.get(name.lower(), name), True
            else:
                # Not a shared session (a DM): the gateway knows the sender, it's the session's user.
                who_id, who, unverified = (owner if chat_type == "dm" else ""), "", chat_type != "dm"
            if not body:
                continue
            seen.add(mid)
            msgs.append({"id": mid, "ts": _iso(ts), "out": False, "who_id": who_id, "who": who,
                         "unverified": unverified, "chat_id": chat, "text": body})
        elif finish == "stop" and not SILENT.match(text):
            key = ("out", text)  # compaction copies may get a new timestamp, so the text is the key
            if key in seen:
                continue
            seen.add(key)
            msgs.append({"id": f"h{rid}", "ts": _iso(ts), "out": True, "who_id": "", "chat_id": chat, "text": text})
    return msgs


def aliases() -> dict:
    return {k.lower(): v for k, v in (cap.names().get("aliases") or {}).items()}


def main() -> int:
    args = sys.argv[1:]
    try:
        con = _connect(DB)
    except Exception as e:
        cap._log(f"hermes ERROR {e!r}"[:500])
        return 0
    al = aliases()
    if args[:1] == ["--session"] and len(args) > 1:
        try:
            n = cap.write("hermes", session_messages(con, args[1], al))
            if n:
                cap._log(f"hermes +{n}")
        except Exception as e:
            cap._log(f"hermes ERROR {e!r}"[:500])
        return 0
    if args[:1] == ["--backfill"] and len(args) > 1:
        since = dt.datetime.fromisoformat(args[1].replace("Z", "+00:00"))
        since = since if since.tzinfo else since.replace(tzinfo=dt.timezone.utc)
        dry = "--dry" in args
        n = found = 0
        sids = [r[0] for r in con.execute(
            "select id from sessions where source='discord' and coalesce(ended_at, started_at) >= ? order by started_at",
            (since.timestamp(),))]
        for sid in sids:
            msgs = session_messages(con, sid, al)
            found += len(msgs)
            n += cap.write("hermes", msgs, dry=dry)
        print(json.dumps({"agent": "hermes", "sessions": len(sids), "messages": found, "exchanges_to_send": n, "dry": dry}))
        if not dry:
            cap._log(f"hermes backfill +{n}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
