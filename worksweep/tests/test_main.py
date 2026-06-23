import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
from worksweep.__main__ import build_digest  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="chandler.hardy", web_url="u",
                description="no link", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_build_digest_end_to_end_with_injected_collectors():
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [_mr()],
        "review_requests": lambda repo, user: [],
        "todos": lambda: [],
        "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, s: True)
    # mine + missing dev link -> exactly one hygiene item
    assert "mr-hygiene" in out
    assert "1." in out


def test_build_digest_empty_is_all_clear():
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [], "review_requests": lambda repo, user: [],
        "todos": lambda: [], "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, s: True)
    assert "nothing needs you" in out.lower()
