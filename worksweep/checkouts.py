"""M4 Task H review fix (C1): per-executor git worktrees.

magi-review, implement, keep-current and address-feedback each need a checkout
directory for one repo. magi-review only ever runs a read-only `git fetch` there (never
`checkout -B`), so it keeps using the shared clone directly -- unchanged,
backward compatible. implement (a 90-min `/rubric:do` run) and keep-current
(merge + push) BOTH switch branches and mutate the working tree; sharing one
directory between overlapping runs of those two (or two overlapping fires of
the SAME one) would let one launchd fire's `checkout -B` yank the branch out
from under the other's live work — that's exactly what happened before this
fix: a keep-current `checkout -B feat/1701-y` could switch the branch under
a live 90-minute implement run on `feat/1775-x`.

address-feedback (2026-08-25) is in the same class as keep-current: it does
`checkout -B <branch>` and commits, so it gets its own worktree for exactly
the same reason.

`worktree_for` gives implement, keep-current and address-feedback EACH their
own git worktree,
created on first use from the shared clone:

    <checkouts_root>/<repo>                          <- magi-review (shared)
    <checkouts_root>/.worktrees/<repo>-implement      <- implement
    <checkouts_root>/.worktrees/<repo>-keep-current   <- keep-current
    <checkouts_root>/.worktrees/<repo>-address-feedback <- address-feedback

Idempotent: a worktree directory that already exists and answers `git
rev-parse --git-dir` is reused as-is (no `worktree add` re-run). One that
exists as a directory but fails that check -- e.g. the shared clone's
`.git/worktrees/<name>` bookkeeping was lost while the leaf directory
survived -- is pruned (from the shared clone) and its stray directory
cleared, then recreated from scratch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, List, Optional

from .runner import RunnerError

_WORKTREE_EXECUTORS = ("implement", "keep-current",
                       "address-feedback")
_WORKTREE_TIMEOUT = 120
_TAIL_LINES = 15


def worktree_for(cfg, repo: str, executor: str,
                 run_subprocess: Callable = subprocess.run) -> str:
    """The checkout directory `executor` should use for `repo`. See the
    module docstring for the layout. Raises RunnerError when the shared
    clone (`<checkouts_root>/<repo>`) is missing, or when `git worktree add`
    fails while creating a fresh worktree."""
    root = os.path.join(cfg.checkouts_root or "", repo)
    if executor not in _WORKTREE_EXECUTORS:
        return root
    if not os.path.isdir(root):
        raise RunnerError(f"no checkout for {repo} at {root}")

    path = os.path.join(cfg.checkouts_root or "", ".worktrees",
                        f"{repo}-{executor}")
    if os.path.isdir(path):
        check = _run(["git", "-C", path, "rev-parse", "--git-dir"],
                     run_subprocess)
        if check.returncode == 0:
            return path
        # The leaf directory survived but isn't a healthy worktree anymore
        # (e.g. the shared clone's bookkeeping was lost) -- prune the shared
        # clone's stale entry and clear the stray directory so `add` below
        # lands on a clean path instead of failing "already exists".
        _run(["git", "-C", root, "worktree", "prune"], run_subprocess)
        shutil.rmtree(path, ignore_errors=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    added = _run(["git", "-C", root, "worktree", "add", "--detach", path,
                 "origin/master"], run_subprocess)
    if added.returncode != 0:
        out = f"{added.stderr or ''}{added.stdout or ''}"
        raise RunnerError(f"git worktree add {path} failed: {_tail(out)}")
    return path


def detach(checkout: str, run_subprocess: Callable = subprocess.run) -> bool:
    """Park `checkout` on a detached HEAD, releasing whatever branch it holds.

    Best-effort by design, and the return value is advisory: every caller runs
    this in a `finally`, where raising would replace the run's real outcome
    (success, or the failure the human actually needs to read) with a tidying-up
    error.

    This exists because the worktrees are PERMANENT. Without it each one keeps
    its last run's branch checked out forever, and the next executor to want
    that branch -- in a different worktree -- gets git's
    "already used by worktree at ..." fatal. That is exactly how the first
    live address-feedback run died (2026-08-26): keep-current had merged the
    branch days earlier and never let go.
    """
    try:
        return _run(["git", "-C", checkout, "checkout", "--detach"],
                    run_subprocess).returncode == 0
    except Exception:
        return False


def checkout_branch(cfg, checkout: str, branch: str,
                    start_point: Optional[str] = None,
                    run_subprocess: Callable = subprocess.run) -> None:
    """Put `branch` in `checkout`, recovering from one of OUR OWN worktrees
    still holding it.

    `start_point` given -> `checkout -B branch start_point` (create/reset);
    omitted -> `checkout branch` (switch to an existing branch without moving
    it, which is what a re-run onto prior work needs).

    On the failure path only, ask git which worktree owns the branch. If it is
    one of ours under `<checkouts_root>/.worktrees/` AND its tree is clean,
    the branch is a leftover rather than a conflict: detach that worktree and
    retry once. Any other holder -- a ferdinand worktree, a /tmp clone,
    Chandler's own checkout, or any dirty tree -- is somebody's live work and
    the original fatal is raised untouched.
    """
    args = (["checkout", "-B", branch, start_point] if start_point
            else ["checkout", branch])
    proc = _run(["git", "-C", checkout] + args, run_subprocess)
    if proc.returncode == 0:
        return
    first = _tail(f"{proc.stderr or ''}{proc.stdout or ''}")

    holder = _branch_holder(run_subprocess, checkout, branch)
    if (holder and holder != checkout
            and _is_our_worktree(cfg, holder)
            and _is_clean(run_subprocess, holder)
            and detach(holder, run_subprocess)):
        retry = _run(["git", "-C", checkout] + args, run_subprocess)
        if retry.returncode == 0:
            return
        first = _tail(f"{retry.stderr or ''}{retry.stdout or ''}")
    raise RunnerError(f"git {' '.join(args)} failed in {checkout}: {first}")


def _branch_holder(run_subprocess: Callable, checkout: str,
                   branch: str) -> str:
    """Path of the worktree that has `branch` checked out, or "".

    Read from `git worktree list --porcelain` rather than scraped out of the
    fatal message: the wording of that message is not a contract, and the
    porcelain format is.
    """
    listed = _run(["git", "-C", checkout, "worktree", "list", "--porcelain"],
                  run_subprocess)
    if listed.returncode != 0:
        return ""
    path = ""
    for line in (listed.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return ""


def _is_our_worktree(cfg, path: str) -> bool:
    """True only for a directory INSIDE `<checkouts_root>/.worktrees/`.

    Compared component-wise via commonpath, not with startswith: a sibling
    named `.worktrees-evil` shares the prefix but is not ours to touch.
    """
    root = os.path.join(cfg.checkouts_root or "", ".worktrees")
    if not (cfg.checkouts_root or ""):
        return False
    try:
        return os.path.commonpath([os.path.abspath(root),
                                   os.path.abspath(path)]) == \
            os.path.abspath(root) and os.path.abspath(path) != \
            os.path.abspath(root)
    except ValueError:              # different drives / relative vs absolute
        return False


def _is_clean(run_subprocess: Callable, checkout: str) -> bool:
    status = _run(["git", "-C", checkout, "status", "--porcelain"],
                  run_subprocess)
    return status.returncode == 0 and not (status.stdout or "").strip()


def _run(cmd: List[str], run_subprocess: Callable):
    """The ONLY way this module spawns a process — stdin=DEVNULL is
    non-negotiable under launchd (see implementer._run's docstring)."""
    return run_subprocess(cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=_WORKTREE_TIMEOUT)


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])
