"""M3.5 Task C wiring: run_sweep tries curate() when actionable items exist
and cfg.curate is on, posts ONE curated message on success, and falls back
to the existing raw multi-part digest whenever curation can't be trusted
(LLM failure, validation reject, curate=False, or no "llm" dep at all --
the --dry-run shape)."""
import json

from worksweep.__main__ import run_sweep
from worksweep.config import WorksweepConfig


def _cfg(curate=True):
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y",
                           curate=curate)


def _gql(review_nodes=()):
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": list(review_nodes)},
        "authoredMergeRequests": {"nodes": []},
        "assignedMergeRequests": {"nodes": []}}}})


def _node(iid=4061, state="UNREVIEWED"):
    return {"iid": str(iid), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-07T00:00:00Z",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "other"},
            "reviewers": {"nodes": [
                {"username": "me", "mergeRequestInteraction": {"reviewState": state}}]},
            "headPipeline": None}


def _deps(store, raw, llm=None, include_llm_key=True):
    d = {
        "graphql": lambda: raw,
        "todos": lambda: [],
        "issues": lambda repo, user: [],
        "post": lambda hook, content: store.append(content),
        "load": lambda: [],
        "save": lambda records: store.append(("saved", records)),
        "now": lambda: "2026-08-07T12:00:00+00:00",
    }
    if include_llm_key:
        d["llm"] = llm
    return d


def test_curated_digest_posts_single_message_with_curated_header():
    posts = []
    good = "1. pb-www !4061 -- review requested"
    deps = _deps(posts, _gql(review_nodes=[_node()]), llm=lambda prompt: good)
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" in texts[0]
    assert "1. pb-www [!4061](<https://gl/x/-/merge_requests/4061>) -- review requested" in texts[0]  # linkified post-validation
    assert "1 actionable" in texts[0]


def test_curator_llm_exception_falls_back_to_raw_digest():
    posts = []
    def boom(prompt):
        raise RuntimeError("claude timed out")
    deps = _deps(posts, _gql(review_nodes=[_node()]), llm=boom)
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" not in texts[0]
    assert "review" in texts[0].lower()


def test_curator_validation_failure_falls_back_to_raw_digest():
    posts = []
    bad = "1. pb-www !4061 -- review requested, also see item 999"
    deps = _deps(posts, _gql(review_nodes=[_node()]), llm=lambda prompt: bad)
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" not in texts[0]


def test_curate_false_skips_llm_entirely():
    posts = []
    calls = []
    def would_blow_up(prompt):
        calls.append(prompt)
        raise AssertionError("run_llm must not be called when cfg.curate is False")
    deps = _deps(posts, _gql(review_nodes=[_node()]), llm=would_blow_up)
    rc = run_sweep(_cfg(curate=False), deps)
    assert rc == 0
    assert calls == []
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" not in texts[0]


def test_missing_llm_dep_skips_curation_raw_path_used():
    """--dry-run wiring never puts an "llm" key in deps at all -- curation
    must be skipped (not crash on a KeyError) and the raw digest posted."""
    posts = []
    deps = _deps(posts, _gql(review_nodes=[_node()]), include_llm_key=False)
    assert "llm" not in deps
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" not in texts[0]


def test_assembled_curated_message_never_exceeds_discord_cap():
    """Important fix: curated is validated at <=1300 bytes, but header +
    "(curated) -- N actionable / M held:" + footer add ~140 more bytes on
    top -- the assembled message must be truncated to the Discord-safe cap
    (formatter.DISCORD_MAX_CHARS = 1900) before posting, not just trust the
    curated budget to leave enough headroom."""
    from worksweep.formatter import DISCORD_MAX_CHARS

    posts = []
    # Build a near-1300-byte curated string that still passes validate():
    # no links/URLs, and the only digit token is the real magi-review ref.
    padding = "review requested. " * 68  # ~1290 bytes of digit-free filler
    good = f"1. pb-www !4061 -- {padding}".rstrip()
    assert len(good.encode("utf-8")) <= 1300  # sanity: curated budget itself is respected
    deps = _deps(posts, _gql(review_nodes=[_node()]), llm=lambda prompt: good)
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert "(curated)" in texts[0]
    assert len(texts[0].encode("utf-8")) <= DISCORD_MAX_CHARS


def test_curated_path_untouched_for_heartbeat_message():
    """When nothing is actionable, the heartbeat message is unaffected by
    curation (curator must not run at all -- no items to curate)."""
    posts = []
    calls = []
    def would_blow_up(prompt):
        calls.append(prompt)
        return "should not be reached"
    deps = _deps(posts, _gql(review_nodes=[_node(state="REVIEWED")]), llm=would_blow_up)
    rc = run_sweep(_cfg(curate=True), deps)
    assert rc == 0
    assert calls == []
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1 and texts[0].startswith("🔍")
