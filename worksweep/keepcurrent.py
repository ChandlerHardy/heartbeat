"""M4 Task H: the `keep-current` executor — merges origin/master into a
stale authored MR's branch, recompiles SCSS when the merge touched any, and
syncs the result onto whichever dev box currently serves that branch.

    fetch -> checkout branch -> merge origin/master --no-edit
    -> (scss changed? compile-css + commit) -> push
    -> find the box serving this branch -> implementer.sync_to_box

This is a SHORT git op, deliberately sharing the magi-review lock/pass
(runner._run_magi_pass) rather than getting its own lock file — see
runner.py's module docstring for the pass-sharing rationale.

Every edge is injected (`run_subprocess`, `run_ssh`, `http_get`); this module
never shells out or sshs on its own, matching implementer.py's discipline.
Contract with the runner:

* `RunnerError` -> the item goes `error`, ⚠️ posted, re-proposed next sweep.
  Merge conflicts are ALWAYS a RunnerError — never auto-resolved (mirrors the
  merge-master skill's Non-Negotiable 2: only the compiled-CSS-artifact and
  `$script_version` conflict classes documented there are safe to
  auto-resolve, and both are out of scope for this v1 -- ANY conflict here
  stops and reports).
* a returned `KeepCurrentResult` -> the item goes `done`, and the 🔄 post
  names the commits merged in, the SCSS outcome, and the sync outcome (or
  the fact that no dev box currently serves the branch, which is a `done`
  outcome too — not an error, since the merge+push already succeeded).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Sequence

from . import devslots
from .implementer import sync_to_box
from .models import WorkItem
from .runner import RunnerError

_FETCH_TIMEOUT = 120
_GIT_TIMEOUT = 120
_PUSH_TIMEOUT = 300
_COMPILE_TIMEOUT = 300
_TAIL_LINES = 15


@dataclass(frozen=True)
class KeepCurrentResult:
    iid: int              # the authored MR whose branch was brought current
    ahead_count: int      # commits merged in from origin/master
    box_name: str          # "" = no dev box currently serves this branch
    scss_recompiled: bool
    result_sha: str = ""   # branch HEAD after merge (+ scss commit) and push
    dev_url: str = ""      # the synced box's url, "" when box_name == ""


def iid_of(item: WorkItem) -> int:
    """MR iid from the item's web_url (`.../merge_requests/<iid>`). Raises
    rather than guessing -- a wrong iid would merge master into someone
    else's branch."""
    m = re.search(r"/merge_requests/(\d+)", item.web_url or "")
    if not m:
        raise RunnerError(f"cannot find MR iid in web_url: {item.web_url!r}")
    return int(m.group(1))


def execute(item: WorkItem, cfg, boxes: Sequence[dict],
            run_subprocess: Callable = subprocess.run,
            run_ssh: Callable[[str, str], str] = None,
            http_get: Callable[[str], int] = None) -> KeepCurrentResult:
    """Bring one stale authored MR's branch current with master. See the
    module docstring for the two-outcome contract."""
    checkout = os.path.join(cfg.checkouts_root or "", item.repo)
    if not os.path.isdir(checkout):
        raise RunnerError(f"no checkout for {item.repo} at {checkout}")
    if run_ssh is None or http_get is None:
        raise RunnerError("keep-current executor is wired without an "
                          "ssh/http edge")
    iid = iid_of(item)
    branch = item.branch
    if not branch:
        raise RunnerError(f"no source branch recorded for !{iid} "
                          f"(WorkItem.branch was not set by assess_stale)")

    _git(run_subprocess, checkout, ["fetch", "origin", "master", branch],
        timeout=_FETCH_TIMEOUT)
    _git(run_subprocess, checkout, ["checkout", "-B", branch, f"origin/{branch}"])
    pre = _git(run_subprocess, checkout, ["rev-parse", "HEAD"]).strip()
    try:
        ahead = int(_git(run_subprocess, checkout,
                         ["rev-list", "--count", f"{pre}..origin/master"],
                         allow_fail=True).strip() or "0")
    except ValueError:
        ahead = 0

    merge = _run(["git", "-C", checkout, "merge", "origin/master", "--no-edit"],
                run_subprocess, timeout=_GIT_TIMEOUT)
    if merge.returncode != 0:
        conflicts = _git(run_subprocess, checkout,
                         ["diff", "--name-only", "--diff-filter=U"],
                         allow_fail=True)
        # Best-effort abort -- never let a failed abort mask the real
        # conflict error the human needs to see.
        _run(["git", "-C", checkout, "merge", "--abort"], run_subprocess,
            timeout=_GIT_TIMEOUT)
        files = ", ".join(conflicts.split()) or "(unknown)"
        raise RunnerError(f"merge conflicts in: {files}")

    scss_changed = _git(run_subprocess, checkout,
                        ["diff", "--name-only", f"{pre}..HEAD", "--", "*.scss"])
    scss_recompiled = False
    if scss_changed.strip():
        compile_proc = _run(["maintenance/compile-css"], run_subprocess,
                            cwd=checkout, timeout=_COMPILE_TIMEOUT)
        if compile_proc.returncode != 0:
            compile_out = f"{compile_proc.stderr or ''}{compile_proc.stdout or ''}"
            raise RunnerError(f"maintenance/compile-css failed: "
                              f"{_tail(compile_out)}")
        _git(run_subprocess, checkout, ["add", "www/home/css/", "www/home/dealer/"])
        _git(run_subprocess, checkout,
            ["commit", "-m", "chore: compile CSS after master merge"])
        scss_recompiled = True

    head = _git(run_subprocess, checkout, ["rev-parse", "HEAD"]).strip()
    _git(run_subprocess, checkout, ["push", "origin", branch], timeout=_PUSH_TIMEOUT)

    probed = devslots.probe(list(boxes), run_ssh)
    box = next((b for b in probed if b.branch == branch), None)
    box_name, dev_url = "", ""
    if box is not None:
        # Drift guard: claim_branch/claim_sha are exactly what the probe
        # above just saw, so sync_to_box refuses the box if it moves again
        # between here and the ssh half of the sync.
        sync_to_box(box, branch, run_ssh, http_get, expected_sha=head,
                   claim_branch=branch, claim_sha=box.sha)
        box_name, dev_url = box.name, box.url

    return KeepCurrentResult(iid=iid, ahead_count=ahead, box_name=box_name,
                             scss_recompiled=scss_recompiled, result_sha=head,
                             dev_url=dev_url)


def _run(cmd: List[str], run_subprocess: Callable, **kw):
    """The ONLY way this module spawns a process -- stdin=DEVNULL is
    non-negotiable under launchd (see implementer._run's docstring)."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return run_subprocess(cmd, stdin=subprocess.DEVNULL, **kw)


def _git(run_subprocess: Callable, checkout: str, args: List[str],
        timeout: int = _GIT_TIMEOUT, allow_fail: bool = False) -> str:
    cmd = ["git", "-C", checkout] + list(args)
    try:
        proc = _run(cmd, run_subprocess, timeout=timeout)
    except subprocess.TimeoutExpired:
        if allow_fail:
            return ""
        raise RunnerError(f"git {args[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        if allow_fail:
            return ""
        out = f"{proc.stderr or ''}{proc.stdout or ''}"
        raise RunnerError(f"git {' '.join(args)} failed: {_tail(out)}")
    return proc.stdout or ""


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])
