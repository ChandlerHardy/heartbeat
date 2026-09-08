"""standup: Monday standup notes from a week of GitLab activity.

Migrated from the OCI cron on 2026-09-08. That script dumped the raw authored-MR
list into `claude -p` with no classification, so a week of keep-current
freshening merges on parked drafts read as "three rounds of updates" (2026-08-17),
and its only failure signal was a line in /tmp (its glab token expired and the
Monday notes silently never arrived, 2026-09-07). The rules here are the
standup-notes skill's: classify every push BEFORE anything narrates, treat a
branch with only freshening pushes as parked, and never be silent.
"""
import json

from worksweep.standup import (
    Facts, build_facts_text, build_prompt, classify_push, collect, render_fallback,
    run_standup, validate, _chunks,
)

NOW = "2026-09-07T01:05:00+00:00"
AFTER = "2026-08-31"


def _event(day, ref, title, count=1, action="pushed to"):
    return {"created_at": f"2026-09-{day:02d}T12:00:00Z", "action_name": action,
            "push_data": {"ref": ref, "commit_count": count, "commit_title": title},
            "project_id": 1172322}


def _review(day, action, title):
    return {"created_at": f"2026-09-{day:02d}T12:00:00Z", "action_name": action,
            "target_title": title, "project_id": 1172322}


def _mr(iid, title, state="opened", branch="", draft=False, merged=None,
        reviewers=(), updated="2026-09-05T00:00:00Z"):
    return {"iid": iid, "title": title, "state": state, "draft": draft,
            "source_branch": branch or f"b/{iid}", "merged_at": merged,
            "updated_at": updated, "web_url": f"https://gl/pb-www/-/merge_requests/{iid}",
            "reviewers": [{"username": r} for r in reviewers]}


# --- classification --------------------------------------------------------

def test_classify_push_freshening_stack_rebase_and_work():
    assert classify_push("Merge remote-tracking branch 'origin/master' into fix/1") == "freshening"
    assert classify_push("Merge remote-tracking branch 'origin/fix/1650-x' into fix/1814-y") == "stack-rebase"
    assert classify_push("Merge branch 'master' into feat/2") == "freshening"
    assert classify_push("fix(#1813): hide the loader on a refused save") == "work"
    assert classify_push("test(#1650): randywu round 3") == "work"
    assert classify_push(None) == "work"   # a missing title never hides a push


# --- collect ---------------------------------------------------------------

def _glab_fake(pages, mrs_by_repo):
    calls = []
    def run_glab(args):
        calls.append(args)
        path = args[1]
        if path.startswith("events?"):
            page = 1
            for part in path.split("&"):
                if part.startswith("page="):
                    page = int(part[5:])
            return json.dumps(pages.get(page, []))
        for repo, mrs in mrs_by_repo.items():
            if f"%2F{repo}/merge_requests?" in path:
                return json.dumps(mrs)
        return "[]"
    return run_glab, calls


def test_collect_paginates_events_until_a_short_page_and_classifies_pushes():
    pages = {1: [_event(5, "fix/1813", "Merge remote-tracking branch 'origin/master' into fix/1813", 7)] * 100,
             2: [_event(4, "fix/1813", "fix(#1813): hide the loader on a refused save"),
                 _review(4, "commented on", "Apply timezone from browser during signup")]}
    run_glab, calls = _glab_fake(pages, {"pb-www": [_mr(4110, "fix(#1813): mint ids", branch="fix/1813",
                                                         reviewers=("randywu",))]})
    facts = collect(run_glab, username="me", repos=("pb-www",), after=AFTER)
    assert [c[1] for c in calls if c[1].startswith("events?")] == [
        f"events?after={AFTER}&per_page=100&page=1",
        f"events?after={AFTER}&per_page=100&page=2"]
    assert facts.work_pushes == [("2026-09-04", "fix/1813", "fix(#1813): hide the loader on a refused save")]
    assert facts.freshening_branches == {"fix/1813": 100}
    assert facts.reviews == [("2026-09-04", "commented on", "Apply timezone from browser during signup")]
    assert facts.mrs[0]["iid"] == 4110 and facts.mrs[0]["repo"] == "pb-www"


def test_collect_asks_every_configured_repo_for_authored_mrs():
    run_glab, calls = _glab_fake({1: []}, {"pb-www": [], "pb-api": []})
    collect(run_glab, username="me", repos=("pb-www", "pb-api"), after=AFTER)
    mr_calls = [c[1] for c in calls if "merge_requests?" in c[1]]
    assert any("%2Fpb-www/" in c and "author_username=me" in c and f"updated_after={AFTER}" in c for c in mr_calls)
    assert any("%2Fpb-api/" in c for c in mr_calls)


def test_collect_raises_a_clean_error_when_glab_fails():
    def run_glab(args):
        raise RuntimeError("glab api events failed: HTTP 401 invalid_token")
    import pytest
    with pytest.raises(RuntimeError, match="401"):
        collect(run_glab, username="me", repos=("pb-www",), after=AFTER)


# --- facts text / prompt ---------------------------------------------------

def _facts():
    return Facts(
        after=AFTER,
        mrs=[dict(_mr(4103, "fix(#1828): safe_div weak-casts", state="merged", branch="fix/1828",
                      merged="2026-09-03T15:00:00Z", reviewers=("lnxprof",)), repo="pb-www"),
             dict(_mr(4085, "fix(#1650): CAS uniqueness", branch="fix/1650", reviewers=("randywu", "leyang")), repo="pb-www"),
             dict(_mr(3985, "Draft: feat(#1599): yard sheets ranch data", branch="feat/1599", draft=True,
                      reviewers=("adamsoper",)), repo="pb-www")],
        work_pushes=[("2026-09-03", "fix/1828", "fix(#1828): cast operands"),
                     ("2026-09-04", "fix/1650", "test(#1650): randywu round 3")],
        freshening_branches={"fix/1650": 3, "feat/1599": 4, "fix/1828": 1},
        reviews=[("2026-09-04", "commented on", "Apply timezone from browser during signup")],
    )


def test_facts_text_puts_work_under_its_mr_and_marks_freshening_only_branches_parked():
    text = build_facts_text(_facts())
    assert "!4103" in text and "merged 2026-09-03" in text
    assert "fix(#1828): cast operands" in text
    assert "!3985" in text and "PARKED" in text          # only freshening pushes
    assert "3 branches kept current with master" in text
    assert "commented on | Apply timezone" in text


def test_prompt_carries_the_format_rules_and_the_facts():
    p = build_prompt(_facts())
    assert p.startswith("Chandler:") is False and "Chandler:" in p
    for word in ("merged", "in review", "parked awaiting", "freshening"):
        assert word in p
    assert "!4085" in p
    assert "NEVER" in p or "never" in p


# --- validate --------------------------------------------------------------

GOOD = ("Chandler:\nsafe_div production fatals (#1828) - merged\n!4103 merged Sep 3.\n"
        "Demo yard-sheet CAS (#1650) - in review\n!4085 round 3 addressed.\n"
        "Yard sheets ranch data (#1599) - parked awaiting pb-api endpoints\n"
        "Reviews: timezone signup MR.\n")


def test_validate_accepts_notes_that_cite_only_known_refs_and_every_merged_mr():
    assert validate(GOOD, _facts())


def test_validate_accepts_an_mr_number_that_appears_inside_a_commit_title():
    f = _facts()
    f.work_pushes.append(("2026-09-03", "fix/1650", "test(ci): cut the guard to the merged !4084 shape"))
    assert validate(GOOD + "Mongo lane guard cut to the !4084 shape.\n", f)


def test_reviews_exclude_replies_on_ones_own_mrs():
    f = _facts()
    f.reviews.append(("2026-09-04", "commented on", "fix(#1650): CAS uniqueness"))   # own MR
    text = build_facts_text(f)
    assert "commented on | Apply timezone" in text
    assert "commented on | fix(#1650)" not in text
    assert "CAS uniqueness" not in render_fallback(f).split("Reviews:")[1].split("\n")[0]


def test_validate_rejects_invented_mr_or_issue_numbers():
    assert not validate(GOOD.replace("!4085", "!4999"), _facts())
    assert not validate(GOOD + "Something (#7777) - merged\n", _facts())


def test_validate_rejects_missing_header_urls_and_a_missing_merged_mr():
    assert not validate(GOOD.replace("Chandler:\n", ""), _facts())
    assert not validate(GOOD + "see https://gitlab.com/x\n", _facts())
    assert not validate(GOOD.replace("!4103 merged Sep 3.\n", ""), _facts())
    assert not validate("", _facts())


# --- fallback + posting ----------------------------------------------------

def test_fallback_is_deterministic_and_passes_validation():
    out = render_fallback(_facts())
    assert out.startswith("Chandler:")
    assert "!4103" in out and "merged" in out
    assert "parked awaiting" in out
    assert validate(out, _facts())


def test_chunks_split_on_lines_under_the_discord_limit():
    text = "\n".join(f"line {i} " + "x" * 120 for i in range(40))
    parts = _chunks(text, 1900)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 1900 for p in parts)
    assert "\n".join(parts) == text


def test_run_standup_posts_curated_notes():
    posts = []
    deps = {"collect": lambda: _facts(), "llm": lambda prompt: GOOD,
            "post": lambda hook, content: posts.append(content), "now": lambda: NOW}
    assert run_standup(_cfg(), deps) == 0
    assert posts[0].startswith("**Monday Standup Notes**")
    assert "!4103" in posts[0]


def test_run_standup_falls_back_when_the_llm_fails_or_hallucinates():
    posts = []
    deps = {"collect": lambda: _facts(), "llm": lambda prompt: GOOD.replace("!4085", "!4999"),
            "post": lambda hook, content: posts.append(content), "now": lambda: NOW}
    assert run_standup(_cfg(), deps) == 0
    assert "!4103" in posts[0] and "!4999" not in posts[0]
    assert "fallback" in posts[0].lower()

    posts.clear()
    def boom(prompt):
        raise RuntimeError("claude timed out")
    deps["llm"] = boom
    assert run_standup(_cfg(), deps) == 0
    assert "!4103" in posts[0]


def test_run_standup_alerts_loudly_when_collection_fails():
    posts = []
    def boom():
        raise RuntimeError("glab api events failed: HTTP 401 invalid_token")
    deps = {"collect": boom, "llm": lambda p: GOOD,
            "post": lambda hook, content: posts.append(content), "now": lambda: NOW}
    assert run_standup(_cfg(), deps) == 1
    assert len(posts) == 1 and posts[0].startswith("🔴") and "401" in posts[0]


def test_run_standup_names_a_logged_out_claude():
    from worksweep.curator import AuthError
    posts = []
    def logged_out(prompt):
        raise AuthError("curator LLM exited 1: Not logged in · Please run /login")
    deps = {"collect": lambda: _facts(), "llm": logged_out,
            "post": lambda hook, content: posts.append(content), "now": lambda: NOW}
    assert run_standup(_cfg(), deps) == 0
    assert any(p.startswith("🔴") and "/login" in p for p in posts)
    assert any("!4103" in p for p in posts)   # the fallback notes still went out


def _cfg():
    from worksweep.config import WorksweepConfig
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y")
