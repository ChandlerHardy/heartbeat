import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.formatter import (  # noqa: E402
    format_digest, format_messages, DISCORD_MAX_CHARS,
    format_digest_from_records, format_messages_from_records,
)


def _wi(i, executor="magi-review", why="why"):
    return WorkItem(schema_version=1, id=f"x{i}", repo="pb-www", kind="mr",
                    executor=executor, risk="low", why=why,
                    web_url=f"https://gitlab.com/x/-/merge_requests/{i}", sha="abc")


def _rec(number, wi):
    return QueueRecord(number=number, first_seen="t", last_seen="t", item=wi)


def test_empty_digest_says_all_clear():
    assert "nothing needs you" in format_digest([]).lower()


def test_empty_messages_is_single_all_clear():
    msgs = format_messages([])
    assert len(msgs) == 1 and "nothing needs you" in msgs[0].lower()


def test_digest_numbers_items():
    out = format_digest([_wi(1), _wi(2)])
    assert "1." in out and "2." in out


def test_digest_includes_executor_and_why():
    out = format_digest([_wi(1, executor="mr-hygiene", why="missing dev link")])
    assert "mr-hygiene" in out and "missing dev link" in out


def test_item_uses_masked_link_no_raw_embed():
    # masked link form [#ref](url) keeps Discord from rendering an embed card
    out = format_digest([_wi(42)])
    assert "[#42](https://gitlab.com/x/-/merge_requests/42)" in out


def test_small_list_is_a_single_message():
    assert len(format_messages([_wi(1), _wi(2)])) == 1


def test_long_digest_splits_into_multiple_messages():
    msgs = format_messages([_wi(i) for i in range(300)])
    assert len(msgs) > 1


def test_every_message_within_byte_cap_multibyte_safe():
    msgs = format_messages([_wi(i, why="café 🔭 needs review urgently now please")
                            for i in range(300)])
    for m in msgs:
        enc = m.encode("utf-8")
        assert len(enc) <= DISCORD_MAX_CHARS
        assert enc.decode("utf-8") == m  # no partial multibyte tail


def test_all_items_present_across_messages():
    n = 80
    joined = "\n".join(format_messages([_wi(i) for i in range(n)]))
    assert "1. " in joined and f"{n}. " in joined  # first and last survive


def test_oversized_single_item_line_truncated_byte_safe():
    huge = "x🔭" * 1000  # > 1900 bytes, multibyte
    msgs = format_messages([_wi(1, why=huge)])
    for m in msgs:
        enc = m.encode("utf-8")
        assert len(enc) <= DISCORD_MAX_CHARS
        assert enc.decode("utf-8") == m


# M2 — render from queue records using the PERSISTED number (not enumerate)
def test_digest_from_records_uses_persisted_numbers():
    # records numbered 1 and 3 (number 2 dropped from an earlier sweep) must
    # render as "1." and "3." — never re-enumerate to "1."/"2."
    recs = [_rec(1, _wi(10, why="first")), _rec(3, _wi(30, why="third"))]
    out = format_digest_from_records(recs)
    assert "1. " in out
    assert "3. " in out
    assert "2. " not in out


def test_digest_from_records_renders_in_number_order():
    # records given out of order must still render ascending by number
    recs = [_rec(3, _wi(30, why="third")), _rec(1, _wi(10, why="first"))]
    out = format_digest_from_records(recs)
    assert out.index("1. ") < out.index("3. ")


def test_messages_from_records_empty_is_all_clear():
    msgs = format_messages_from_records([])
    assert len(msgs) == 1 and "nothing needs you" in msgs[0].lower()


def test_messages_from_records_uses_persisted_number():
    recs = [_rec(7, _wi(42, why="lucky"))]
    joined = "\n".join(format_messages_from_records(recs))
    assert "7. " in joined
    assert "[#42](https://gitlab.com/x/-/merge_requests/42)" in joined


# Age marker tests (Task B)
def test_age_marker_on_6day_old_record():
    # A record with first_seen 6 days ago should render with ⏳6d marker
    import datetime
    now = "2026-08-17T00:00:00+00:00"
    six_days_ago = (datetime.datetime.fromisoformat(now) -
                    datetime.timedelta(days=6)).isoformat()
    rec = QueueRecord(number=1, item=_wi(1, why="old item"),
                      first_seen=six_days_ago, last_seen=now)
    output = format_digest_from_records([rec], now=now)
    assert "⏳6d" in output


def test_age_marker_not_on_fresh_record():
    # A record with first_seen less than 5 days ago should not have marker
    import datetime
    now = "2026-08-17T00:00:00+00:00"
    one_day_ago = (datetime.datetime.fromisoformat(now) -
                   datetime.timedelta(days=1)).isoformat()
    rec = QueueRecord(number=1, item=_wi(1, why="fresh item"),
                      first_seen=one_day_ago, last_seen=now)
    output = format_digest_from_records([rec], now=now)
    assert "⏳" not in output


def test_age_marker_not_on_exactly_5day_old_record():
    # A record with first_seen exactly 5 days ago should NOT have marker (> 5 days)
    import datetime
    now = "2026-08-17T00:00:00+00:00"
    five_days_ago = (datetime.datetime.fromisoformat(now) -
                     datetime.timedelta(days=5)).isoformat()
    rec = QueueRecord(number=1, item=_wi(1, why="boundary item"),
                      first_seen=five_days_ago, last_seen=now)
    output = format_digest_from_records([rec], now=now)
    assert "⏳" not in output


def test_age_marker_on_unparseable_first_seen():
    # A record with unparseable first_seen should not crash and should not show marker
    now = "2026-08-17T00:00:00+00:00"
    rec = QueueRecord(number=1, item=_wi(1, why="unparseable"),
                      first_seen="invalid-date", last_seen=now)
    output = format_digest_from_records([rec], now=now)
    assert "⏳" not in output
    assert "unparseable" in output  # item should still be rendered


def test_age_marker_not_when_now_is_none():
    # When now=None, no age markers should be rendered (backward compat)
    import datetime
    six_days_ago = "2026-08-11T00:00:00+00:00"
    rec = QueueRecord(number=1, item=_wi(1, why="old item"),
                      first_seen=six_days_ago, last_seen="2026-08-17T00:00:00+00:00")
    output = format_digest_from_records([rec], now=None)
    assert "⏳" not in output
    assert "old item" in output


def test_age_marker_in_messages_from_records():
    # Age marker should also appear in format_messages_from_records
    import datetime
    now = "2026-08-17T00:00:00+00:00"
    six_days_ago = (datetime.datetime.fromisoformat(now) -
                    datetime.timedelta(days=6)).isoformat()
    rec = QueueRecord(number=1, item=_wi(1, why="old item"),
                      first_seen=six_days_ago, last_seen=now)
    messages = format_messages_from_records([rec], now=now)
    joined = "\n".join(messages)
    assert "⏳6d" in joined
