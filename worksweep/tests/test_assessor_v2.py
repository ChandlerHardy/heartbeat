"""Review-state buckets -> items/resolutions; new own-MR item kinds."""
from worksweep.assessor import (
    assess_assigned_mr, assess_issue, assess_own_mr, assess_review_request,
    covered_issue_iids, filter_todos, resolutions)
from worksweep.models import Issue, MergeRequest, Todo, WorkItem


def _mr(**kw):
    base = dict(repo="pb-www", iid=9, title="t", author="other",
                web_url="https://gl/x/-/merge_requests/9", description="",
                sha="s9", is_draft=False, reviewers=("chandler.hardy",),
                ci_status="unknown", updated_at="")
    base.update(kw)
    return MergeRequest(**base)


def test_unreviewed_emits_review_item():
    items = assess_review_request(_mr(my_review_state="UNREVIEWED"), "chandler.hardy")
    assert [i.id for i in items] == ["review:pb-www!9"]
    assert items[0].executor == "magi-review"


def test_reviewed_emits_nothing():
    for state in ("REVIEWED", "REQUESTED_CHANGES", "APPROVED"):
        assert assess_review_request(_mr(my_review_state=state), "chandler.hardy") == []


def test_draft_review_request_included_and_tagged():
    items = assess_review_request(
        _mr(my_review_state="UNREVIEWED", is_draft=True), "chandler.hardy")
    assert len(items) == 1 and "(draft)" in items[0].why


def test_resolutions_for_waiting_states():
    mrs = [_mr(iid=1, my_review_state="REVIEWED"),
           _mr(iid=2, my_review_state="UNREVIEWED"),
           _mr(iid=3, my_review_state="REQUESTED_CHANGES")]
    assert resolutions(mrs, "chandler.hardy") == {
        "review:pb-www!1": "already-reviewed",
        "review:pb-www!3": "already-reviewed"}


def test_own_mr_feedback_and_ci_items():
    mr = _mr(author="chandler.hardy", changes_requested=True,
             unresolved_count=2, ci_status="failed")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    ids = {i.id for i in items}
    assert "feedback:pb-www!9" in ids
    assert "ci:pb-www!9" in ids
    assert "magi:pb-www!9@s9" not in ids  # has_magi True suppresses


def test_own_mr_magi_item_when_no_history():
    mr = _mr(author="chandler.hardy")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert any(i.id == "magi:pb-www!9@s9" for i in items)


# Task A.2 — draft hygiene exemption: a draft MR shouldn't nag about a
# missing dev-URL (the link often isn't ready until the MR leaves draft).
def test_own_draft_mr_missing_devurl_no_hygiene_item():
    mr = _mr(author="chandler.hardy", is_draft=True, description="no link")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    assert not any(i.executor == "park" for i in items)


def test_own_non_draft_mr_missing_devurl_still_gets_hygiene_item():
    mr = _mr(author="chandler.hardy", is_draft=False, description="no link")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    assert any(i.executor == "park" for i in items)


# Task A.3 — issue-covered-by-MR suppression
def _authored_mr(**kw):
    base = dict(repo="pb-www", iid=100, title="feat(#1701): x", author="me",
               web_url="https://gl/x/-/merge_requests/100", description="",
               sha="s100", is_draft=False, reviewers=(),
               ci_status="success", updated_at="")
    base.update(kw)
    return MergeRequest(**base)


def test_covered_issue_iids_extracts_from_title():
    mrs = [_authored_mr(title="feat(#1701): Add Usage column")]
    assert covered_issue_iids(mrs) == {1701}


def test_covered_issue_iids_ignores_untagged_titles():
    mrs = [_authored_mr(title="feat(#1701): x"),
           _authored_mr(iid=101, title="chore: cleanup")]
    assert covered_issue_iids(mrs) == {1701}


def test_covered_issue_iids_empty_for_no_authored_mrs():
    assert covered_issue_iids([]) == set()


# Review follow-up: a bare "#\d+ anywhere in the title" scan over-suppresses
# -- it wrongly covers incidental refs (e.g. "follow-up to #796 review")
# alongside the real leading tag, and treats "(see #869)" as a coverage
# claim. Narrowed to the leading conventional tag + explicit closing
# keywords only.
def test_covered_issue_iids_ignores_incidental_mid_title_ref():
    mrs = [_authored_mr(title="feat(#1701): follow-up to #796 review")]
    assert covered_issue_iids(mrs) == {1701}


def test_covered_issue_iids_leading_tag_with_draft_prefix():
    mrs = [_authored_mr(title="Draft: feat(#1598): bulk edit")]
    assert covered_issue_iids(mrs) == {1598}


def test_covered_issue_iids_parenthetical_mention_not_covered():
    mrs = [_authored_mr(title="Fix the thing (see #869)")]
    assert covered_issue_iids(mrs) == set()


def test_covered_issue_iids_closing_keyword_without_leading_tag():
    mrs = [_authored_mr(title="chore: cleanup — Closes #42")]
    assert covered_issue_iids(mrs) == {42}


def test_assess_issue_suppressed_when_covered():
    issue = Issue(repo="pb-www", iid=1701, title="t", web_url="u")
    assert assess_issue(issue, covered={1701}) == []


def test_assess_issue_survives_when_not_covered():
    issue = Issue(repo="pb-www", iid=42, title="t", web_url="u")
    items = assess_issue(issue, covered={1701})
    assert len(items) == 1


def test_assess_issue_default_covered_is_empty():
    # backward-compat: callers that don't pass `covered` still get the item.
    issue = Issue(repo="pb-www", iid=42, title="t", web_url="u")
    assert len(assess_issue(issue)) == 1


# Task A.4 — todo hard filter
def _wi(**kw):
    base = dict(schema_version=1, id="x", repo="pb-www", kind="mr",
               executor="magi-review", risk="low", why="w",
               web_url="https://gl/x/-/merge_requests/9", sha="s")
    base.update(kw)
    return WorkItem(**base)


def test_filter_todos_drops_note_anchor_matching_tracked_item():
    todos = [Todo(target="MergeRequest", action="mentioned",
                  web_url="https://gl/x/-/merge_requests/9#note_123")]
    assert filter_todos(todos, [_wi()], []) == []


def test_filter_todos_drops_review_requested_unconditionally():
    todos = [Todo(target="MergeRequest", action="review_requested",
                  web_url="https://gl/x/-/merge_requests/999")]
    assert filter_todos(todos, [], []) == []


def test_filter_todos_drops_assigned_unconditionally():
    todos = [Todo(target="MergeRequest", action="assigned",
                  web_url="https://gl/x/-/merge_requests/999")]
    assert filter_todos(todos, [], []) == []


def test_filter_todos_keeps_novel_mention():
    todos = [Todo(target="Issue", action="directly_addressed",
                  web_url="https://gl/x/-/issues/5")]
    assert filter_todos(todos, [], []) == todos


def test_filter_todos_drops_when_matches_tracked_mr_bucket():
    mr = _mr(iid=30, web_url="https://gl/x/-/merge_requests/30/")
    todos = [Todo(target="MergeRequest", action="mentioned",
                  web_url="https://gl/x/-/merge_requests/30")]
    assert filter_todos(todos, [], [mr]) == []


# Task A.1 — assignee bucket
def test_assigned_mr_emits_item_when_not_authored_and_not_tracked():
    mr = _mr(iid=20, author="other", web_url="https://gl/x/-/merge_requests/20")
    items = assess_assigned_mr(mr, "chandler.hardy", tracked=set())
    assert len(items) == 1
    it = items[0]
    assert it.id == "assigned:pb-www!20"
    assert it.kind == "assigned_mr"
    assert it.executor == "triage"
    assert it.why == "assigned to you"


def test_assigned_mr_self_authored_no_duplicate():
    mr = _mr(iid=21, author="chandler.hardy")
    assert assess_assigned_mr(mr, "chandler.hardy", tracked=set()) == []


def test_assigned_mr_already_in_another_bucket_no_duplicate():
    mr = _mr(iid=22, author="other")
    tracked = {("pb-www", 22)}
    assert assess_assigned_mr(mr, "chandler.hardy", tracked=tracked) == []


# --- address-feedback emission (2026-08-25) --------------------------------
#
# Two arms sharing one id (`feedback:{repo}!{iid}`, so reconcile keeps the
# queue number as an MR moves between them):
#   unaddressed_count > 0            -> runnable `address-feedback` work
#   changes_requested, none unaddressed -> plain `triage` information
#   neither                          -> nothing at all

def _own(**kw):
    base = dict(author="chandler.hardy", description="dev link: "
                "https://dev2.chandlerhardy-dev.performancebeef.com/",
                source_branch="chardy/1588-ranch-data")
    base.update(kw)
    return _mr(**base)


def _feedback(items):
    return [i for i in items if i.id.startswith("feedback:")]


def _assess(mr):
    return assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)


def test_addressed_threads_emit_nothing():
    """FALSIFYING (AC #3). Two threads are unresolved but Chandler's reply is
    the last word in both -- the ball is in the reviewer's court and worksweep
    has nothing to propose.

    Mutation: restore the old `mr.changes_requested or mr.unresolved_count > 0`
    gate and this list stops being empty.
    """
    mr = _own(unresolved_count=2, unaddressed_count=0, changes_requested=False)
    assert _feedback(_assess(mr)) == []


def test_address_feedback_item_shape():
    mr = _own(unresolved_count=3, unaddressed_count=3)
    items = _feedback(_assess(mr))
    assert len(items) == 1
    it = items[0]
    assert it.id == "feedback:pb-www!9"          # id preserved across the rename
    assert it.kind == "feedback"
    assert it.executor == "address-feedback"
    assert it.branch == "chardy/1588-ranch-data"
    assert it.why == "3 unaddressed threads"
    assert it.status == "proposed"               # ✅-gated, never auto-approved
    assert it.web_url == mr.web_url and it.sha == mr.sha and it.title == mr.title


def test_address_feedback_why_is_singular_for_one_thread():
    it = _feedback(_assess(_own(unresolved_count=1, unaddressed_count=1)))[0]
    assert it.why == "1 unaddressed thread"


def test_address_feedback_why_is_prefixed_when_changes_are_requested():
    it = _feedback(_assess(_own(unresolved_count=2, unaddressed_count=2,
                                changes_requested=True)))[0]
    assert it.why == "changes requested, 2 unaddressed threads"


def test_changes_requested_without_unaddressed_threads_stays_informational():
    """AC #16 (Round 3): REQUESTED_CHANGES with every thread already answered
    is information, not runnable work -- it keeps its row so the MR stays
    visible, but as a non-runnable `triage` line with no branch."""
    items = _feedback(_assess(_own(changes_requested=True, unresolved_count=2,
                                   unaddressed_count=0)))
    assert len(items) == 1
    it = items[0]
    assert it.id == "feedback:pb-www!9"          # same id as the runnable arm
    assert it.kind == "feedback"
    assert it.executor == "triage"
    assert it.why == "changes requested"
    assert it.branch == ""


def test_no_signal_emits_no_feedback_row():
    assert _feedback(_assess(_own())) == []


def test_the_two_arms_are_mutually_exclusive():
    """One MR never produces two feedback rows -- they share an id, so a
    double emission would be a duplicate key in the queue."""
    for kw in (dict(unaddressed_count=2, changes_requested=True),
               dict(unaddressed_count=2, changes_requested=False),
               dict(unaddressed_count=0, changes_requested=True),
               dict(unaddressed_count=0, changes_requested=False)):
        assert len(_feedback(_assess(_own(unresolved_count=2, **kw)))) <= 1


def test_address_feedback_is_not_auto_approved():
    """AC #6. This executor posts replies under Chandler's GitLab identity and
    a reply cannot be unsent, so consent is per-MR: the name must never reach
    the auto-approve default, and a proposed row must survive auto_approve()."""
    from worksweep.config import WorksweepConfig
    from worksweep.queue import auto_approve
    from worksweep.models import QueueRecord
    default = WorksweepConfig.__dataclass_fields__["auto_approve"].default
    assert default == ("keep-current",)
    assert "address-feedback" not in default

    item = _feedback(_assess(_own(unresolved_count=1, unaddressed_count=1)))[0]
    rec = QueueRecord(number=1, item=item, first_seen="", last_seen="")
    assert auto_approve([rec], default)[0].item.status == "proposed"


def test_dashboard_renders_an_approve_checkbox_for_address_feedback():
    """AC #5: the approve control comes free from RUNNABLE_EXECUTORS -- this
    passes with ZERO edits to dashboard.has_checkbox."""
    from worksweep import dashboard
    from worksweep.queue import is_dismissable
    item = _feedback(_assess(_own(unresolved_count=1, unaddressed_count=1)))[0]
    assert dashboard.has_checkbox(item) is True
    # and the flip side the rename buys: runnable work is no longer a row you
    # can wave away, it is a row you approve or leave alone.
    assert is_dismissable(item) is False


def test_the_informational_arm_keeps_its_manual_affordance():
    from worksweep import dashboard
    from worksweep.queue import is_dismissable
    item = _feedback(_assess(_own(changes_requested=True)))[0]
    assert dashboard.has_checkbox(item) is False
    assert is_dismissable(item) is True
