#!/usr/bin/env python3
"""stop-brain-capture.py: every Discord turn lands in the team brain automatically.

Why (2026-09-27): logging depended on each agent *remembering* to call log_message. It decayed from about
400 turns a month (July) to about 20 (Sept). Duke then told the Boss the last thing he said to Frank was on
11 Sep, when they had talked all day. The fix is the pattern the field uses (claude-mem, Hermes memory
providers, Letta sleep-time agents): capture at the runtime layer, not by model discipline.

What it does, as a Claude Code Stop hook (or by hand with --backfill):
  - reads the session transcript;
  - finds every inbound Discord message (<channel source="plugin:discord:discord" ... message_id=...>)
    and every outbound reply tool call (mcp__plugin_discord_discord__reply);
  - writes each one ONCE to Hindsight, as a chat memory with the real Discord timestamp and
    document_id = discord:<id>. Re-runs are idempotent (update_mode=replace on the same id).

Never blocks and never wedges the agent: the Stop hook forks, returns 0 at once, and every failure fails
open. Stdlib + httpx only (the blimp venv).

Agent = $BLIMP_AGENT, else inferred from the session cwd (nanook -> "nanook", blimp or duke -> "duke").
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "postgres"))

STATE_DIR = Path.home() / ".cache" / "blimp-brain-capture"
TAIL_BYTES = 4_000_000  # a Stop hook only needs the recent part of a long transcript
INBOUND = re.compile(
    r'<channel source="plugin:discord:discord"[^>]*?message_id="(\d+)"[^>]*?user="([^"]*)"[^>]*?ts="([^"]+)"[^>]*>\n?(.*?)\n?</channel>',
    re.S,
)
REPLY_TOOL = "mcp__plugin_discord_discord__reply"
MAX_CHARS = 4000


def agent_for(cwd: str) -> str:
    if os.getenv("BLIMP_AGENT"):
        return os.environ["BLIMP_AGENT"]
    c = cwd.lower()
    if "nanook" in c:
        return "nanook"
    if "blimp" in c or "duke" in c:
        return "duke"
    return ""


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def extract(transcript: Path, tail: bool = True, since: str = "") -> list[dict]:
    """Discord turns in the transcript, oldest first: {id, speaker, ts, text}."""
    size = transcript.stat().st_size
    with transcript.open("rb") as f:
        if tail and size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()  # drop the partial first line
        raw = f.read().decode("utf-8", "replace")
    seen: dict[str, dict] = {}
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        stamp = d.get("timestamp") or ""
        if d.get("type") == "assistant":
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == REPLY_TOOL:
                    text = ((c.get("input") or {}).get("text") or "").strip()
                    if text:
                        seen[c["id"]] = {"id": c["id"], "speaker": "agent", "ts": stamp, "text": text}
            continue
        for s in _strings(d):
            if "plugin:discord:discord" not in s:
                continue
            for mid, user, ts, text in INBOUND.findall(s):
                if mid not in seen and text.strip():
                    seen[mid] = {"id": mid, "speaker": "logan", "user": user, "ts": ts, "text": text.strip()}
    out = [m for m in seen.values() if m["ts"] and m["ts"] >= since]
    return sorted(out, key=lambda m: m["ts"])


def load_done(agent: str) -> set[str]:
    try:
        return set(json.loads((STATE_DIR / f"{agent}.json").read_text()))
    except Exception:
        return set()


def save_done(agent: str, done: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    keep = sorted(done)[-20000:]
    tmp = STATE_DIR / f"{agent}.json.tmp"
    tmp.write_text(json.dumps(keep))
    tmp.replace(STATE_DIR / f"{agent}.json")


def write(agent: str, msgs: list[dict], dry: bool = False) -> int:
    import hindsight_client as hc  # noqa: WPS433 (deferred: only the child process needs httpx)

    done = load_done(agent)
    todo = [m for m in msgs if m["id"] not in done]
    if dry:
        return len(todo)
    for i in range(0, len(todo), 20):
        batch = todo[i : i + 20]
        items = []
        for m in batch:
            speaker = agent if m["speaker"] == "agent" else "logan"
            text = m["text"][:MAX_CHARS]
            items.append(
                hc._memory_item(
                    f"[{speaker}] {text}",
                    context=f"Discord chat in #{agent} between the Boss and {agent}",
                    tags=["conversation", f"channel:{agent}", f"speaker:{speaker}", f"author:{speaker}", "capture:auto"],
                    metadata={"kind": "chat", "user": "logan", "channel": agent, "speaker": speaker,
                              "author": speaker, "verified": "True", "discord_id": m["id"], "source": "stop-brain-capture"},
                    document_id=f"discord:{agent}:{m['id']}",
                    timestamp=m["ts"],
                )
            )
        hc._post(hc._bank_path("/memories"), {"items": items, "async": True})
        done.update(m["id"] for m in batch)
        save_done(agent, done)
    return len(todo)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--backfill":
        # --backfill <agent> <since-ISO> [--dry] <transcript>...
        agent, since = args[1], args[2]
        dry = "--dry" in args
        files = [Path(a) for a in args[3:] if a != "--dry"]
        msgs: dict[str, dict] = {}
        for f in files:
            for m in extract(f, tail=False, since=since):
                msgs.setdefault(m["id"], m)
        ordered = sorted(msgs.values(), key=lambda m: m["ts"])
        n = write(agent, ordered, dry=dry)
        print(json.dumps({"agent": agent, "found": len(ordered), "new": n, "dry": dry}))
        return 0

    # Stop hook: read the payload, fork, return at once.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    transcript = Path(payload.get("transcript_path") or "")
    agent = agent_for(payload.get("cwd") or os.getcwd())
    if not agent or not transcript.is_file():
        return 0
    if os.fork() != 0:
        return 0
    try:
        os.setsid()
        log = STATE_DIR / "capture.log"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            n = write(agent, extract(transcript))
            if n:
                with log.open("a") as f:
                    f.write(f"{time.strftime('%F %T')} {agent} +{n}\n")
        except Exception as e:  # fail open, but leave a trace
            with log.open("a") as f:
                f.write(f"{time.strftime('%F %T')} {agent} ERROR {e!r}\n")
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
