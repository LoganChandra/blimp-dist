"""Discord Bridge outbound (spec §18.1, §8.7): thread per task, cards edited in place, personas,
exactly-once effect, retries, dry-run safety, and the real REST shapes."""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blimp.mesh.bridge.bridge import Bridge
from blimp.mesh.bridge.discord_rest import DiscordTransport
from blimp.mesh.deck.board import Board

from .test_board import LEDGER, contract, msg


class FakeDiscord:
    def __init__(self, fail_times=0):
        self.threads, self.posts, self.edits = [], [], []
        self.fail_times = fail_times
        self.n = 0

    def _id(self):
        self.n += 1
        return f"{1000 + self.n}"

    def _maybe_fail(self):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("429 rate limited")

    def create_thread(self, channel, title, content, persona, nonce=None):
        self._maybe_fail()
        tid, mid = self._id(), self._id()
        self.threads.append((channel, title, content, tid, mid))
        return tid, mid

    def post(self, channel, thread_id, content, persona, nonce=None):
        self._maybe_fail()
        mid = self._id()
        self.posts.append((channel, thread_id, content, persona, mid))
        return mid

    def edit(self, channel, thread_id, message_id, content, persona):
        self._maybe_fail()
        self.edits.append((channel, thread_id, message_id, content))


@pytest.fixture()
def world(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    fake = FakeDiscord()
    return b, Bridge(fresh_db, fake, out=lambda *_: None), fake


def cycle(br, n=3):
    for _ in range(n):
        br.project()
        br.drain()


def test_thread_per_task_with_card_and_edits(world):
    b, br, fake = world
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"]), "hermes")
    b.make_ready(t["id"], "duke")
    b.claim(t["id"], "duke")
    b.ledger_update(t["id"], "duke", LEDGER)
    cycle(br)
    assert len(fake.threads) == 1
    ch, title, content, thread_id, card_id = fake.threads[0]
    assert ch == "board" and title == f"{t['id']} · Intake" and content.startswith(f"[{t['id']}] Intake")
    assert fake.edits, "state changes edit the card in place"
    assert all(e[2] == card_id for e in fake.edits)
    assert "WORKING" in fake.edits[-1][3] and "Next: scaffold" in fake.edits[-1][3]
    assert fake.posts == []  # no new posts for state changes: no notification noise
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT discord_thread_id FROM mesh.threads WHERE context_id=%s", (t["context_id"],)).fetchone()[0] == thread_id


def test_agent_messages_post_as_persona_in_thread(world):
    b, br, fake = world
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.send(msg((t["id"], t["context_id"])))
    cycle(br)
    thread_id = fake.threads[0][3]
    assert len(fake.posts) == 1
    ch, th, content, persona, _ = fake.posts[0]
    assert (ch, th, persona) == ("board", thread_id, "hermes") and content.startswith("[QUERY] Hermes → Duke")


def test_inform_and_status_fold_into_one_edited_timeline(world):
    b, br, fake = world
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    for i in range(3):
        b.send(msg((t["id"], t["context_id"]), act="INFORM", text=f"INFORM: step {i} finished"))
    cycle(br)
    timeline_posts = [p for p in fake.posts if p[2].startswith("Timeline")]
    assert len(timeline_posts) == 1
    assert any("step 2 finished" in e[3] for e in fake.edits)


def test_private_messages_never_mirrored_and_hq_summarized(world):
    b, br, fake = world
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.send(msg((t["id"], t["context_id"]), visibility="private", text="QUERY: about client PII artifact AR-0001?"))
    b.send(msg((t["id"], t["context_id"]), visibility="hq", text="QUERY: can we ship Friday?"))
    cycle(br)
    texts = [p[2] for p in fake.posts]
    assert not any("PII" in x for x in texts)
    assert any(p[0] == "hq" and "ship Friday" in p[2] for p in fake.posts)


def test_exactly_once_under_reprojection(world):
    b, br, fake = world
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.send(msg((t["id"], t["context_id"])))
    cycle(br)
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.bridge_cursor SET last_event_id=0")  # replay the whole log
    cycle(br)
    assert len(fake.threads) == 1 and len(fake.posts) == 1


def test_retries_then_delivers(world):
    b, br, fake = world
    fake.fail_times = 2
    b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    br.project()
    br.drain()
    with psycopg.connect(b.dsn) as c:
        attempts, err = c.execute("SELECT attempts, last_error FROM mesh.outbox").fetchone()
        assert attempts == 1 and "429" in err
        c.execute("UPDATE mesh.outbox SET next_attempt_at=now()")
    br.drain()
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.outbox SET next_attempt_at=now()")
    br.drain()
    assert len(fake.threads) == 1


def test_dry_run_posts_nothing_and_stays_pending(fresh_db):
    b = Board(fresh_db)
    b.create_task(title="Intake", type="maintenance", created_by="frank")
    logs = []
    br = Bridge(fresh_db, None, out=logs.append)
    br.project()
    s = br.drain()
    assert s["dry_run"] == 1 and s["sent"] == 0 and any("[dry-run] thread" in l for l in logs)
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT status FROM mesh.outbox").fetchone()[0] == "pending"


def test_backfill_skips_history(world):
    b, br, fake = world
    b.create_task(title="old", type="maintenance", created_by="frank")
    br.backfill()
    b.create_task(title="new", type="maintenance", created_by="frank")
    cycle(br)
    assert [t[1].split(" · ")[1] for t in fake.threads] == ["new"]


# ── the real REST client, against a fake HTTP opener ─────────────────────────
class FakeHTTP:
    def __init__(self, fail_429=False):
        self.calls = []
        self.fail_429 = fail_429

    def __call__(self, req, timeout=20):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        if self.fail_429:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"retry_after": 1.2}'))
        rid = str(900 + len(self.calls))

        class R(io.BytesIO):
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R(json.dumps({"id": rid}).encode())


def rest(http):
    return DiscordTransport("BOT", {"board": "111", "hq": "222"}, {"board": "https://discord.com/api/webhooks/9/tok"},
                            {"duke": {"name": "Duke", "avatar_url": "https://x/d.png"}}, opener=http)


def test_rest_thread_from_card_message():
    http = FakeHTTP()
    tid, mid = rest(http).create_thread("board", "T-0001 · Intake", "[T-0001] card", "deck")
    (m1, u1, h1, b1), (m2, u2, _, b2) = http.calls
    assert (m1, u1) == ("POST", "https://discord.com/api/v10/channels/111/messages") and h1["Authorization"] == "Bot BOT"
    assert b1["allowed_mentions"] == {"parse": []}  # a card can never ping @everyone
    assert u2 == f"https://discord.com/api/v10/channels/111/messages/{mid}/threads" and b2["name"] == "T-0001 · Intake"
    assert tid == "902"


def test_rest_persona_post_uses_webhook_without_bot_token():
    http = FakeHTTP()
    rest(http).post("board", "555", "hi", "duke")
    m, u, h, b = http.calls[0]
    assert u == "https://discord.com/api/webhooks/9/tok?wait=true&thread_id=555"
    assert "Authorization" not in h and b["username"] == "Duke"


def test_rest_clips_to_discord_limit_and_raises_on_429():
    http = FakeHTTP()
    rest(http).post("board", "555", "x" * 5000, "deck")
    assert len(http.calls[0][3]["content"]) <= 2000
    with pytest.raises(Exception) as e:
        rest(FakeHTTP(fail_429=True)).post("board", "555", "x", "deck")
    assert "429" in str(e.value)


def test_approval_cards_wait_for_the_decision_window(world):
    from blimp.mesh.bridge.bridge import next_window
    from blimp.mesh.toolgate.gate import Toolgate
    b, br, fake = world
    g = Toolgate(b.dsn)
    ap = g.request_approval(requested_by="frank", executor="frank", tool="deploy.prod_deploy", args={"app": "<your-product>", "ref": "abc123"},
                            why="ships intake", rollback="redeploy @9f2e", evidence=["RC-0001"])
    urgent = g.request_approval(requested_by="frank", executor="frank", tool="deploy.rollback", args={"app": "<your-product>"},
                                why="sev2 5xx", rollback="roll forward", urgent=True)
    cycle(br)
    posted = [p[2] for p in fake.posts if p[0] == "approvals"]
    assert len(posted) == 1 and posted[0].startswith(f"APPROVAL {urgent['id']} · rollback <your-product>")
    with psycopg.connect(b.dsn) as c:
        when = c.execute("SELECT next_attempt_at FROM mesh.outbox WHERE idempotency_key=%s", (f"approval:{ap['id']}",)).fetchone()[0]
    assert when == next_window() and when > datetime.now(timezone.utc)
    g.decide_approval(urgent["id"], "logan", True, note="go")
    cycle(br)
    assert any(p[2] == f"{urgent['id']} approved by the Boss · go" for p in fake.posts)


def test_next_window_in_myt():
    from blimp.mesh.bridge.bridge import next_window
    myt = timezone(timedelta(hours=8))
    assert next_window(datetime(2026, 10, 1, 8, 0, tzinfo=myt)) == datetime(2026, 10, 1, 9, 30, tzinfo=myt)
    assert next_window(datetime(2026, 10, 1, 12, 0, tzinfo=myt)) == datetime(2026, 10, 1, 17, 30, tzinfo=myt)
    assert next_window(datetime(2026, 10, 1, 18, 0, tzinfo=myt)) == datetime(2026, 10, 2, 9, 30, tzinfo=myt)
