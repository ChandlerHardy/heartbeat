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
    assert not any(i.executor == "mr-hygiene" for i in items)


def test_own_non_draft_mr_missing_devurl_still_gets_hygiene_item():
    mr = _mr(author="chandler.hardy", is_draft=False, description="no link")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    assert any(i.executor == "mr-hygiene" for i in items)


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
