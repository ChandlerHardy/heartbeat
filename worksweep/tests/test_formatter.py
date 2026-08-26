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
    assert "**1.** " in joined and f"**{n}.** " in joined  # first and last survive


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
    assert "**1.** " in out
    assert "**3.** " in out
    assert "2. " not in out


def test_digest_from_records_renders_in_number_order():
    # records given out of order must still render ascending by number
    recs = [_rec(3, _wi(30, why="third")), _rec(1, _wi(10, why="first"))]
    out = format_digest_from_records(recs)
    assert out.index("**1.** ") < out.index("**3.** ")


def test_messages_from_records_empty_is_all_clear():
    msgs = format_messages_from_records([])
    assert len(msgs) == 1 and "nothing needs you" in msgs[0].lower()


def test_messages_from_records_uses_persisted_number():
    recs = [_rec(7, _wi(42, why="lucky"))]
    joined = "\n".join(format_messages_from_records(recs))
    assert "**7.** " in joined
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


def test_format_messages_respects_max_bytes_cap():
    # Verify that max_bytes parameter actually works (not shadowed by now param)
    # With max_bytes=200, many items should split into multiple messages
    items = [_wi(i, why="x" * 50) for i in range(50)]
    messages = format_messages(items, max_bytes=200)
    # With such a small cap, we should get multiple messages
    assert len(messages) > 3, f"Expected > 3 messages with max_bytes=200, got {len(messages)}"
    # All messages should respect the cap
    for msg in messages:
        enc = msg.encode("utf-8")
        assert len(enc) <= 200, f"Message exceeds max_bytes=200: {len(enc)} bytes"


# M4 Task F — dev-slot preamble line renders once, right under the header.
_SLOT_LINE = "Dev slots: dev1 free · dev4, dev5 reclaimable (approved, awaiting merge) · dev0 live"


def test_digest_from_records_renders_preamble_under_header():
    rec = _rec(1, _wi(1))
    out = format_digest_from_records([rec], preamble=_SLOT_LINE)
    lines = out.splitlines()
    assert lines[0].startswith("###")
    # header is now two lines (title + bold counts); preamble renders as
    # subtext directly beneath them
    assert lines[2] == f"-# {_SLOT_LINE}"


def test_digest_from_records_no_preamble_when_none():
    rec = _rec(1, _wi(1))
    out = format_digest_from_records([rec])
    assert _SLOT_LINE not in out


def test_digest_from_records_no_preamble_when_all_clear():
    # Nothing to preface when there are no items at all.
    out = format_digest_from_records([], preamble=_SLOT_LINE)
    assert _SLOT_LINE not in out
    assert "nothing needs you" in out.lower()


def test_messages_from_records_renders_preamble_under_header():
    rec = _rec(1, _wi(1))
    msgs = format_messages_from_records([rec], preamble=_SLOT_LINE)
    lines = msgs[0].splitlines()
    assert lines[0].startswith("###")
    # header is now two lines (title + bold counts); preamble renders as
    # subtext directly beneath them
    assert lines[2] == f"-# {_SLOT_LINE}"


def test_messages_from_records_preamble_survives_multipart_split():
    items = [_wi(i, why="x" * 50) for i in range(50)]
    recs = [_rec(i, it) for i, it in enumerate(items, 1)]
    msgs = format_messages_from_records(recs, max_bytes=300, preamble=_SLOT_LINE)
    assert len(msgs) > 1
    assert _SLOT_LINE in msgs[0]


def test_format_messages_and_digest_m1_path_accept_preamble():
    # M1 (no-queue) entry points thread preamble too, for symmetry.
    out = format_digest([_wi(1)], preamble=_SLOT_LINE)
    assert out.splitlines()[2] == f"-# {_SLOT_LINE}"
    msgs = format_messages([_wi(1)], preamble=_SLOT_LINE)
    assert msgs[0].splitlines()[2] == f"-# {_SLOT_LINE}"


# --------------------------------------------------------------------------
# 2026-08-24: auto-flowing keep-current items collapse to one subtext line
# --------------------------------------------------------------------------

def _stale_wi(i, status="approved"):
    import dataclasses
    return dataclasses.replace(
        _wi(i, executor="keep-current", why="7 commits behind master"),
        kind="stale", status=status)


def test_keep_current_items_collapse_to_one_auto_line():
    out = format_digest([_wi(1), _stale_wi(2), _stale_wi(3)])
    assert "auto-merging master into 2 branch(es)" in out
    assert "[#2]" in out and "[#3]" in out
    assert "7 commits behind master" not in out       # no per-item lines
    assert out.count("auto-merging master into") == 1


def test_header_counts_actionable_auto_and_handoff_separately():
    import dataclasses
    handoff = dataclasses.replace(_wi(4), kind="handoff")
    out = format_digest([_wi(1), _stale_wi(2), handoff])
    assert "**1 need you** · 1 auto-merging · 1 handed off" in out


def test_needs_input_keep_current_stays_actionable():
    out = format_digest([_stale_wi(2, status="needs-input")])
    assert "auto-merging" not in out
    assert "`keep-current`" in out                    # renders as its own line
    assert "7 commits behind master" in out


def test_handoff_lines_render_as_subtext():
    import dataclasses
    handoff = dataclasses.replace(_wi(4), kind="handoff")
    out = format_digest([_wi(1), handoff])
    assert "-# **Handed off (no action):**" in out
    assert "\n-# ✅ **2.**" in out


def test_footer_documents_the_blanket_approval():
    """AC #7: `✅ all` is only discoverable if the digest footer names it."""
    from worksweep.formatter import _FOOTER
    assert "✅ all" in _FOOTER
    # the numbered form stays documented too -- the blanket form is an addition,
    # not a replacement
    assert "✅ 1,3" in _FOOTER


def test_format_reproposed_line_shape():
    """Bold numbers (Discord eats a leading `214.` as an ordered-list marker)
    and masked refs (25 raw URLs would spawn 25 embed cards)."""
    from worksweep.formatter import format_reproposed
    from worksweep.models import WorkItem

    def _it(iid):
        return WorkItem(schema_version=1, id=f"mr:pb-www!{iid}", repo="pb-www",
                        kind="mr", executor="magi-review", risk="low", why="w",
                        web_url=f"https://gl/x/-/merge_requests/{iid}", sha="s")

    line = format_reproposed([(214, _it(4078)), (215, _it(4076))])
    assert line == ("↩️ re-proposed (MR changed since your ✅): "
                    "**214** [#4078](https://gl/x/-/merge_requests/4078), "
                    "**215** [#4076](https://gl/x/-/merge_requests/4076)")
    assert line.count("**") == 4
    assert not line.startswith("214")          # never a bare list marker


def test_format_reproposed_is_empty_for_no_resets():
    from worksweep.formatter import format_reproposed
    assert format_reproposed([]) == ""


# --- the auto-approved re-review reads as auto (2026-08-26) ---------------

def _auto_magi(number=13, sha="newsha123"):
    from worksweep.runner import AUTO_MAGI_WHY
    return QueueRecord(
        number=number, first_seen="2026-08-26T00:00:00+00:00",
        last_seen="2026-08-26T00:00:00+00:00",
        item=WorkItem(schema_version=1, id=f"magi:pb-www!3997@{sha}",
                      repo="pb-www", kind="mr", executor="magi-review",
                      risk="low", why=AUTO_MAGI_WHY,
                      web_url="https://gl/x/-/merge_requests/3997", sha=sha,
                      status="approved", title="Ranch data tab"))


def test_the_digest_marks_a_self_approved_review_as_auto():
    """It reached `approved` with no ✅, so the line has to say so -- an
    unexplained approved row reads as something Chandler forgot doing."""
    from worksweep.formatter import format_digest
    out = format_digest_from_records([_auto_magi()])
    assert "(auto)" in out
    assert "post-feedback re-review" in out


def test_the_auto_review_keeps_its_own_line_and_number():
    """It must NOT collapse into the keep-current auto-merge line: the
    curator's validator requires every approved magi number to appear, so a
    collapsed one would fail the digest outright."""
    from worksweep.formatter import format_digest
    out = format_digest_from_records([_auto_magi()])
    assert "**13.**" in out
    assert "auto-merging" not in out
