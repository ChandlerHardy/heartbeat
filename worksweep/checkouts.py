"""M4 Task H review fix (C1): per-executor git worktrees.

magi-review, implement, and keep-current each need a checkout directory for
one repo. magi-review only ever runs a read-only `git fetch` there (never
`checkout -B`), so it keeps using the shared clone directly -- unchanged,
backward compatible. implement (a 90-min `/rubric:do` run) and keep-current
(merge + push) BOTH switch branches and mutate the working tree; sharing one
directory between overlapping runs of those two (or two overlapping fires of
the SAME one) would let one launchd fire's `checkout -B` yank the branch out
from under the other's live work — that's exactly what happened before this
fix: a keep-current `checkout -B feat/1701-y` could switch the branch under
a live 90-minute implement run on `feat/1775-x`.

`worktree_for` gives implement and keep-current EACH their own git worktree,
created on first use from the shared clone:

    <checkouts_root>/<repo>                          <- magi-review (shared)
    <checkouts_root>/.worktrees/<repo>-implement      <- implement
    <checkouts_root>/.worktrees/<repo>-keep-current   <- keep-current

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
from typing import Callable, List

from .runner import RunnerError

_WORKTREE_EXECUTORS = ("implement", "keep-current")
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


def _run(cmd: List[str], run_subprocess: Callable):
    """The ONLY way this module spawns a process — stdin=DEVNULL is
    non-negotiable under launchd (see implementer._run's docstring)."""
    return run_subprocess(cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=_WORKTREE_TIMEOUT)


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])
