"""Run: ~/blimp/mesh-venv/bin/pytest blimp-infra/mcp/test_messagelog.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import messagelog as ml  # noqa: E402

DOCS = {
    "discord:duke:200": "Han (2026-11-05 17:46 UTC): check the journey\n\nDuke (2026-11-05 17:47 UTC): on it",
    "discord:duke:100": "Han (2026-11-05 17:46 UTC): <@1>\n\nDuke (2026-11-05 17:46 UTC): yes?",
    "discord:duke:300": ("Han (display name) (2026-11-05 18:00 UTC): line one\n\nstill the same message\n\n"
                         "the Boss (2026-11-05 18:01 UTC): ok\n\nDuke (2026-11-05 18:02 UTC): done"),
}


def fake_get(path, params):
    if path.endswith("/documents"):
        items = [{"id": k} for k in DOCS] if params["offset"] == 0 else []
        return {"items": items}
    return {"original_text": DOCS[path.rsplit("/", 1)[-1]]}


def run(**kw):
    return ml.messages(fake_get, lambda s: s, "duke", **kw)


def test_parse_keeps_multi_paragraph_messages_whole():
    m = ml.parse(DOCS["discord:duke:300"])
    assert [x["who"] for x in m] == ["Han", "the Boss", "Duke"] and m[0]["text"] == "line one\n\nstill the same message"
    assert m[0]["unverified_name"] and not m[1]["unverified_name"]


def test_exact_order_and_positions_for_one_speaker():
    r = run(speaker="han", limit=10)
    assert r["total"] == 3
    assert [x["text"] for x in r["messages"]] == ["line one\n\nstill the same message", "check the journey", "<@1>"]
    assert [x["position_from_newest"] for x in r["messages"]] == [1, 2, 3]


def test_same_minute_tie_broken_by_discord_id_not_list_order():
    r = run(newest_first=False, limit=10)
    assert [x["text"] for x in r["messages"]][:3] == ["<@1>", "yes?", "check the journey"]


def test_limit_and_everyone():
    r = run(limit=2)
    assert r["total"] == 7 and len(r["messages"]) == 2 and r["messages"][0]["text"] == "done"
