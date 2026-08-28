import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest  # noqa: E402
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import load_queue, save_queue   # noqa: E402


def _rec(n, id_, status="proposed"):
    return QueueRecord(number=n, first_seen="2026-06-23T08:00:00Z",
                       last_seen="2026-06-23T08:00:00Z",
                       item=WorkItem(schema_version=1, id=id_, repo="pb-www", kind="mr",
                                     executor="magi-review", risk="low", why="w",
                                     web_url="u", sha="abc", status=status))


def test_load_missing_file_returns_empty():
    assert load_queue("/nonexistent/dir/queue.json") == []


def test_save_then_load_roundtrips():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sub", "queue.json")   # parent dir does not exist yet
        recs = [_rec(1, "magi:pb-www!1@abc"), _rec(2, "review:pb-www!2", status="approved")]
        save_queue(p, recs)
        out = load_queue(p)
        assert [r.number for r in out] == [1, 2]
        assert out[1].item.status == "approved"
        assert out[0].item.id == "magi:pb-www!1@abc"


def test_save_is_atomic_no_partial_on_existing():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        save_queue(p, [_rec(1, "a")])
        save_queue(p, [_rec(1, "a"), _rec(2, "b")])   # overwrite
        assert len(load_queue(p)) == 2
        # no leftover temp files in the dir
        assert [f for f in os.listdir(tmp) if f.endswith(".json")] == ["queue.json"]


def test_load_malformed_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        with open(p, "w") as f:
            f.write("{ not valid json")
        assert load_queue(p) == []


def test_roundtrip_preserves_first_and_last_seen():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        rec = QueueRecord(number=7, first_seen="2026-06-01T00:00:00Z",
                          last_seen="2026-06-23T00:00:00Z",
                          item=WorkItem(schema_version=1, id="x", repo="r", kind="mr",
                                        executor="review", risk="low", why="w",
                                        web_url="u", sha="def"))
        save_queue(p, [rec])
        out = load_queue(p)
        assert out[0].first_seen == "2026-06-01T00:00:00Z"
        assert out[0].last_seen == "2026-06-23T00:00:00Z"
        assert out[0].item.sha == "def"


def test_save_queue_uses_a_unique_temp_name_per_write(tmp_path):
    """F3: a FIXED `path + '.tmp'` is shared by every writer -- two concurrent
    writers interleave their bytes into the same temp file and os.replace then
    publishes the mixture as the whole queue."""
    import glob, threading
    from worksweep.queue import save_queue, load_queue
    from worksweep.models import WorkItem, QueueRecord

    qp = os.path.join(str(tmp_path), "queue.json")

    def _recs(n, tag):
        return [QueueRecord(number=i, first_seen="t", last_seen=tag,
                            item=WorkItem(schema_version=1, id=f"{tag}{i}",
                                          repo="pb-www", kind="mr",
                                          executor="magi-review", risk="low",
                                          why="w" * 400, web_url="u", sha="s"))
                for i in range(1, n + 1)]

    errors = []

    def writer(tag):
        try:
            for _ in range(15):
                save_queue(qp, _recs(60, tag))
        except Exception as e:                       # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,))
               for t in ("aaaa", "bbbb", "cccc")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    # the published file is always ONE writer's complete payload, never a mix
    records = load_queue(qp)
    assert len(records) == 60
    assert len({r.last_seen for r in records}) == 1
    # and no temp files are left lying around
    assert glob.glob(os.path.join(str(tmp_path), ".queue-*")) == []
    assert not os.path.exists(qp + ".tmp")


# --- dismiss: the dashboard's retire-a-manual-row transition -----------------

def _drec(n, executor="triage", status="proposed", kind="todo", sha="",
          ident=None):
    from worksweep.models import WorkItem, QueueRecord
    return QueueRecord(
        number=n, first_seen="2026-08-01T00:00:00Z", last_seen="2026-08-01T00:00:00Z",
        item=WorkItem(schema_version=1,
                      id=ident or f"todo:assigned:https://gl/x/-/work_items/{n}",
                      repo="", kind=kind, executor=executor, risk="low", why="w",
                      web_url=f"https://gl/x/-/work_items/{n}", sha=sha,
                      status=status))


def test_is_dismissable_matrix():
    from worksweep.queue import is_dismissable
    for ex in ("triage", "mr-hygiene", "none", "review"):
        assert is_dismissable(_drec(1, executor=ex).item) is True
        assert is_dismissable(_drec(1, executor=ex, status="done").item) is False
        assert is_dismissable(_drec(1, executor=ex, status="error").item) is False
    # anything the runner claims is approve-territory, never dismiss-territory
    for ex in ("magi-review", "keep-current", "implement"):
        assert is_dismissable(_drec(1, executor=ex).item) is False


def test_dismiss_flips_only_the_named_dismissable_record():
    from worksweep.queue import dismiss
    recs = [_drec(1), _drec(2), _drec(3, executor="magi-review")]
    out, flipped = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    assert flipped is not None and flipped.number == 1
    by = {r.number: r for r in out}
    assert (by[1].item.status, by[1].item.done_reason) == ("done", "dismissed")
    assert by[1].first_seen == "2026-08-01T00:00:00Z"      # preserved
    assert by[1].last_seen == "2026-08-25T09:00:00Z"       # bumped
    assert by[2].item.status == "proposed"
    assert by[3].item.status == "proposed"


def test_dismiss_returns_none_for_ineligible_or_missing_records():
    from worksweep.queue import dismiss
    recs = [_drec(1, executor="magi-review"), _drec(2, status="done")]
    for number in (1, 2, 99):
        out, flipped = dismiss(recs, number, "2026-08-25T09:00:00Z")
        assert flipped is None, number
        assert out == recs, number


def test_a_dismissed_todo_stays_dismissed_when_the_gitlab_todo_is_cleared():
    """The todo drops out of the sweep entirely; `done` is retained, not
    re-proposed."""
    from worksweep.queue import dismiss, reconcile
    recs = [_drec(1)]
    out, _ = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    after = reconcile(out, [], "2026-08-25T13:00:00Z")
    assert len(after) == 1
    assert (after[0].item.status, after[0].item.done_reason) == ("done", "dismissed")


def test_a_dismissed_todo_stays_dismissed_even_if_gitlab_still_lists_it():
    """The case that actually matters here: worksweep cannot mark the GitLab
    todo done (no id is carried), so the todo comes back in every sweep. It
    must NOT resurrect -- todo items have sha "" on both sides, so reconcile's
    same-sha branch keeps the record `done`."""
    from worksweep.queue import dismiss, reconcile
    recs = [_drec(1)]
    out, _ = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    still_pending = out[0].item.__class__(
        **{**out[0].item.__dict__, "status": "proposed", "done_reason": ""})
    after = reconcile(out, [still_pending], "2026-08-25T13:00:00Z")
    assert len(after) == 1
    assert after[0].item.status == "done"
    assert after[0].item.done_reason == "dismissed"
    assert after[0].number == 1                    # and keeps its handle


# --- todo_id across sweeps ---------------------------------------------------

def _todo_item(todo_id, status="proposed"):
    from worksweep.models import WorkItem
    return WorkItem(schema_version=1,
                    id="todo:assigned:https://gl/x/-/work_items/9", repo="",
                    kind="todo", executor="triage", risk="low",
                    why="assigned on MergeRequest",
                    web_url="https://gl/x/-/work_items/9", sha="",
                    status=status, todo_id=todo_id)


def test_reconcile_refreshes_todo_id_from_the_fresh_sweep_item():
    """No carry is needed, and a carry would actively be WRONG here.

    Todos have sha "" on both sides, so reconcile's same-sha branch fires every
    sweep and rebuilds from the FRESH item -- which now carries todo_id. That is
    what heals a legacy record written before the field existed: it picks the id
    up on the next sweep with no migration. (dev_box/mr_iid are carried from the
    prior item because the executor owns those; the todo id is upstream data, so
    the fresh value is the authoritative one.)
    """
    from worksweep.models import QueueRecord
    from worksweep.queue import reconcile
    prior = [QueueRecord(number=5, first_seen="2026-08-20T09:00:00Z",
                         last_seen="2026-08-20T09:00:00Z", item=_todo_item(0))]
    out = reconcile(prior, [_todo_item(77)], "2026-08-25T09:00:00Z")
    assert len(out) == 1
    assert out[0].item.todo_id == 77          # legacy 0 healed by the sweep
    assert out[0].number == 5                 # and it keeps its approval handle
    assert out[0].item.status == "proposed"


def test_reconcile_does_not_carry_a_stale_todo_id_over_a_fresh_one():
    from worksweep.models import QueueRecord
    from worksweep.queue import reconcile
    prior = [QueueRecord(number=5, first_seen="2026-08-20T09:00:00Z",
                         last_seen="2026-08-20T09:00:00Z", item=_todo_item(11))]
    out = reconcile(prior, [_todo_item(22)], "2026-08-25T09:00:00Z")
    assert out[0].item.todo_id == 22


def test_a_dismissed_todo_keeps_its_id_and_stays_done():
    """The dismissed record is retained verbatim, so its id survives whether or
    not the todo is still pending upstream."""
    from worksweep.models import QueueRecord
    from worksweep.queue import dismiss, reconcile
    prior = [QueueRecord(number=5, first_seen="2026-08-20T09:00:00Z",
                         last_seen="2026-08-20T09:00:00Z", item=_todo_item(77))]
    dismissed, _ = dismiss(prior, 5, "2026-08-25T09:00:00Z")
    for fresh in ([_todo_item(77)], []):       # still pending, and cleared
        out = reconcile(dismissed, fresh, "2026-08-25T13:00:00Z")
        assert len(out) == 1
        assert out[0].item.status == "done"
        assert out[0].item.done_reason == "dismissed"
        assert out[0].item.todo_id == 77


def test_a_queue_written_before_todo_id_existed_still_loads(tmp_path):
    """Additive field: an old queue.json has no `todo_id` key at all."""
    import json as _json
    from worksweep.queue import load_queue
    qp = os.path.join(str(tmp_path), "queue.json")
    item = {"schema_version": 1, "id": "todo:assigned:u", "repo": "",
            "kind": "todo", "executor": "triage", "risk": "low", "why": "w",
            "web_url": "u", "sha": "", "status": "proposed"}
    _json.dump([{"number": 3, "first_seen": "t", "last_seen": "t", "item": item}],
               open(qp, "w"))
    records = load_queue(qp)
    assert len(records) == 1
    assert records[0].item.todo_id == 0
    assert records[0].number == 3


# --- reconcile observes the fresh-wins reset of an approved record ----------

def _mr_item(sha, status="approved", iid=4078):
    from worksweep.models import WorkItem
    return WorkItem(schema_version=1, id=f"mr:pb-www!{iid}", repo="pb-www",
                    kind="mr", executor="magi-review", risk="low",
                    why="review requested",
                    web_url=f"https://gl/x/-/merge_requests/{iid}",
                    sha=sha, status=status)


def _prior(number, item):
    from worksweep.models import QueueRecord
    return QueueRecord(number=number, first_seen="2026-08-24T09:00:00Z",
                       last_seen="2026-08-24T09:00:00Z", item=item)


def test_reconcile_reports_an_approved_record_reset_by_a_new_sha():
    """Falsifying: drop the observation and the ✅ is revoked silently, which
    is exactly the live bug -- the item just reappears as `proposed`."""
    from worksweep.queue import reconcile
    prior = [_prior(214, _mr_item("aaa", iid=4078)),
             _prior(215, _mr_item("aaa", iid=4076))]
    fresh = [_mr_item("bbb", iid=4078), _mr_item("bbb", iid=4076)]
    resets = set()
    out = reconcile(prior, fresh, "2026-08-25T09:00:00Z", resets=resets)
    assert resets == {214, 215}
    assert {r.item.status for r in out} == {"proposed"}


def test_reconcile_reports_nothing_when_the_sha_is_unchanged():
    from worksweep.queue import reconcile
    prior = [_prior(214, _mr_item("aaa"))]
    resets = set()
    out = reconcile(prior, [_mr_item("aaa")], "2026-08-25T09:00:00Z",
                    resets=resets)
    assert resets == set()
    assert out[0].item.status == "approved"      # the ✅ still stands


@pytest.mark.parametrize("prior_status,expected_status", [
    ("error", "proposed"),        # a retry, not a revoked decision
    ("done", "proposed"),         # a resurrection, not a revoked decision
    ("proposed", "proposed"),     # a no-op
    # A live claim now reconciles wholly from the prior record (fix-mode
    # round 2): re-proposing a `running` row on a new sha left the executor
    # mid-flight against a row that no longer said it was running.
    ("running", "running"),
    ("needs-input", "needs-input"),
])
def test_only_an_approved_reset_is_reported(prior_status, expected_status):
    """Every other transition through the same branch leaves the set empty --
    none of them revokes a human's ✅."""
    from worksweep.queue import reconcile
    prior = [_prior(214, _mr_item("aaa", status=prior_status))]
    resets = set()
    out = reconcile(prior, [_mr_item("bbb")], "2026-08-25T09:00:00Z",
                    resets=resets)
    assert resets == set()
    assert out[0].item.status == expected_status


def test_reconcile_without_the_out_param_still_works():
    """30 existing call sites pass no `resets`; the default must be inert."""
    from worksweep.queue import reconcile
    out = reconcile([_prior(214, _mr_item("aaa"))], [_mr_item("bbb")],
                    "2026-08-25T09:00:00Z")
    assert out[0].item.status == "proposed"


def test_reconcile_reports_only_the_records_that_actually_reset():
    from worksweep.queue import reconcile
    prior = [_prior(1, _mr_item("aaa", iid=1)),            # moves -> reported
             _prior(2, _mr_item("aaa", iid=2)),            # unchanged
             _prior(3, _mr_item("aaa", iid=3, status="error"))]  # retry
    fresh = [_mr_item("bbb", iid=1), _mr_item("aaa", iid=2), _mr_item("bbb", iid=3)]
    resets = set()
    reconcile(prior, fresh, "2026-08-25T09:00:00Z", resets=resets)
    assert resets == {1}


# --- address-feedback rows get BOTH controls (2026-08-28) ----------------
#
# Three permanent proposed rows whose entire content was "LGTM" could not be
# dismissed, because dismissability was "non-runnable" and this executor is
# runnable. Approve and dismiss are not alternatives here: approve runs it,
# dismiss says "I read it and there is nothing to run".

def _fb_item(status="proposed", executor="address-feedback"):
    from worksweep.models import WorkItem
    return WorkItem(schema_version=1, id="feedback:pb-www!4084", repo="pb-www",
                    kind="feedback", executor=executor, risk="low",
                    why="1 unaddressed thread", web_url="u", sha="s",
                    status=status, note_refs=(("d1", "101"),))


def test_an_address_feedback_row_is_dismissable():
    """FALSIFYING. Runnable used to mean undismissable, which left the LGTM
    rows with no resolution at all."""
    from worksweep.queue import is_dismissable
    assert is_dismissable(_fb_item()) is True


def test_it_is_still_approvable_too():
    """Both controls. Dismiss is not a substitute for running it."""
    from worksweep import dashboard
    assert dashboard.has_checkbox(_fb_item()) is True


def test_other_runnable_rows_stay_undismissable():
    """The safety gate that made this rule is unchanged everywhere else:
    dismissing a magi or implement row silently drops real work."""
    from worksweep.queue import is_dismissable
    for executor in ("magi-review", "keep-current", "implement", "park"):
        assert is_dismissable(_fb_item(executor=executor)) is False, executor


def test_a_terminal_feedback_row_is_not_dismissable_again():
    from worksweep.queue import is_dismissable
    for status in ("done", "error"):
        assert is_dismissable(_fb_item(status=status)) is False, status


def test_dismissing_records_the_notes_it_saw(tmp_path):
    """The point of the whole round: the dismissal has to outlive the row."""
    from worksweep.queue import dismiss
    from worksweep.models import QueueRecord
    now = "2026-08-28T12:00:00+00:00"
    rec = QueueRecord(number=7, first_seen=now, last_seen=now,
                      item=_fb_item())
    out, dismissed = dismiss([rec], 7, now)
    assert dismissed is not None
    assert out[0].item.status == "done"
    assert out[0].item.done_reason == "dismissed"
    assert dismissed.item.note_refs == (("d1", "101"),)
