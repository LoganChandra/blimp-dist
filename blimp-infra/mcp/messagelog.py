"""messagelog.py: exact, ordered Discord messages from the brain's saved conversations. No model calls.

recall() returns extracted facts and summaries, which merge several messages into one sentence, so
"the 11th-to-last thing Han said" or "how many times did the Boss ask for X" can't be answered from it
(Hermes's ordinal test, 2026-11-06: 14 positions off). But every captured exchange is also stored
verbatim as a document, one line per message: "Name (YYYY-MM-DD HH:MM UTC): text". This reads those.

Limits, stated so callers can say them: times are to the minute (same-minute order follows the
transcript); a message longer than the capture cap is cut; secrets are already redacted; an exchange
with no reply yet is saved once the agent replies.
"""
from __future__ import annotations

import re
from typing import Callable

LINE = re.compile(r"^(?P<who>[^\n]{1,80}?) \((?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d) UTC\): ", re.M)


def parse(text: str) -> list[dict]:
    """Split one saved conversation into messages, in order."""
    out, marks = [], list(LINE.finditer(text or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        who = m.group("who").replace(" (display name)", "").strip()
        out.append({"who": who, "unverified_name": "(display name)" in m.group("who"),
                    "ts": m.group("ts"), "text": text[m.end():end].strip()})
    return out


def messages(get: Callable[[str, dict | None], dict], bank_path: Callable[[str], str], channel: str,
             speaker: str = "", limit: int = 20, newest_first: bool = True) -> dict:
    """All captured messages in a Discord channel (name as in the capture tags: duke, frank, hermes,
    general, dm-<id>), optionally only one speaker's, exactly ordered."""
    ids, off = [], 0
    while True:
        page = get(bank_path("/documents"), {"tags": f"discord:{channel}", "limit": 200, "offset": off}).get("items") or []
        ids += [d["id"] for d in page if str(d.get("id", "")).startswith("discord:")]
        if len(page) < 200:
            break
        off += 200
    seen, rows = set(), []
    for n, doc_id in enumerate(ids):
        text = (get(bank_path(f"/documents/{doc_id}"), None) or {}).get("original_text") or ""
        for k, m in enumerate(parse(text)):
            key = (m["who"], m["ts"], m["text"])
            if key in seen:
                continue
            seen.add(key)
            # Same minute across exchanges: the document id ends in the first message's Discord
            # snowflake, which grows with time, so it breaks the tie in true order.
            snow = doc_id.rsplit(":", 1)[-1]
            rows.append({**m, "_order": (m["ts"], int(snow) if snow.isdigit() else n, k), "document_id": doc_id})
    rows.sort(key=lambda r: r["_order"])
    if speaker:
        rows = [r for r in rows if r["who"].lower() == speaker.lower()]
    total = len(rows)
    for i, r in enumerate(rows):
        r["position_from_oldest"], r["position_from_newest"] = i + 1, total - i
        del r["_order"]
    if newest_first:
        rows.reverse()
    return {"channel": channel, "speaker": speaker or "anyone", "total": total, "messages": rows[: max(0, limit)],
            "limits": "minute-resolution times; long messages cut at the capture cap; secrets redacted; "
                      "only conversations the capture saved (since 2026-09-02 backfill)"}
