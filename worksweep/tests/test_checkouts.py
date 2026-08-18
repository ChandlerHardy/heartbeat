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
