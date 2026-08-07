import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord, DiscordMessage  # noqa: E402
from worksweep.approvals import apply_approvals  # noqa: E402

USER = "chandler-123"
OTHER = "colleague-999"
T0 = "2026-06-23T08:00:00Z"
T1 = "2026-06-23T09:00:00Z"


def _rec(n, status="proposed"):
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=WorkItem(schema_version=1, id=f"id{n}", repo="pb-www",
                                     kind="mr", executor="magi-review", risk="low",
                                     why="w", web_url="u", sha="abc", status=status))


def _msg(author_id, content, mid="1"):
    return DiscordMessage(id=mid, author_id=author_id, content=content, timestamp=T1)


def _by_num(records):
    return {r.number: r for r in records}


def test_approval_from_user_flips_named_proposed_items():
    recs = [_rec(1), _rec(2), _rec(3)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,3")], USER, T1)
    by = _by_num(out)
    assert by[1].item.status == "approved"
    assert by[3].item.status == "approved"
    assert by[2].item.status == "proposed"
    assert approved == {1, 3}


def test_approved_record_last_seen_is_bumped():
    out, _ = apply_approvals([_rec(1)], [_msg(USER, "✅ 1")], USER, T1)
    assert _by_num(out)[1].last_seen == T1


def test_approval_from_other_author_is_ignored():
    recs = [_rec(1), _rec(2)]
    out, approved = apply_approvals(recs, [_msg(OTHER, "✅ 1,2")], USER, T1)
    assert approved == set()
    for r in out:
        assert r.item.status == "proposed"


def test_number_with_no_matching_record_is_noop():
    out, approved = apply_approvals([_rec(1)], [_msg(USER, "✅ 1,9")], USER, T1)
    assert approved == {1}                      # 9 has no record -> not approved
    assert _by_num(out)[1].item.status == "approved"


def test_already_approved_is_idempotent_and_not_in_returned_set():
    # #1 is already approved; re-approving must keep it approved and NOT report it
    # as newly approved (so the confirmation only names freshly flipped items).
    recs = [_rec(1, status="approved"), _rec(2)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,2")], USER, T1)
    by = _by_num(out)
    assert by[1].item.status == "approved"
    assert by[2].item.status == "approved"
    assert approved == {2}


def test_multiple_messages_union_only_user_authored():
    recs = [_rec(1), _rec(2), _rec(3)]
    msgs = [_msg(USER, "✅ 1", mid="1"),
            _msg(OTHER, "✅ 2", mid="2"),
            _msg(USER, "approve 3", mid="3")]
    out, approved = apply_approvals(recs, msgs, USER, T1)
    assert approved == {1, 3}
    assert _by_num(out)[2].item.status == "proposed"


def test_non_approval_user_message_changes_nothing():
    recs = [_rec(1)]
    out, approved = apply_approvals(recs, [_msg(USER, "looks good")], USER, T1)
    assert approved == set()
    assert _by_num(out)[1].item.status == "proposed"
