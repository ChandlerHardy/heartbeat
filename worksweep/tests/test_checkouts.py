"""M4 Task H review fix (C1): per-executor git worktrees.

`worktree_for` mixes a real filesystem check (does the worktree directory
already exist and answer `rev-parse --git-dir`?) with an injected
`run_subprocess` edge for every git call -- so these tests use real tmp_path
directories but a faked git binary, matching checkouts.py's own contract.
"""
import subprocess

import pytest

from worksweep.checkouts import worktree_for
from worksweep.config import WorksweepConfig
from worksweep.runner import RunnerError


def _cfg(tmp_path):
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y",
                           checkouts_root=str(tmp_path))


class _Git:
    """Scriptable fake git: records every call, and lets a test control the
    exit code of `rev-parse --git-dir` (worktree health check) and
    `worktree add` (creation)."""

    def __init__(self, git_dir_rc=0, add_rc=0):
        self.calls = []
        self.git_dir_rc = git_dir_rc
        self.add_rc = add_rc

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        c = list(cmd)
        if c[3:5] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(c, self.git_dir_rc, stdout="", stderr="")
        if c[3:5] == ["worktree", "add"]:
            err = "fatal: already exists\n" if self.add_rc else ""
            return subprocess.CompletedProcess(c, self.add_rc, stdout="", stderr=err)
        return subprocess.CompletedProcess(c, 0, stdout="", stderr="")


# --------------------------------------------------------------------------
# magi-review: unchanged, shared clone
# --------------------------------------------------------------------------

def test_magi_review_gets_the_shared_clone_unchanged(tmp_path):
    (tmp_path / "pb-www").mkdir()
    git = _Git()
    path = worktree_for(_cfg(tmp_path), "pb-www", "magi-review", git.run)
    assert path == str(tmp_path / "pb-www")
    assert git.calls == []          # no subprocess call at all -- pure path math


def test_unknown_executor_also_gets_the_shared_clone(tmp_path):
    (tmp_path / "pb-www").mkdir()
    path = worktree_for(_cfg(tmp_path), "pb-www", "triage")
    assert path == str(tmp_path / "pb-www")


# --------------------------------------------------------------------------
# implement / keep-current: dedicated worktrees
# --------------------------------------------------------------------------

def test_missing_shared_clone_raises(tmp_path):
    with pytest.raises(RunnerError, match="no checkout"):
        worktree_for(_cfg(tmp_path), "pb-www", "implement", _Git().run)


def test_create_when_no_worktree_exists_yet(tmp_path):
    (tmp_path / "pb-www").mkdir()
    git = _Git()
    path = worktree_for(_cfg(tmp_path), "pb-www", "implement", git.run)
    assert path == str(tmp_path / ".worktrees" / "pb-www-implement")
    add_calls = [c for c, kw in git.calls if c[3:5] == ["worktree", "add"]]
    assert len(add_calls) == 1
    assert add_calls[0] == ["git", "-C", str(tmp_path / "pb-www"), "worktree",
                            "add", "--detach", path, "origin/master"]
    # never probed rev-parse --git-dir -- there was nothing to reuse yet
    assert not any(c[3:5] == ["rev-parse", "--git-dir"] for c, kw in git.calls)


def test_implement_and_keep_current_get_different_worktrees(tmp_path):
    (tmp_path / "pb-www").mkdir()
    git = _Git()
    implement_path = worktree_for(_cfg(tmp_path), "pb-www", "implement", git.run)
    keep_current_path = worktree_for(_cfg(tmp_path), "pb-www", "keep-current", git.run)
    assert implement_path != keep_current_path
    assert implement_path == str(tmp_path / ".worktrees" / "pb-www-implement")
    assert keep_current_path == str(tmp_path / ".worktrees" / "pb-www-keep-current")


def test_reuse_when_a_healthy_worktree_already_exists(tmp_path):
    (tmp_path / "pb-www").mkdir()
    existing = tmp_path / ".worktrees" / "pb-www-implement"
    existing.mkdir(parents=True)
    git = _Git(git_dir_rc=0)      # `rev-parse --git-dir` succeeds -> healthy
    path = worktree_for(_cfg(tmp_path), "pb-www", "implement", git.run)
    assert path == str(existing)
    # reused -- never re-ran `worktree add`
    assert not any(c[3:5] == ["worktree", "add"] for c, kw in git.calls)
    assert any(c[3:5] == ["rev-parse", "--git-dir"] for c, kw in git.calls)


def test_prune_and_recreate_when_worktree_is_broken(tmp_path):
    """The leaf directory survives but `rev-parse --git-dir` fails (the
    shared clone's `.git/worktrees/<name>` bookkeeping was lost) -> prune the
    shared clone's stale entry, clear the stray directory, and add fresh."""
    (tmp_path / "pb-www").mkdir()
    broken = tmp_path / ".worktrees" / "pb-www-implement"
    broken.mkdir(parents=True)
    (broken / "stray.txt").write_text("leftover")
    git = _Git(git_dir_rc=1)      # unhealthy
    path = worktree_for(_cfg(tmp_path), "pb-www", "implement", git.run)
    assert path == str(broken)
    prune_calls = [c for c, kw in git.calls if c[3:4] == ["worktree"] and c[4:5] == ["prune"]]
    assert len(prune_calls) == 1
    assert prune_calls[0][:3] == ["git", "-C", str(tmp_path / "pb-www")]
    add_calls = [c for c, kw in git.calls if c[3:5] == ["worktree", "add"]]
    assert len(add_calls) == 1
    # the stray directory's old contents are gone (cleared before the re-add)
    assert not (broken / "stray.txt").exists()


def test_worktree_add_failure_raises(tmp_path):
    (tmp_path / "pb-www").mkdir()
    git = _Git(add_rc=1)
    with pytest.raises(RunnerError, match="worktree add"):
        worktree_for(_cfg(tmp_path), "pb-www", "keep-current", git.run)


def test_every_call_gets_devnull_stdin(tmp_path):
    (tmp_path / "pb-www").mkdir()
    git = _Git()
    worktree_for(_cfg(tmp_path), "pb-www", "implement", git.run)
    for cmd, kw in git.calls:
        assert kw.get("stdin") is subprocess.DEVNULL


# --------------------------------------------------------------------------
# branch collisions between persistent worktrees (2026-08-26 live failure)
#
# The worktrees are permanent and keep the LAST run's branch checked out
# forever. So the first live address-feedback run died at:
#
#   fatal: 'refactor/1681-…' is already used by worktree at
#          …/.worktrees/pb-www-keep-current
#
# keep-current had merged that branch days earlier and simply never let go.
# --------------------------------------------------------------------------

BRANCH = "refactor/1681-analytics-feed-data-debt"


def _worktree_list(*entries):
    """`git worktree list --porcelain` output. Each entry is (path, branch);
    branch None renders a detached worktree."""
    out = []
    for path, branch in entries:
        out.append(f"worktree {path}\nHEAD abc123\n"
                   + (f"branch refs/heads/{branch}\n" if branch
                      else "detached\n"))
    return "\n".join(out) + "\n"


class _Collide:
    """A git that refuses the checkout while `holder` still owns the branch.

    Models the real thing: the fatal is emitted by `checkout`, the ownership
    is discoverable via `worktree list`, and detaching the holder makes the
    retry succeed.
    """

    def __init__(self, holder, dirty=False, holder_branch=BRANCH,
                 detach_rc=0, retry_rc=0):
        self.holder, self.dirty = holder, dirty
        self.holder_branch, self.detach_rc = holder_branch, detach_rc
        self.retry_rc = retry_rc
        self.calls, self.released = [], False

    def run(self, cmd, **kw):
        c = list(cmd)
        self.calls.append(c)
        cwd_repo = c[2] if c[:2] == ["git", "-C"] else ""
        rest = c[3:]
        if rest[:1] == ["checkout"] and "--detach" in rest:
            if cwd_repo == self.holder and self.detach_rc == 0:
                self.released = True
            return subprocess.CompletedProcess(c, self.detach_rc, stdout="",
                                               stderr="")
        if rest[:1] == ["checkout"]:
            if self.released:
                return subprocess.CompletedProcess(c, self.retry_rc, stdout="",
                                                   stderr="fatal: retry\n")
            return subprocess.CompletedProcess(
                c, 128, stdout="",
                stderr=f"fatal: '{BRANCH}' is already used by worktree at "
                       f"'{self.holder}'\n")
        if rest[:2] == ["worktree", "list"]:
            return subprocess.CompletedProcess(
                c, 0, stdout=_worktree_list((self.holder, self.holder_branch),
                                            ("/elsewhere/pb-www", None)),
                stderr="")
        if rest[:1] == ["status"]:
            return subprocess.CompletedProcess(
                c, 0, stdout=" M www/home/x.php\n" if self.dirty else "",
                stderr="")
        return subprocess.CompletedProcess(c, 0, stdout="", stderr="")

    def ran(self, *args):
        return [c for c in self.calls if all(a in c for a in args)]


def _sibling(tmp_path, name="pb-www-keep-current"):
    p = tmp_path / ".worktrees" / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def test_a_clean_sibling_worktree_hands_the_branch_over(tmp_path):
    """The whole point: one of OUR worktrees parked on the branch is not a
    conflict, it is a leftover. Detach it and carry on."""
    from worksweep.checkouts import checkout_branch
    holder = _sibling(tmp_path)
    git = _Collide(holder)
    checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                    f"origin/{BRANCH}", git.run)
    assert git.released is True
    assert git.ran("-C", holder, "--detach")
    # and the checkout was actually retried, not merely assumed
    assert len(git.ran("checkout", "-B", BRANCH)) == 2


def test_a_dirty_sibling_worktree_is_left_alone(tmp_path):
    """Uncommitted work in there is somebody's unfinished business. Stealing
    the branch would strand it with no way back."""
    from worksweep.checkouts import checkout_branch
    holder = _sibling(tmp_path)
    git = _Collide(holder, dirty=True)
    with pytest.raises(RunnerError) as e:
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)
    assert "already used by worktree" in str(e.value)
    assert git.released is False
    assert git.ran("--detach") == []


def test_a_worktree_outside_our_root_is_never_touched(tmp_path):
    """Ferdinand worktrees, a /tmp clone, Chandler's own checkout -- none of
    those are ours to reach into, however clean they look."""
    from worksweep.checkouts import checkout_branch
    git = _Collide("/Users/chandlerhardy/workspaces/pla/pla3/pb-www")
    with pytest.raises(RunnerError) as e:
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)
    assert "already used by worktree" in str(e.value)
    assert git.released is False


def test_a_sibling_path_that_merely_starts_with_our_root_is_not_ours(tmp_path):
    """`<root>/.worktrees-evil/x` shares a prefix with `<root>/.worktrees/`
    but is not inside it."""
    from worksweep.checkouts import checkout_branch
    holder = tmp_path / ".worktrees-evil" / "pb-www-keep-current"
    holder.mkdir(parents=True)
    git = _Collide(str(holder))
    with pytest.raises(RunnerError):
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)
    assert git.released is False


def test_a_checkout_that_failed_for_another_reason_still_raises(tmp_path):
    """No worktree holds the branch -- the fatal is about something else, and
    inventing a recovery for it would hide a real problem."""
    from worksweep.checkouts import checkout_branch
    git = _Collide(_sibling(tmp_path), holder_branch=None)
    with pytest.raises(RunnerError):
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)
    assert git.released is False


def test_a_failed_detach_does_not_pretend_to_have_recovered(tmp_path):
    from worksweep.checkouts import checkout_branch
    git = _Collide(_sibling(tmp_path), detach_rc=1)
    with pytest.raises(RunnerError):
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)


def test_the_retry_is_attempted_exactly_once(tmp_path):
    """If the branch is STILL held after a successful detach, something else
    is going on -- do not loop."""
    from worksweep.checkouts import checkout_branch
    git = _Collide(_sibling(tmp_path), retry_rc=128)
    with pytest.raises(RunnerError) as e:
        checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                        f"origin/{BRANCH}", git.run)
    assert len(git.ran("checkout", "-B", BRANCH)) == 2
    assert "retry" in str(e.value)


def test_a_clean_checkout_costs_no_extra_calls(tmp_path):
    """The recovery is a failure path only: the ordinary case must not pay
    for a `worktree list` on every run."""
    from worksweep.checkouts import checkout_branch
    git = _Git()
    checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                    f"origin/{BRANCH}", git.run)
    assert [c[0][3] for c in git.calls] == ["checkout"]


def test_checkout_without_a_start_point_switches_rather_than_creates(tmp_path):
    """implement re-runs onto an EXISTING branch and must never reset it onto
    a start point -- that would discard the prior run's commits."""
    from worksweep.checkouts import checkout_branch
    git = _Git()
    checkout_branch(_cfg(tmp_path), str(tmp_path / "mine"), BRANCH,
                    None, git.run)
    assert git.calls[0][0][3:] == ["checkout", BRANCH]
