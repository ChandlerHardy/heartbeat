import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import reconcile  # noqa: E402

T0 = "2026-06-23T08:00:00Z"
T1 = "2026-06-23T09:00:00Z"


def _item(id_, sha="abc", status="proposed"):
    return WorkItem(schema_version=1, id=id_, repo="pb-www", kind="mr",
                    executor="magi-review", risk="low", why="w",
                    web_url="u", sha=sha, status=status)


def _rec(n, id_, sha="abc", status="proposed", first=T0, last=T0):
    return QueueRecord(number=n, first_seen=first, last_seen=last,
                       item=_item(id_, sha=sha, status=status))


def _by_id(records):
    return {r.item.id: r for r in records}


def test_new_items_get_sequential_numbers():
    out = reconcile([], [_item("a"), _item("b"), _item("c")], T0)
    nums = {r.item.id: r.number for r in out}
    assert nums == {"a": 1, "b": 2, "c": 3}
    for r in out:
        assert r.item.status == "proposed"
        assert r.first_seen == T0 and r.last_seen == T0


def test_new_item_added_to_existing_gets_max_plus_one():
    existing = [_rec(1, "a"), _rec(3, "b")]   # gap at 2 — numbers need not be gapless
    out = reconcile(existing, [_item("a"), _item("b"), _item("c")], T1)
    assert _by_id(out)["c"].number == 4   # max(1,3)+1


def test_existing_item_same_sha_keeps_number_and_approved_status():
    existing = [_rec(5, "a", sha="abc", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a", sha="abc", status="proposed")], T1)
    r = _by_id(out)["a"]
    assert r.number == 5                 # stable number
    assert r.item.status == "approved"   # approval preserved, not reset to proposed
    assert r.first_seen == T0            # original first_seen kept
    assert r.last_seen == T1             # last_seen bumped to now


def test_proposed_item_gone_from_sweep_is_dropped():
    existing = [_rec(1, "a", status="proposed"), _rec(2, "b", status="proposed")]
    out = reconcile(existing, [_item("a")], T1)   # b vanished
    assert set(_by_id(out)) == {"a"}


def test_approved_item_gone_from_sweep_is_retained():
    existing = [_rec(1, "a", status="proposed"),
                _rec(2, "b", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a")], T1)   # b vanished but is approved
    ids = _by_id(out)
    assert set(ids) == {"a", "b"}
    assert ids["b"].number == 2
    assert ids["b"].item.status == "approved"
    assert ids["b"].last_seen == T0   # not re-seen this sweep, so last_seen unchanged


def test_running_item_gone_from_sweep_is_retained():
    existing = [_rec(2, "b", status="running")]
    out = reconcile(existing, [], T1)
    assert _by_id(out)["b"].item.status == "running"


def test_same_id_new_sha_resets_to_proposed_keeps_number():
    existing = [_rec(3, "a", sha="old", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a", sha="new", status="proposed")], T1)
    r = _by_id(out)["a"]
    assert r.number == 3                 # number kept
    assert r.item.sha == "new"           # sha updated
    assert r.item.status == "proposed"   # prior approval was for the old SHA -> reset
    assert r.last_seen == T1


def test_retained_numbers_are_stable_when_lower_drops():
    # #2 (proposed) drops, #3 (approved) must keep its number even though it's now
    # the only retained-by-status item — stability over density.
    existing = [_rec(1, "a"), _rec(2, "b", status="proposed"),
                _rec(3, "c", status="approved")]
    out = reconcile(existing, [_item("a")], T1)   # only a still in sweep; c approved
    ids = _by_id(out)
    assert ids["a"].number == 1
    assert ids["c"].number == 3
    assert "b" not in ids


# --- consent across an arm swap (fix-mode round 2, blocker 4) --------------
#
# The feedback id is deliberately stable while its EXECUTOR changes between
# the runnable `address-feedback` arm and the informational `triage` one. That
# made reconcile's same-sha branch launder consent: a ✅ given to an
# informational row carried over onto a row that now posts replies under
# Chandler's name.

NOW = T1


def _fb(number=3, executor="address-feedback", status="proposed",
        why="2 unaddressed threads", sha="s1", iid=3997):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"feedback:pb-www!{iid}",
                      repo="pb-www", kind="feedback", executor=executor,
                      risk="low", why=why,
                      web_url=f"https://gl/x/-/merge_requests/{iid}", sha=sha,
                      status=status, branch="chardy/1588-ranch-data"))


def test_an_approved_triage_row_does_not_carry_its_tick_onto_a_runnable_one():
    """FALSIFYING. The ✅ was given to "changes requested — go look". It must
    not silently authorise a run that replies in his name."""
    prior = [_fb(status="approved", executor="triage", why="changes requested")]
    fresh = [_fb(executor="address-feedback", why="2 unaddressed threads").item]
    resets = set()
    out = reconcile(prior, fresh, NOW, resets=resets)
    assert out[0].item.status == "proposed"
    assert out[0].item.executor == "address-feedback"
    assert out[0].number == 3                  # keeps its approval handle
    assert resets == {3}                       # and he is told why


def test_the_swap_back_also_resets():
    prior = [_fb(status="approved", executor="address-feedback")]
    fresh = [_fb(executor="triage", why="changes requested").item]
    out = reconcile(prior, fresh, NOW, resets=set())
    assert out[0].item.status == "proposed"


def test_new_threads_arriving_need_a_new_tick():
    """Same executor, same sha, but the ask grew. The ✅ covered two threads;
    the third is work he has not seen."""
    prior = [_fb(status="approved", why="2 unaddressed threads")]
    fresh = [_fb(why="3 unaddressed threads").item]
    resets = set()
    out = reconcile(prior, fresh, NOW, resets=resets)
    assert out[0].item.status == "proposed"
    assert resets == {3}


def test_an_unchanged_address_feedback_row_keeps_its_approval():
    prior = [_fb(status="approved")]
    out = reconcile(prior, [_fb().item], NOW, resets=set())
    assert out[0].item.status == "approved"


def test_a_live_claim_is_never_rewritten_by_a_sweep():
    """A `running` row is mid-flight. Merging fresh content into it would let
    a sweep rewrite the why/branch of work already in progress."""
    prior = [_fb(status="running", why="2 unaddressed threads")]
    fresh = [_fb(why="5 unaddressed threads").item]
    out = reconcile(prior, fresh, NOW, resets=set())
    assert out[0].item.status == "running"
    assert out[0].item.why == "2 unaddressed threads"
    assert out[0].last_seen == NOW             # still seen this sweep


def test_a_running_row_survives_an_executor_change_untouched():
    prior = [_fb(status="running", executor="address-feedback")]
    fresh = [_fb(executor="triage", why="changes requested").item]
    out = reconcile(prior, fresh, NOW, resets=set())
    assert out[0].item.status == "running"
    assert out[0].item.executor == "address-feedback"


def test_a_needs_input_row_unstrands_when_the_arm_changes():
    """W7: a halted address-feedback row whose signal decayed to the
    informational arm used to stay `needs-input` forever -- not dismissable,
    not runnable, and answering a question nobody could act on."""
    from worksweep.queue import is_dismissable
    prior = [_fb(status="needs-input", executor="address-feedback")]
    fresh = [_fb(executor="triage", why="changes requested").item]
    out = reconcile(prior, fresh, NOW, resets=set())
    assert out[0].item.status == "proposed"
    assert is_dismissable(out[0].item) is True


def test_a_needs_input_row_stays_halted_when_the_arm_does_not_change():
    prior = [_fb(status="needs-input", why="2 unaddressed threads")]
    out = reconcile(prior, [_fb().item], NOW, resets=set())
    assert out[0].item.status == "needs-input"


def test_other_executors_keep_their_approval_when_the_why_drifts():
    """Rule (c) is scoped to address-feedback. A keep-current row whose commit
    count moved must not lose its ✅ -- it is auto-approved anyway, and
    churning it would re-post the digest line every sweep."""
    prior = [_fb(number=4, executor="keep-current", status="approved",
                 why="7 commits behind master")]
    fresh = [_fb(number=4, executor="keep-current",
                 why="9 commits behind master").item]
    out = reconcile(prior, fresh, NOW, resets=set())
    assert out[0].item.status == "approved"


# --- stranded error rows (fix-mode round 2, warning 12) --------------------

def test_an_errored_feedback_row_closes_when_the_signal_clears():
    """The run failed, then the reviewer resolved everything themselves. The
    error row is no longer emitted, so it was retained forever -- a permanent
    ⚠️ on the dashboard for work that no longer exists."""
    prior = [_fb(status="error")]
    out = reconcile(prior, [], NOW,
                    resolved={"feedback:pb-www!3997": "signal-cleared"})
    assert out[0].item.status == "done"
    assert out[0].item.done_reason == "signal-cleared"


def test_an_errored_row_is_still_retained_for_any_other_reason():
    """Scoped deliberately: only a cleared signal closes an error row. Every
    other resolution leaves the existing retain-and-retry behaviour alone."""
    prior = [_fb(status="error")]
    out = reconcile(prior, [], NOW,
                    resolved={"feedback:pb-www!3997": "handed-off"})
    assert out[0].item.status == "error"


def test_a_cleared_signal_does_not_disturb_a_live_claim():
    prior = [_fb(status="running")]
    out = reconcile(prior, [], NOW,
                    resolved={"feedback:pb-www!3997": "signal-cleared"})
    assert out[0].item.status == "running"


# --- the auto-approved re-review vs a new push (2026-08-26) ---------------

def _auto_magi(number=13, sha="newsha123", status="approved"):
    from worksweep.runner import AUTO_MAGI_WHY
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"magi:pb-www!3997@{sha}",
                      repo="pb-www", kind="mr", executor="magi-review",
                      risk="low", why=AUTO_MAGI_WHY,
                      web_url="https://gl/x/-/merge_requests/3997", sha=sha,
                      status=status, title="Ranch data tab"))


def test_an_auto_approved_review_of_a_dead_head_is_retained_not_dropped():
    """The id carries the sha, so a new push proposes a DIFFERENT id and this
    one simply stops being emitted. `approved` keeps it (retain-if-gone)
    rather than deleting it and recycling its number."""
    prior = [_auto_magi()]
    out = reconcile(prior, [], NOW, resets=set())
    assert len(out) == 1
    assert out[0].item.status == "approved"
    assert out[0].number == 13


def test_a_fresh_push_gets_its_own_row_beside_the_old_one():
    fresh = _auto_magi(sha="evennewer").item
    out = reconcile([_auto_magi()], [fresh], NOW, resets=set())
    assert {r.item.id for r in out} == {"magi:pb-www!3997@newsha123",
                                        "magi:pb-www!3997@evennewer"}
    # the new one takes the next free number, the old keeps its handle
    assert {r.number for r in out} == {13, 14}


def test_an_auto_approved_row_re_proposed_at_a_new_sha_reports_the_reset():
    """Same id, moved sha -- the ✅ (ours, in this case) is revoked and the
    ↩️ notice has to fire like any other, so the digest explains itself."""
    import dataclasses
    prior = [_auto_magi()]
    fresh = [dataclasses.replace(prior[0].item, sha="rebased1")]
    resets = set()
    out = reconcile(prior, fresh, NOW, resets=resets)
    assert out[0].item.status == "proposed"
    assert resets == {13}
