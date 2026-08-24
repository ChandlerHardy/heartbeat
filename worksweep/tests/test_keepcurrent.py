"""M4 Task H: the `keep-current` executor's subprocess/ssh/http contract.

Every edge is injected — this file must never touch the network, a real
checkout, ssh, or a real git binary."""
import subprocess

import pytest

from worksweep.config import WorksweepConfig
from worksweep.keepcurrent import KeepCurrentResult, execute, iid_of
from worksweep.models import WorkItem
from worksweep.runner import RunnerError

NOW = "2026-08-18T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, stale_threshold=5)
    base.update(kw)
    return WorksweepConfig(**base)


def _item(iid=4020, branch="feat/1701-thing", title="Feed schedule tweak"):
    return WorkItem(schema_version=1, id=f"stale:pb-www!{iid}", repo="pb-www",
                    kind="stale", executor="keep-current", risk="low",
                    why="7 commits behind master",
                    web_url=f"https://gl/x/-/merge_requests/{iid}", sha="s",
                    status="approved", title=title, branch=branch)


_BOXES = [{"name": "dev4", "host": "chandlerhardy-dev",
          "path": "/p/pb-www",
          "url": "https://dev4.chandlerhardy-dev.performancebeef.com/"}]

# review fix C1: keep-current runs in its own worktree
# (<checkouts_root>/.worktrees/<repo>-keep-current), not the shared clone.
_WORKTREE_SUFFIX = ".worktrees/pb-www-keep-current"


def _worktree(tmp_path):
    return tmp_path / ".worktrees" / "pb-www-keep-current"


def test_iid_of_reads_mr_iid_from_web_url():
    assert iid_of(_item(iid=4020)) == 4020


def test_iid_of_raises_on_unparseable_web_url():
    import dataclasses
    bad = dataclasses.replace(_item(), web_url="https://gl/x/-/issues/1")
    with pytest.raises(RunnerError, match="iid"):
        iid_of(bad)


class _Edges:
    """Scriptable subprocess/ssh/http triple for execute()."""

    def __init__(self, **kw):
        self.calls = []
        self.ssh_calls = []
        self.http_calls = []
        self.checkout = kw.get("checkout", "")
        self.pre_sha = kw.get("pre_sha", "pre123")
        self.head_sha = kw.get("head_sha", "post456")
        self.rev_list_out = kw.get("rev_list_out", "3\n")
        self.merge_rc = kw.get("merge_rc", 0)
        self.merge_raises_timeout = kw.get("merge_raises_timeout", False)
        self.conflict_files = kw.get("conflict_files", "a.php\nb.php\n")
        self.scss_diff_out = kw.get("scss_diff_out", "")
        self.compile_rc = kw.get("compile_rc", 0)
        self.push_rc = kw.get("push_rc", 0)
        # review fix I4: does a MERGE_HEAD exist when the preflight checks?
        self.merge_head_present = kw.get("merge_head_present", False)
        # review fix I4: `git status --porcelain` output the preflight sees.
        self.status_out = kw.get("status_out", "")
        # review fix C2: `git diff --cached --quiet` exit code -- 0 = nothing
        # staged (compiled output identical to what master already had),
        # 1 = staged changes exist (the common "real change" case, so this
        # defaults to 1 to match the pre-fix behavior every existing test
        # already assumes: scss changed -> a commit happens).
        self.staged_diff_rc = kw.get("staged_diff_rc", 1)
        # what devslots.probe (first ssh call) and sync_to_box's drift
        # re-probe (second ssh call) see on the box:
        self.probe_branch = kw.get("probe_branch", "feat/1701-thing")
        self.probe_sha = kw.get("probe_sha", "boxsha")
        self.sync_landed_sha = kw.get("sync_landed_sha", None)
        # conflict-escalation (2026-08-24): what the claude resolver run does.
        self.claude_rc = kw.get("claude_rc", 0)
        self.claude_raises_timeout = kw.get("claude_raises_timeout", False)
        # post-resolver state the verifier sees:
        self.resolve_unresolved_out = kw.get("resolve_unresolved_out", "")
        self.resolve_no_commit = kw.get("resolve_no_commit", False)
        self.parent2_sha = kw.get("parent2_sha", "mastersha")
        self.master_sha = kw.get("master_sha", "mastersha")
        self.resolve_status_out = kw.get("resolve_status_out", "")
        self.claude_ran = False
        self._rev_parse_n = 0

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        c = list(cmd)
        if c and c[0] == "git":
            sub = c[3] if len(c) > 3 else ""
            if sub == "rev-parse":
                if "MERGE_HEAD" in c:
                    if self.claude_ran:
                        rc = 0 if self.resolve_no_commit else 1
                    else:
                        rc = 0 if self.merge_head_present else 1
                    return subprocess.CompletedProcess(c, rc, stdout="", stderr="")
                if "HEAD^2" in c:
                    return subprocess.CompletedProcess(
                        c, 0, stdout=self.parent2_sha + "\n", stderr="")
                if "origin/master" in c:
                    return subprocess.CompletedProcess(
                        c, 0, stdout=self.master_sha + "\n", stderr="")
                self._rev_parse_n += 1
                sha = self.pre_sha if self._rev_parse_n == 1 else self.head_sha
                return subprocess.CompletedProcess(c, 0, stdout=sha + "\n", stderr="")
            if sub == "status":
                out = self.resolve_status_out if self.claude_ran else self.status_out
                return subprocess.CompletedProcess(c, 0, stdout=out, stderr="")
            if sub == "rev-list":
                return subprocess.CompletedProcess(c, 0, stdout=self.rev_list_out,
                                                   stderr="")
            if sub == "merge":
                if "--abort" in c:
                    return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
                if self.merge_raises_timeout:
                    raise subprocess.TimeoutExpired(c, 120)
                err = "CONFLICT (content): Merge conflict\n" if self.merge_rc else ""
                return subprocess.CompletedProcess(c, self.merge_rc, stdout="",
                                                   stderr=err)
            if sub == "diff":
                if "--diff-filter=U" in c:
                    out = (self.resolve_unresolved_out if self.claude_ran
                           else self.conflict_files)
                    return subprocess.CompletedProcess(c, 0, stdout=out, stderr="")
                if "--cached" in c:
                    return subprocess.CompletedProcess(c, self.staged_diff_rc,
                                                       stdout="", stderr="")
                return subprocess.CompletedProcess(c, 0, stdout=self.scss_diff_out,
                                                   stderr="")
            if sub == "push":
                err = "rejected\n" if self.push_rc else ""
                return subprocess.CompletedProcess(c, self.push_rc, stdout="", stderr=err)
            return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
        if c and c[0] == "maintenance/compile-css":
            err = "sass error\n" if self.compile_rc else ""
            return subprocess.CompletedProcess(c, self.compile_rc, stdout="", stderr=err)
        if c and c[0] == "claude":
            if self.claude_raises_timeout:
                raise subprocess.TimeoutExpired(c, 900)
            self.claude_ran = True
            return subprocess.CompletedProcess(c, self.claude_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(c, 0, stdout="", stderr="")

    def ssh(self, host, command):
        self.ssh_calls.append((host, command))
        n = len(self.ssh_calls)
        if n <= 2:  # 1: devslots.probe  2: sync_to_box's drift re-probe
            return f"{self.probe_branch}\n{self.probe_sha}\n"
        landed = self.sync_landed_sha if self.sync_landed_sha is not None else self.head_sha
        return f"remote: {self.probe_branch} @ {landed}\n{landed}\n"

    def http(self, url):
        self.http_calls.append(url)
        return 200


def _run_execute(tmp_path, edges=None, boxes=None, cfg=None, item=None):
    root = tmp_path / "pb-www"
    root.mkdir(exist_ok=True)
    edges = edges or _Edges()
    edges.checkout = str(_worktree(tmp_path))
    return execute(item or _item(), cfg or _cfg(tmp_path),
                   boxes if boxes is not None else _BOXES,
                   run_subprocess=edges.run, run_ssh_probe=edges.ssh,
                   run_ssh=edges.ssh, http_get=edges.http), edges


# --------------------------------------------------------------------------
# runs in its own worktree (review fix C1)
# --------------------------------------------------------------------------

def test_execute_runs_in_the_keep_current_worktree_not_the_shared_clone(tmp_path):
    _, edges = _run_execute(tmp_path)
    flat = [c for c, kw in edges.calls]
    worktree, root = str(_worktree(tmp_path)), str(tmp_path / "pb-www")
    # every operational git command (fetch/checkout/merge/push/etc) runs
    # against the dedicated worktree -- the shared clone is touched ONLY by
    # the one-time `worktree add` bootstrap call that creates it.
    non_bootstrap = [c for c in flat if not (c[0] == "git" and "worktree" in c)]
    assert any(c[:3] == ["git", "-C", worktree] for c in non_bootstrap)
    assert not any(c[:3] == ["git", "-C", root] for c in non_bootstrap)
    assert ["git", "-C", root, "worktree", "add", "--detach", worktree,
           "origin/master"] in flat


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------

def test_execute_happy_path_no_scss_change_syncs_to_the_serving_box(tmp_path):
    result, edges = _run_execute(tmp_path)
    assert isinstance(result, KeepCurrentResult)
    assert result.iid == 4020
    assert result.ahead_count == 3
    assert result.scss_recompiled is False
    assert result.box_name == "dev4"
    assert result.dev_url.endswith("performancebeef.com/")
    assert result.result_sha == "post456"

    flat = [" ".join(c) for c, _ in edges.calls]
    assert any(f.endswith("fetch origin master feat/1701-thing") for f in flat)
    assert any("checkout -B feat/1701-thing origin/feat/1701-thing" in f for f in flat)
    assert any("merge origin/master --no-edit" in f for f in flat)
    assert any(f.endswith("push origin feat/1701-thing") for f in flat)
    assert not any("compile-css" in f for f in flat)
    assert not any(f.endswith(" commit -m chore: compile CSS after master merge")
                  for f in flat)
    # sync verified with a 200 on the box's own url
    assert edges.http_calls == [result.dev_url]


def test_execute_scss_change_recompiles_and_commits(tmp_path):
    edges = _Edges(scss_diff_out="www/home/scss/_totals.scss\n")
    result, edges = _run_execute(tmp_path, edges=edges)
    assert result.scss_recompiled is True
    flat = [(c, kw) for c, kw in edges.calls]
    compile_calls = [c for c, kw in flat if c[0] == "maintenance/compile-css"]
    assert compile_calls, "compile-css was never invoked"
    compile_kw = next(kw for c, kw in flat if c[0] == "maintenance/compile-css")
    assert compile_kw["cwd"] == edges.checkout
    assert compile_kw["timeout"] == 300
    add_calls = [c for c, kw in flat
                if c[:4] == ["git", "-C", edges.checkout, "add"]]
    assert add_calls and "www/home/css/" in add_calls[0] and "www/home/dealer/" in add_calls[0]
    commit_calls = [c for c, kw in flat
                   if c[:4] == ["git", "-C", edges.checkout, "commit"]]
    assert commit_calls
    assert "chore: compile CSS after master merge" in commit_calls[0]


def test_execute_no_scss_change_skips_compile(tmp_path):
    _, edges = _run_execute(tmp_path, edges=_Edges(scss_diff_out=""))
    flat = [c for c, kw in edges.calls]
    assert not any(c[0] == "maintenance/compile-css" for c in flat)
    assert not any(c[3:4] == ["commit"] for c in flat)


def test_execute_no_box_serving_branch_is_still_done(tmp_path):
    """No box has this branch checked out -> done, not an error. The merge +
    push already succeeded; there's just nobody to sync it to."""
    edges = _Edges(probe_branch="some-other-branch")
    result, edges = _run_execute(tmp_path, edges=edges)
    assert result.box_name == ""
    assert result.dev_url == ""
    assert edges.http_calls == []
    # only ONE ssh call happened (the probe) -- sync_to_box's drift re-probe
    # never runs because no box matched
    assert len(edges.ssh_calls) == 1


def test_execute_no_boxes_configured_is_done_with_no_sync(tmp_path):
    result, edges = _run_execute(tmp_path, boxes=[])
    assert result.box_name == ""
    assert edges.ssh_calls == []


# --------------------------------------------------------------------------
# conflicts -- never auto-resolved
# --------------------------------------------------------------------------

def test_execute_merge_conflict_aborts_and_raises_with_file_list(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files="www/home/php/Foo.php\nsrc/Bar.vue\n")
    with pytest.raises(RunnerError, match="merge conflicts outside"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("merge --abort" in f for f in flat)
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)
    assert not any(c[0] == "maintenance/compile-css" for c, _ in edges.calls)
    assert edges.ssh_calls == []      # never got to the box-sync step
    assert not edges.claude_ran      # source conflicts NEVER escalate


def test_execute_merge_conflict_error_names_the_conflicting_files(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files="a.php\nb.vue\n")
    with pytest.raises(RunnerError) as ei:
        _run_execute(tmp_path, edges=edges)
    assert "a.php" in str(ei.value) and "b.vue" in str(ei.value)


# --------------------------------------------------------------------------
# 2026-08-24: safe-class conflicts escalate to a claude resolver run
# --------------------------------------------------------------------------

_CACHE_BUSTER = "www/home/php/templates/tab_bar_common_logic.php"


def test_safe_conflicts_classification():
    from worksweep.keepcurrent import safe_conflicts
    assert safe_conflicts([_CACHE_BUSTER])
    assert safe_conflicts(["www/home/css/style.css",
                           "www/home/css/style.css.map"])
    assert safe_conflicts(["www/home/dealer/acme/style.css",
                           "www/home/dealer/acme/style.css.map",
                           _CACHE_BUSTER])
    assert not safe_conflicts([])                       # unknown, not safe
    assert not safe_conflicts([_CACHE_BUSTER, "www/home/php/Foo.php"])
    assert not safe_conflicts(["www/home/scss/style.scss"])
    assert not safe_conflicts(["www/home/dealer/acme/other.css"])


def test_cache_buster_conflict_escalates_resolves_and_pushes(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n")
    result, edges = _run_execute(tmp_path, edges=edges)
    assert edges.claude_ran
    claude_calls = [c for c, kw in edges.calls if c[0] == "claude"]
    assert claude_calls and claude_calls[0][1] == "-p"
    assert _CACHE_BUSTER in claude_calls[0][2]          # prompt names the file
    kw = next(kw for c, kw in edges.calls if c[0] == "claude")
    assert kw.get("cwd") == edges.checkout              # runs IN the worktree
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any(f.endswith("push origin feat/1701-thing") for f in flat)
    assert result.conflicts_resolved == (_CACHE_BUSTER,)


def test_resolver_failure_restores_and_raises(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n", claude_rc=1)
    with pytest.raises(RunnerError, match="conflict resolver failed"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("reset -q --hard pre123" in f for f in flat)
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


def test_resolver_timeout_restores_and_raises(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n",
                   claude_raises_timeout=True)
    with pytest.raises(RunnerError, match="timed out"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("reset -q --hard pre123" in f for f in flat)
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


def test_resolver_leaving_unresolved_conflicts_fails_verification(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n",
                   resolve_unresolved_out=_CACHE_BUSTER + "\n")
    with pytest.raises(RunnerError, match="unresolved"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


def test_resolver_not_committing_fails_verification(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n",
                   resolve_no_commit=True)
    with pytest.raises(RunnerError, match="did not commit"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


def test_resolver_head_not_a_master_merge_fails_verification(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n",
                   parent2_sha="something-else")
    with pytest.raises(RunnerError, match="not a merge of origin/master"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("reset -q --hard pre123" in f for f in flat)
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


def test_resolver_dirty_tree_fails_verification(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files=_CACHE_BUSTER + "\n",
                   resolve_status_out=" M www/home/php/other.php\n")
    with pytest.raises(RunnerError, match="dirty tree"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)


# --------------------------------------------------------------------------
# review fix I4: preflight worktree hygiene + merge-exception abort
# --------------------------------------------------------------------------

def test_preflight_aborts_a_dangling_merge_head_before_the_real_merge(tmp_path):
    edges = _Edges(merge_head_present=True)
    result, edges = _run_execute(tmp_path, edges=edges)
    assert isinstance(result, KeepCurrentResult)     # happy path still completes
    flat = [" ".join(c) for c, _ in edges.calls]
    abort_idx = next(i for i, f in enumerate(flat) if "merge --abort" in f)
    real_merge_idx = next(i for i, f in enumerate(flat)
                          if f.endswith("merge origin/master --no-edit"))
    assert abort_idx < real_merge_idx


def test_preflight_leaves_a_clean_worktree_alone(tmp_path):
    """No MERGE_HEAD, no dirty tree -> no abort, no reset, no clean."""
    _, edges = _run_execute(tmp_path)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any("merge --abort" in f for f in flat)
    assert not any(f.endswith(" reset -q --hard") for f in flat)
    assert not any(f.endswith(" clean -fdq") for f in flat)


def test_preflight_hard_resets_a_dirty_worktree(tmp_path):
    edges = _Edges(status_out=" M some/leftover/file.php\n")
    result, edges = _run_execute(tmp_path, edges=edges)
    assert isinstance(result, KeepCurrentResult)
    calls = [c for c, kw in edges.calls]
    checkout = edges.checkout
    assert ["git", "-C", checkout, "reset", "-q", "--hard"] in calls
    assert ["git", "-C", checkout, "clean", "-fdq"] in calls


def test_merge_exception_aborts_before_reraising(tmp_path):
    """A merge timeout must still abort so MERGE_HEAD isn't left behind for
    the next run -- and the original exception propagates unwrapped."""
    edges = _Edges(merge_raises_timeout=True)
    with pytest.raises(subprocess.TimeoutExpired):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("merge --abort" in f for f in flat)


# --------------------------------------------------------------------------
# review fix C2: scss no-op safety + narrowed pathspec
# --------------------------------------------------------------------------

def test_scss_predicate_uses_narrowed_pathspec_not_wildcard(tmp_path):
    """compile-css only ever reads www/home/scss/* -- admin-app scss lives
    under a different tree and must never trip this predicate."""
    _, edges = _run_execute(tmp_path, edges=_Edges(scss_diff_out=""))
    flat = [" ".join(c) for c, _ in edges.calls]
    scss_diff_calls = [f for f in flat if "diff --name-only" in f and "HEAD --" in f]
    assert scss_diff_calls, "scss predicate diff was never run"
    assert any("www/home/scss/*" in f for f in scss_diff_calls)
    assert not any("*.scss" in f for f in scss_diff_calls)


def test_scss_change_with_identical_compiled_output_skips_the_commit(tmp_path):
    """A master-side scss change that recompiles byte-identical (the common
    case) stages nothing -- `git diff --cached --quiet` exits 0 -> no commit,
    scss_recompiled=False, and this is still a `done` outcome, not an error."""
    edges = _Edges(scss_diff_out="www/home/scss/_totals.scss\n", staged_diff_rc=0)
    result, edges = _run_execute(tmp_path, edges=edges)
    assert isinstance(result, KeepCurrentResult)
    assert result.scss_recompiled is False
    flat = [(c, kw) for c, kw in edges.calls]
    # compile DID run (the predicate fired) but nothing was committed
    assert any(c[0] == "maintenance/compile-css" for c, kw in flat)
    assert not any(c[:4] == ["git", "-C", edges.checkout, "commit"] for c, kw in flat)


def test_scss_change_with_real_diff_commits(tmp_path):
    """The default _Edges (staged_diff_rc=1) mirrors the common branch-
    affecting change: `git diff --cached --quiet` exits 1 -> commit."""
    edges = _Edges(scss_diff_out="www/home/scss/_totals.scss\n")
    result, edges = _run_execute(tmp_path, edges=edges)
    assert result.scss_recompiled is True
    flat = [c for c, kw in edges.calls]
    assert any(c[:4] == ["git", "-C", edges.checkout, "commit"] for c in flat)


# --------------------------------------------------------------------------
# review fix I3: a failed compile-css leaves the tree dirty -- clean it up
# --------------------------------------------------------------------------

def test_compile_failure_cleans_scss_dir_before_raising(tmp_path):
    edges = _Edges(scss_diff_out="www/home/scss/_totals.scss\n", compile_rc=1)
    with pytest.raises(RunnerError, match="compile-css failed"):
        _run_execute(tmp_path, edges=edges)
    calls = [c for c, kw in edges.calls]
    checkout = edges.checkout
    assert ["git", "-C", checkout, "checkout", "--", "www/home/scss"] in calls
    assert ["git", "-C", checkout, "clean", "-fdq", "www/home/scss"] in calls
    # never staged (let alone committed) a broken compile
    assert not any(c[3:4] == ["add"] for c in calls)
    assert not any(c[3:4] == ["commit"] for c in calls)


# --------------------------------------------------------------------------
# sync failure -> error, not done
# --------------------------------------------------------------------------

def test_execute_sync_verify_failure_raises(tmp_path):
    edges = _Edges(sync_landed_sha="SOMETHING-ELSE")
    with pytest.raises(RunnerError, match="did NOT land"):
        _run_execute(tmp_path, edges=edges)


def test_execute_push_failure_raises(tmp_path):
    with pytest.raises(RunnerError, match="rejected"):
        _run_execute(tmp_path, edges=_Edges(push_rc=1))


# --------------------------------------------------------------------------
# review fix I5: probe/sync ssh split
# --------------------------------------------------------------------------

def test_probe_and_sync_use_separate_ssh_edges(tmp_path):
    """The branch-discovery fan-out (devslots.probe over every configured
    box) must use run_ssh_probe; only sync_to_box's drift re-probe + write
    against the ONE matched box use run_ssh."""
    probe_calls, sync_calls = [], []

    def probe_ssh(host, cmd):
        probe_calls.append((host, cmd))
        return "feat/1701-thing\nboxsha\n"

    def sync_ssh(host, cmd):
        sync_calls.append((host, cmd))
        if len(sync_calls) == 1:
            return "feat/1701-thing\nboxsha\n"       # drift re-probe
        return "remote: feat/1701-thing @ post456\npost456\n"

    root = tmp_path / "pb-www"
    root.mkdir(exist_ok=True)
    edges = _Edges()
    result = execute(_item(), _cfg(tmp_path), _BOXES,
                     run_subprocess=edges.run, run_ssh_probe=probe_ssh,
                     run_ssh=sync_ssh, http_get=edges.http)
    assert len(probe_calls) == 1
    assert len(sync_calls) == 2
    assert result.box_name == "dev4"


def test_execute_missing_probe_edge_raises(tmp_path):
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    with pytest.raises(RunnerError, match="ssh/http"):
        execute(_item(), _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run,
                run_ssh=lambda h, c: "", http_get=lambda u: 200)


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------

def test_execute_missing_checkout_raises(tmp_path):
    with pytest.raises(RunnerError, match="no checkout"):
        execute(_item(), _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run,
                run_ssh_probe=lambda h, c: "", run_ssh=lambda h, c: "",
                http_get=lambda u: 200)


def test_execute_missing_branch_raises(tmp_path):
    import dataclasses
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    no_branch = dataclasses.replace(_item(), branch="")
    with pytest.raises(RunnerError, match="source branch"):
        execute(no_branch, _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run,
                run_ssh_probe=lambda h, c: "", run_ssh=lambda h, c: "",
                http_get=lambda u: 200)


def test_execute_without_ssh_http_edges_raises(tmp_path):
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    with pytest.raises(RunnerError, match="ssh/http"):
        execute(_item(), _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run)


# --------------------------------------------------------------------------
# every subprocess gets stdin=DEVNULL (mirrors implementer's C1 fix)
# --------------------------------------------------------------------------

def test_every_subprocess_gets_devnull_stdin(tmp_path):
    edges = _Edges(scss_diff_out="www/home/scss/_x.scss\n")
    _, edges = _run_execute(tmp_path, edges=edges)
    for cmd, kw in edges.calls:
        assert kw.get("stdin") is subprocess.DEVNULL, \
            f"{cmd[0]} {cmd[1:3]} was spawned without stdin=DEVNULL"
