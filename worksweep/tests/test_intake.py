import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord, DiscordMessage  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
from worksweep.queue import save_queue, load_queue  # noqa: E402
import worksweep.__main__ as wsmain  # noqa: E402

USER = "chandler-123"
OTHER = "colleague-999"
T = "2026-06-23T09:00:00Z"


def _rec(n, status="proposed"):
    return QueueRecord(number=n, first_seen=T, last_seen=T,
                       item=WorkItem(schema_version=1, id=f"id{n}", repo="pb-www",
                                     kind="mr", executor="magi-review", risk="low",
                                     why="w", web_url="https://gl/x/-/merge_requests/389%d" % n,
                                     sha="abc", status=status))


def _cfg():
    return WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                           discord_webhook="https://discord.com/api/webhooks/1/x",
                           bot_token="BOT", channel_id="chan-1", discord_user_id=USER)


def _seed_queue(tmp_path):
    qp = os.path.join(str(tmp_path), "queue.json")
    save_queue(qp, [_rec(1), _rec(2), _rec(3)])
    return qp


def test_intake_approves_items_from_user_and_confirms(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: os.path.join(str(tmp_path), "cursor"))
    monkeypatch.setattr(wsmain, "fetch_messages",
                        lambda channel_id, bot_token, after=None: [
                            DiscordMessage(id="100", author_id=USER, content="✅ 1,3", timestamp=T)])
    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))

    assert wsmain.main(["intake"]) == 0

    records = load_queue(qp)
    out = {r.number: r.item.status for r in records}
    assert out[1] == "approved"
    assert out[3] == "approved"
    assert out[2] == "proposed"
    # a confirmation naming 1 and 3 was posted exactly once
    assert len(posted) == 1
    assert "Approved" in posted[0]
    # Numbering contract: the numbers in the confirmation are exactly the
    # persisted QueueRecord.numbers that flipped to approved (not render-order).
    approved_nums = sorted(r.number for r in records if r.item.status == "approved")
    assert approved_nums == [1, 3]
    for n in approved_nums:
        assert f"{n}" in posted[0]


def test_intake_ignores_other_author(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: os.path.join(str(tmp_path), "cursor"))
    monkeypatch.setattr(wsmain, "fetch_messages",
                        lambda channel_id, bot_token, after=None: [
                            DiscordMessage(id="100", author_id=OTHER, content="✅ 1,2,3", timestamp=T)])
    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))

    assert wsmain.main(["intake"]) == 0
    out = {r.number: r.item.status for r in load_queue(qp)}
    assert out == {1: "proposed", 2: "proposed", 3: "proposed"}
    assert posted == []   # nothing approved -> no confirmation


def test_intake_no_new_messages_posts_nothing(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: os.path.join(str(tmp_path), "cursor"))
    monkeypatch.setattr(wsmain, "fetch_messages",
                        lambda channel_id, bot_token, after=None: [])
    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))

    assert wsmain.main(["intake"]) == 0
    assert posted == []


def test_intake_persists_cursor_to_latest_message_id(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    cursor = os.path.join(str(tmp_path), "cursor")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: cursor)
    monkeypatch.setattr(wsmain, "fetch_messages",
                        lambda channel_id, bot_token, after=None: [
                            DiscordMessage(id="500", author_id=USER, content="✅ 1", timestamp=T),
                            DiscordMessage(id="900", author_id=USER, content="nope", timestamp=T)])
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: None)

    assert wsmain.main(["intake"]) == 0
    # cursor advances to the max message id seen so the next poll skips these
    assert os.path.exists(cursor)
    with open(cursor) as f:
        assert f.read().strip() == "900"


def test_intake_passes_cursor_as_after(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    cursor = os.path.join(str(tmp_path), "cursor")
    with open(cursor, "w") as f:
        f.write("42")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: cursor)
    seen = {}

    def fake_fetch(channel_id, bot_token, after=None):
        seen["after"] = after
        return []

    monkeypatch.setattr(wsmain, "fetch_messages", fake_fetch)
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: None)
    assert wsmain.main(["intake"]) == 0
    assert seen["after"] == "42"


def test_intake_no_bot_token_returns_1(monkeypatch, tmp_path):
    qp = _seed_queue(tmp_path)
    cfg = WorksweepConfig(repos=(), username="x", discord_webhook="x",
                          bot_token="", channel_id="", discord_user_id="")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain, "_cursor_path", lambda: os.path.join(str(tmp_path), "cursor"))
    assert wsmain.main(["intake"]) == 1
