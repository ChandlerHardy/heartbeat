"""M4 Task H: the `keep-current` executor's subprocess/ssh/http contract.

Every edge is injected — this file must never touch the network, a real
checkout, ssh, or a real git binary."""
import os
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
        self.conflict_files = kw.get("conflict_files", "a.php\nb.php\n")
        self.scss_diff_out = kw.get("scss_diff_out", "")
        self.compile_rc = kw.get("compile_rc", 0)
        self.push_rc = kw.get("push_rc", 0)
        # what devslots.probe (first ssh call) and sync_to_box's drift
        # re-probe (second ssh call) see on the box:
        self.probe_branch = kw.get("probe_branch", "feat/1701-thing")
        self.probe_sha = kw.get("probe_sha", "boxsha")
        self.sync_landed_sha = kw.get("sync_landed_sha", None)
        self._rev_parse_n = 0

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        c = list(cmd)
        if c and c[0] == "git":
            sub = c[3] if len(c) > 3 else ""
            if sub == "rev-parse":
                self._rev_parse_n += 1
                sha = self.pre_sha if self._rev_parse_n == 1 else self.head_sha
                return subprocess.CompletedProcess(c, 0, stdout=sha + "\n", stderr="")
            if sub == "rev-list":
                return subprocess.CompletedProcess(c, 0, stdout=self.rev_list_out,
                                                   stderr="")
            if sub == "merge":
                if "--abort" in c:
                    return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
                err = "CONFLICT (content): Merge conflict\n" if self.merge_rc else ""
                return subprocess.CompletedProcess(c, self.merge_rc, stdout="",
                                                   stderr=err)
            if sub == "diff":
                if "--diff-filter=U" in c:
                    return subprocess.CompletedProcess(c, 0, stdout=self.conflict_files,
                                                       stderr="")
                return subprocess.CompletedProcess(c, 0, stdout=self.scss_diff_out,
                                                   stderr="")
            if sub == "push":
                err = "rejected\n" if self.push_rc else ""
                return subprocess.CompletedProcess(c, self.push_rc, stdout="", stderr=err)
            return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
        if c and c[0] == "maintenance/compile-css":
            err = "sass error\n" if self.compile_rc else ""
            return subprocess.CompletedProcess(c, self.compile_rc, stdout="", stderr=err)
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
    co = tmp_path / "pb-www"
    co.mkdir(exist_ok=True)
    edges = edges or _Edges()
    edges.checkout = str(co)
    return execute(item or _item(), cfg or _cfg(tmp_path),
                   boxes if boxes is not None else _BOXES,
                   run_subprocess=edges.run, run_ssh=edges.ssh,
                   http_get=edges.http), edges


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
    assert not any("commit" in f for f in flat)
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
    with pytest.raises(RunnerError, match="merge conflicts in:"):
        _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("merge --abort" in f for f in flat)
    assert not any(f.endswith("push origin feat/1701-thing") for f in flat)
    assert not any(c[0] == "maintenance/compile-css" for c, _ in edges.calls)
    assert edges.ssh_calls == []      # never got to the box-sync step


def test_execute_merge_conflict_error_names_the_conflicting_files(tmp_path):
    edges = _Edges(merge_rc=1, conflict_files="a.php\nb.vue\n")
    with pytest.raises(RunnerError) as ei:
        _run_execute(tmp_path, edges=edges)
    assert "a.php" in str(ei.value) and "b.vue" in str(ei.value)


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
# input validation
# --------------------------------------------------------------------------

def test_execute_missing_checkout_raises(tmp_path):
    with pytest.raises(RunnerError, match="no checkout"):
        execute(_item(), _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run,
                run_ssh=lambda h, c: "", http_get=lambda u: 200)


def test_execute_missing_branch_raises(tmp_path):
    import dataclasses
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    no_branch = dataclasses.replace(_item(), branch="")
    with pytest.raises(RunnerError, match="source branch"):
        execute(no_branch, _cfg(tmp_path), _BOXES, run_subprocess=_Edges().run,
                run_ssh=lambda h, c: "", http_get=lambda u: 200)


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
