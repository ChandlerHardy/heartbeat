"""M4 Task F: dev-slot sensing.

`probe` runs one ssh per configured box (edge injected) and parses branch +
sha. `classify` maps each box's branch onto the tier GitLab-side state
implies (free / handed_off / live) using only the OPEN MRs the GraphQL sweep
already fetched. `pick` and `summary_line` are pure helpers over the
resulting tier dict.
"""
from worksweep.devslots import DevBox, classify, pick, probe, summary_line
from worksweep.models import MergeRequest


def _box(name="dev1", branch="", sha=""):
    return DevBox(name=name, host=f"{name}-host", path=f"/home/x/{name}/pb-www",
                  url=f"https://{name}.example.com/", branch=branch, sha=sha)


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="other",
               web_url="https://gl/x/-/merge_requests/1", description="",
               sha="s1", is_draft=False, reviewers=(), ci_status="success",
               updated_at="", source_branch="")
    base.update(kw)
    return MergeRequest(**base)


# --- probe -------------------------------------------------------------

def test_probe_parses_branch_and_sha_from_ssh_output():
    boxes_cfg = [{"name": "dev1", "host": "h1", "path": "/p1", "url": "u1"}]

    def run_ssh(host, cmd):
        assert host == "h1"
        assert "/p1" in cmd and "git branch --show-current" in cmd
        return "feat/1775-thing\nabc123def\n"

    out = probe(boxes_cfg, run_ssh)
    assert out == [DevBox(name="dev1", host="h1", path="/p1", url="u1",
                          branch="feat/1775-thing", sha="abc123def")]


def test_probe_unreachable_box_degrades_to_unknown_branch():
    boxes_cfg = [{"name": "dev2", "host": "h2", "path": "/p2", "url": "u2"}]

    def run_ssh(host, cmd):
        raise RuntimeError("ssh h2 timed out after 20s")

    out = probe(boxes_cfg, run_ssh)
    assert out == [DevBox(name="dev2", host="h2", path="/p2", url="u2",
                          branch="", sha="")]


def test_probe_nonzero_exit_raising_edge_degrades_to_unknown():
    boxes_cfg = [{"name": "dev3", "host": "h3", "path": "/p3", "url": "u3"}]

    def run_ssh(host, cmd):
        raise RuntimeError("ssh h3 failed: Permission denied")

    out = probe(boxes_cfg, run_ssh)
    assert out[0].branch == "" and out[0].sha == ""


def test_probe_multiple_boxes_preserves_order():
    boxes_cfg = [
        {"name": "dev1", "host": "h1", "path": "/p1", "url": "u1"},
        {"name": "dev2", "host": "h2", "path": "/p2", "url": "u2"},
    ]

    def run_ssh(host, cmd):
        return f"branch-{host}\nsha-{host}\n"

    out = probe(boxes_cfg, run_ssh)
    assert [b.name for b in out] == ["dev1", "dev2"]
    assert out[0].branch == "branch-h1"
    assert out[1].branch == "branch-h2"


def test_probe_empty_boxes_returns_empty_list():
    assert probe([], lambda h, c: "") == []


def test_probe_malformed_single_line_output_yields_empty_sha():
    boxes_cfg = [{"name": "dev1", "host": "h1", "path": "/p1", "url": "u1"}]
    out = probe(boxes_cfg, lambda h, c: "onlybranch\n")
    assert out[0].branch == "onlybranch" and out[0].sha == ""


# --- classify ------------------------------------------------------------

def test_classify_branch_with_no_open_mr_is_free():
    boxes = [_box("dev1", branch="feat/999-orphan")]
    assert classify(boxes, [], "chandler.hardy") == {"dev1": "free"}


def test_classify_branch_matching_handed_off_mr_is_handed_off():
    boxes = [_box("dev4", branch="feat/1701-thing")]
    mr = _mr(source_branch="feat/1701-thing", author="chandler.hardy",
            approved=True, merge_status="MERGEABLE",
            assignees=("someone-else",))
    assert classify(boxes, [mr], "chandler.hardy") == {"dev4": "handed_off"}


def test_classify_branch_matching_live_mr_is_live():
    boxes = [_box("dev0", branch="feat/1701-thing")]
    mr = _mr(source_branch="feat/1701-thing", author="chandler.hardy",
            approved=False, merge_status="", assignees=())
    assert classify(boxes, [mr], "chandler.hardy") == {"dev0": "live"}


def test_classify_unknown_branch_is_live_failsafe():
    boxes = [_box("dev5", branch="")]
    assert classify(boxes, [], "chandler.hardy") == {"dev5": "live"}


def test_classify_claimed_box_is_live_even_if_branch_would_be_free():
    boxes = [_box("dev1", branch="feat/999-orphan")]
    out = classify(boxes, [], "chandler.hardy", claimed=frozenset({"dev1"}))
    assert out == {"dev1": "live"}


def test_classify_multiple_boxes_mixed_tiers():
    boxes = [
        _box("dev1", branch="feat/999-orphan"),           # no MR -> free
        _box("dev4", branch="feat/1701-thing"),            # handed off
        _box("dev0", branch="feat/1702-other"),            # live
        _box("dev5", branch=""),                           # unknown -> live
    ]
    handed = _mr(source_branch="feat/1701-thing", author="chandler.hardy",
                approved=True, merge_status="MERGEABLE",
                assignees=("someone-else",))
    live = _mr(source_branch="feat/1702-other", author="chandler.hardy",
              approved=False, merge_status="", assignees=())
    tiers = classify(boxes, [handed, live], "chandler.hardy")
    assert tiers == {"dev1": "free", "dev4": "handed_off",
                     "dev0": "live", "dev5": "live"}


def test_classify_closed_mr_not_in_sweep_leaves_branch_free():
    # Only OPEN MRs come from the GraphQL sweep -- a branch whose MR merged/
    # closed simply has no entry in all_mrs, so it reads as free.
    boxes = [_box("dev2", branch="feat/900-merged-already")]
    other = _mr(source_branch="some-other-branch")
    assert classify(boxes, [other], "chandler.hardy") == {"dev2": "free"}


# --- pick ------------------------------------------------------------

def test_pick_prefers_free_over_handed_off():
    tiers = {"dev4": "handed_off", "dev1": "free", "dev0": "live"}
    assert pick(tiers, ["dev0", "dev1", "dev4"]) == "dev1"


def test_pick_falls_back_to_handed_off_when_no_free():
    tiers = {"dev4": "handed_off", "dev0": "live"}
    assert pick(tiers, ["dev0", "dev4"]) == "dev4"


def test_pick_none_when_all_live():
    tiers = {"dev0": "live", "dev2": "live"}
    assert pick(tiers, ["dev0", "dev2"]) is None


def test_pick_deterministic_order_first_free_wins():
    tiers = {"dev1": "free", "dev2": "free"}
    assert pick(tiers, ["dev2", "dev1"]) == "dev2"
    assert pick(tiers, ["dev1", "dev2"]) == "dev1"


def test_pick_empty_tiers_returns_none():
    assert pick({}, []) is None


# --- summary_line ------------------------------------------------------

def test_summary_line_matches_example_shape():
    tiers = {"dev1": "free", "dev4": "handed_off", "dev5": "handed_off",
            "dev0": "live", "dev2": "live", "dev3": "live"}
    line = summary_line(tiers)
    assert line == ("Dev slots: dev1 free · dev4, dev5 reclaimable "
                    "(approved, awaiting merge) · dev0, dev2, dev3 live")


def test_summary_line_all_free():
    assert summary_line({"dev1": "free"}) == "Dev slots: dev1 free"


def test_summary_line_all_live():
    assert summary_line({"dev0": "live", "dev1": "live"}) == "Dev slots: dev0, dev1 live"


def test_summary_line_empty_tiers():
    assert summary_line({}) == "Dev slots: none configured"
