"""M4 Task G: the `needs-input` status, end to end.

`needs-input` means the implementer stopped and asked Chandler a question. It
is terminal-ish: the sweep keeps the record (never drops it), reconcile NEVER
auto-re-proposes it (so the runner can't pick it up again on its own), and the
ONLY way back to `approved` is Chandler's ✅ in Discord.
"""
from worksweep.approvals import apply_approvals
from worksweep.config import load_config
from worksweep.models import DiscordMessage, QueueRecord, WorkItem
from worksweep.queue import reconcile
from worksweep.runner import pick_claim

USER = "chandler-123"
T0 = "2026-08-17T08:00:00+00:00"
T1 = "2026-08-17T09:00:00+00:00"


def _item(id_="issue:pb-www#1775", sha="", status="proposed"):
    return WorkItem(schema_version=1, id=id_, repo="pb-www", kind="issue",
                    executor="implement", risk="low", why="w",
                    web_url="https://gl/x/-/issues/1775", sha=sha, status=status,
                    title="Add cost page inline validation")


def _rec(n=1, status="needs-input", sha="", error_summary="QUESTION: which?"):
    import dataclasses
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=dataclasses.replace(_item(sha=sha, status=status),
                                                error_summary=error_summary))


def _msg(content, author_id=USER):
    return DiscordMessage(id="1", author_id=author_id, content=content,
                          timestamp=T1)


def test_needs_input_is_not_picked_by_the_runner():
    assert pick_claim([_rec()]) is None
    assert pick_claim([_rec()], ("implement",)) is None


def test_reconcile_does_not_re_propose_needs_input_when_still_present():
    out = reconcile([_rec()], [_item()], T1)
    assert len(out) == 1
    assert out[0].item.status == "needs-input"
    assert out[0].number == 1
    assert out[0].last_seen == T1


def test_reconcile_keeps_needs_input_even_when_the_sha_moves():
    out = reconcile([_rec(sha="old")], [_item(sha="new")], T1)
    assert out[0].item.status == "needs-input"
    assert out[0].item.sha == "new"


def test_reconcile_retains_needs_input_when_it_vanishes_from_the_sweep():
    out = reconcile([_rec()], [], T1)
    assert [r.item.status for r in out] == ["needs-input"]


def test_reconcile_still_re_proposes_error_items():
    """Contrast: `error` retries on the next sweep, `needs-input` does not."""
    out = reconcile([_rec(status="error")], [_item()], T1)
    assert out[0].item.status == "proposed"


def test_check_mark_flips_needs_input_back_to_approved():
    out, newly = apply_approvals([_rec()], [_msg("✅ 1")], USER, T1)
    assert out[0].item.status == "approved"
    assert newly == {1}
    assert pick_claim(out, ("implement",)).number == 1


def test_check_mark_from_another_author_does_not_unstick_needs_input():
    out, newly = apply_approvals([_rec()], [_msg("✅ 1", author_id="other")],
                                 USER, T1)
    assert out[0].item.status == "needs-input"
    assert newly == set()


def test_needs_input_survives_a_sweep_then_a_check_mark_releases_it():
    """The whole loop: halt -> sweep -> still needs-input -> ✅ -> claimable."""
    after_sweep = reconcile([_rec()], [_item()], T1)
    assert after_sweep[0].item.status == "needs-input"
    approved, _ = apply_approvals(after_sweep, [_msg("✅ 1")], USER, T1)
    assert pick_claim(approved, ("implement",)).number == 1


def test_config_implement_timeout_default_and_override(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                 '"runner": {"checkouts_root": "/co"}}')
    assert load_config(str(p)).implement_timeout == 5400
    p.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                 '"runner": {"implement_timeout": 900}}')
    assert load_config(str(p)).implement_timeout == 900


def test_config_rejects_non_integer_implement_timeout(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text('{"gitlab": {}, "runner": {"implement_timeout": "soon"}}')
    try:
        load_config(str(p))
    except RuntimeError as e:
        assert "implement_timeout" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_curated_digest_may_not_drop_a_needs_input_item():
    """A halted item is the most human-blocking thing in the queue: the
    curator validator must reject a body that omits its number."""
    from worksweep.curator import validate, partition_counts
    recs = [_rec(4)]
    assert partition_counts(recs) == (1, 0)
    assert validate("nothing to see here", recs) is False
    assert validate("4. #1775 is waiting on your answer", recs) is True


# --- C2 (Task G review): the dev-box claim must survive a sweep -------------

def test_reconcile_preserves_dev_box_and_mr_iid_on_a_running_implement_item():
    """Issue items carry sha="" so the same-sha branch fires EVERY sweep. If
    it rebuilt from the fresh item, `dev_box` would be wiped and the next
    digest would advertise an occupied box as free."""
    import dataclasses
    prior = QueueRecord(number=1, first_seen=T0, last_seen=T0,
                        item=dataclasses.replace(_item(status="running"),
                                                 dev_box="dev1", mr_iid=42,
                                                 claimed_at=T0))
    out = reconcile([prior], [_item()], T1)
    assert out[0].item.status == "running"
    assert out[0].item.dev_box == "dev1"
    assert out[0].item.mr_iid == 42
    assert out[0].item.claimed_at == T0


def test_reconcile_preserves_dev_box_on_an_approved_implement_item():
    import dataclasses
    prior = QueueRecord(number=1, first_seen=T0, last_seen=T0,
                        item=dataclasses.replace(_item(status="approved"),
                                                 dev_box="dev4"))
    out = reconcile([prior], [_item()], T1)
    assert out[0].item.status == "approved" and out[0].item.dev_box == "dev4"
