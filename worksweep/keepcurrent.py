"""M4 Task H: the `keep-current` executor — merges origin/master into a
stale authored MR's branch, recompiles SCSS when the merge touched any, and
syncs the result onto whichever dev box currently serves that branch.

    preflight clean -> fetch -> checkout branch -> merge origin/master --no-edit
    -> (scss changed? compile-css + commit-if-changed) -> push
    -> probe for the box serving this branch -> implementer.sync_to_box

This is a SHORT git op, deliberately sharing the magi-review lock/pass
(runner._run_magi_pass) rather than getting its own lock file — see
runner.py's module docstring for the pass-sharing rationale. It runs in its
OWN dedicated git worktree (checkouts.worktree_for), separate from the
`implement` executor's — see checkouts.py's module docstring for why that
separation is load-bearing (review fix C1, 2026-08-18).

Every edge is injected (`run_subprocess`, `run_ssh_probe`, `run_ssh`,
`http_get`); this module never shells out or sshs on its own, matching
implementer.py's discipline. `run_ssh_probe` (fast, ~20s budget) is used only
for the branch-discovery fan-out over every configured box; `run_ssh` (the
longer sync budget, ~300s) is reserved for `sync_to_box`'s drift re-probe +
write against the ONE box that matched (review fix I5).

Contract with the runner:

* `RunnerError` -> the item goes `error`, ⚠️ posted, re-proposed next sweep.
  Merge conflicts: when EVERY conflicted file is in one of the merge-master
  skill's two auto-resolvable classes (the `$script_version` cache-buster and
  compiled-CSS artifacts -- Step 1b of chandler-personal:merge-master, the
  canonical policy), the executor escalates to a short `claude -p` run in the
  worktree to resolve them per that skill, then VERIFIES the outcome in
  Python (no unresolved files, merge committed with origin/master as second
  parent, clean tree) before continuing. Any conflict outside those classes,
  a failed resolver run, or a failed verification restores the worktree and
  raises -- an unattended LLM never resolves source-file conflicts, and
  nothing is pushed unless verification passed (2026-08-24, replacing the v1
  any-conflict-aborts rule after the ranch-data stack all tripped on the
  cache-buster).
* a returned `KeepCurrentResult` -> the item goes `done`, and the 🔄 post
  names the commits merged in, the SCSS outcome, and the sync outcome (or
  the fact that no dev box currently serves the branch, which is a `done`
  outcome too — not an error, since the merge+push already succeeded).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Sequence

from . import checkouts, devslots
from .implementer import sync_to_box
from .models import WorkItem
from .runner import RunnerError

_FETCH_TIMEOUT = 120
_RESOLVE_TIMEOUT = 900
_GIT_TIMEOUT = 120
_PUSH_TIMEOUT = 300
_COMPILE_TIMEOUT = 300
_TAIL_LINES = 15
# review fix C2: compile-css only ever reads/writes www/home/scss/* -- the
# admin app has its own scss under www/admin, a different pipeline this
# executor must not react to.
_SCSS_PATHSPEC = "www/home/scss/*"
# The two conflict classes merge-master Step 1b blesses for auto-resolution.
# Everything else is a source conflict and must abort.
_CACHE_BUSTER = "www/home/php/templates/tab_bar_common_logic.php"
_CSS_ARTIFACT_RE = re.compile(
    r"^www/home/(css/style\.css(\.map)?|dealer/[^/]+/style\.css(\.map)?)$")


def safe_conflicts(files: Sequence[str]) -> bool:
    """True when every conflicted file is in a merge-master Step 1b
    auto-resolvable class. Empty list -> False (a failed merge with no
    conflicted files is something unknown, not something safe)."""
    files = [f for f in files if f]
    return bool(files) and all(
        f == _CACHE_BUSTER or _CSS_ARTIFACT_RE.match(f) for f in files)


@dataclass(frozen=True)
class KeepCurrentResult:
    iid: int              # the authored MR whose branch was brought current
    ahead_count: int      # commits merged in from origin/master
    box_name: str          # "" = no dev box currently serves this branch
    scss_recompiled: bool
    result_sha: str = ""   # branch HEAD after merge (+ scss commit) and push
    dev_url: str = ""      # the synced box's url, "" when box_name == ""
    conflicts_resolved: tuple = ()  # files claude auto-resolved, () = clean merge


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
            run_ssh_probe: Callable[[str, str], str] = None,
            run_ssh: Callable[[str, str], str] = None,
            http_get: Callable[[str], int] = None) -> KeepCurrentResult:
    """Bring one stale authored MR's branch current with master. See the
    module docstring for the two-outcome contract."""
    checkout = checkouts.worktree_for(cfg, item.repo, "keep-current",
                                      run_subprocess)
    if run_ssh_probe is None or run_ssh is None or http_get is None:
        raise RunnerError("keep-current executor is wired without an "
                          "ssh/http edge")
    iid = iid_of(item)
    branch = item.branch
    if not branch:
        raise RunnerError(f"no source branch recorded for !{iid} "
                          f"(WorkItem.branch was not set by assess_stale)")
    try:
        return _execute_in(item, cfg, boxes, checkout, iid, branch,
                           run_subprocess, run_ssh_probe, run_ssh, http_get)
    finally:
        # This worktree is permanent, so holding the branch after the merge
        # blocks any LATER executor that wants it in a different worktree.
        # That is not hypothetical: this executor is what stranded
        # refactor/1681-... and killed the first address-feedback run
        # (2026-08-26). Best-effort and last -- tidying up must never become
        # the reported outcome.
        checkouts.detach(checkout, run_subprocess)


def _execute_in(item: WorkItem, cfg, boxes: Sequence[dict], checkout: str,
                iid: int, branch: str, run_subprocess: Callable,
                run_ssh_probe: Callable, run_ssh: Callable,
                http_get: Callable) -> KeepCurrentResult:

    # review fix I4: this worktree is reused run over run -- a prior claim
    # that timed out mid-merge, or crashed mid-compile, can leave it with a
    # dangling MERGE_HEAD or dirty tracked/untracked files. It's a mirror
    # worktree (nothing of value lives here outside what's pushed), so the
    # fix is unconditional: make it pristine before touching this run's
    # branch.
    _preflight_clean(run_subprocess, checkout)

    _git(run_subprocess, checkout, ["fetch", "origin", "master", branch],
        timeout=_FETCH_TIMEOUT)
    checkouts.checkout_branch(cfg, checkout, branch, f"origin/{branch}",
                              run_subprocess)
    pre = _git(run_subprocess, checkout, ["rev-parse", "HEAD"]).strip()
    try:
        ahead = int(_git(run_subprocess, checkout,
                         ["rev-list", "--count", f"{pre}..origin/master"],
                         allow_fail=True).strip() or "0")
    except ValueError:
        ahead = 0

    merge = _merge_master(run_subprocess, checkout)
    conflicts_resolved: tuple = ()
    if merge.returncode != 0:
        conflicts = _git(run_subprocess, checkout,
                         ["diff", "--name-only", "--diff-filter=U"],
                         allow_fail=True)
        conflict_files = [f for f in conflicts.split() if f]
        if not safe_conflicts(conflict_files):
            # Best-effort abort -- never let a failed abort mask the real
            # conflict error the human needs to see.
            _run(["git", "-C", checkout, "merge", "--abort"], run_subprocess,
                timeout=_GIT_TIMEOUT)
            files = ", ".join(conflict_files) or "(unknown)"
            raise RunnerError(f"merge conflicts outside the auto-resolve "
                              f"classes in: {files}")
        _resolve_conflicts(run_subprocess, checkout, cfg, conflict_files, pre)
        _verify_resolution(run_subprocess, checkout, pre)
        conflicts_resolved = tuple(conflict_files)

    scss_changed = _git(run_subprocess, checkout,
                        ["diff", "--name-only", f"{pre}..HEAD", "--",
                         _SCSS_PATHSPEC])
    scss_recompiled = False
    if scss_changed.strip():
        compile_proc = _run(["maintenance/compile-css"], run_subprocess,
                            cwd=checkout, timeout=_COMPILE_TIMEOUT)
        if compile_proc.returncode != 0:
            # review fix I3: compile-css sed-patches www/home/scss/style.scss
            # and print.scss in place before restoring them -- a failure
            # mid-patch leaves those tracked files (and any *.scss-e sed
            # backups) dirty. Clean up BEFORE raising so the worktree is
            # safe for the next run, not just for the next preflight.
            _run(["git", "-C", checkout, "checkout", "--", "www/home/scss"],
                run_subprocess, timeout=_GIT_TIMEOUT)
            _run(["git", "-C", checkout, "clean", "-fdq", "www/home/scss"],
                run_subprocess, timeout=_GIT_TIMEOUT)
            compile_out = f"{compile_proc.stderr or ''}{compile_proc.stdout or ''}"
            raise RunnerError(f"maintenance/compile-css failed: "
                              f"{_tail(compile_out)}")
        _git(run_subprocess, checkout, ["add", "www/home/css/", "www/home/dealer/"])
        # review fix C2: a master-side scss change that recompiles to
        # byte-identical CSS (the common case) stages nothing -- `git commit`
        # with an empty index is a hard failure, not a no-op. Check first and
        # skip the commit entirely rather than treating "nothing to commit"
        # as an error.
        staged = _run(["git", "-C", checkout, "diff", "--cached", "--quiet"],
                     run_subprocess, timeout=_GIT_TIMEOUT)
        if staged.returncode != 0:
            _git(run_subprocess, checkout,
                ["commit", "-m", "chore: compile CSS after master merge"])
            scss_recompiled = True

    head = _git(run_subprocess, checkout, ["rev-parse", "HEAD"]).strip()
    _git(run_subprocess, checkout, ["push", "origin", branch], timeout=_PUSH_TIMEOUT)

    # review fix I5: the branch-discovery fan-out probes EVERY configured
    # box, so it runs on the fast probe budget; only the one box that
    # actually matches gets the longer sync budget.
    probed = devslots.probe(list(boxes), run_ssh_probe)
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
                             dev_url=dev_url,
                             conflicts_resolved=conflicts_resolved)


_RESOLVE_PROMPT = """You are completing a `git merge origin/master` in this \
pb-www checkout. The merge stopped on conflicts in exactly these files: \
{files}.

Resolve them per the merge-master skill's Step 1b conflict classes \
(chandler-personal:merge-master is the canonical policy):

- www/home/php/templates/tab_bar_common_logic.php: the $script_version \
cache-buster. Resolve each conflict hunk by keeping OUR side (this branch's \
version bump). Edit the conflict markers in place -- NEVER `git checkout \
--ours` the whole file, because master-side changes elsewhere in the file \
must survive the merge.
- www/home/css/style.css(.map) and www/home/dealer/*/style.css(.map): \
compiled artifacts. Take master's: `git checkout --theirs -- <paths>`. They \
are regenerated from source when SCSS changed, by the caller, not by you.

Then `git add` the resolved files and complete the merge with \
`git commit --no-edit`. Do NOT push. Do NOT edit any file that is not in \
the list above. Do NOT run maintenance/compile-css."""


def _resolve_conflicts(run_subprocess: Callable, checkout: str, cfg,
                       files: List[str], pre: str) -> None:
    """Escalate a safe-class conflict set to a `claude -p` run in the
    worktree (the same unattended-claude pattern the implement executor
    uses). Any failure restores the worktree to `pre` before raising."""
    prompt = _RESOLVE_PROMPT.format(files=", ".join(files))
    try:
        proc = _run([cfg.claude_bin, "-p", prompt], run_subprocess,
                    cwd=checkout, timeout=_RESOLVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError(f"conflict resolver timed out after "
                          f"{_RESOLVE_TIMEOUT}s")
    if proc.returncode != 0:
        _restore(run_subprocess, checkout, pre)
        out = f"{proc.stderr or ''}{proc.stdout or ''}"
        raise RunnerError(f"conflict resolver failed: {_tail(out)}")


def _verify_resolution(run_subprocess: Callable, checkout: str,
                       pre: str) -> None:
    """Trust nothing the resolver did until proven: no unresolved files, the
    merge actually committed with origin/master as second parent, and a
    clean tree. Any failure restores `pre` -- nothing half-resolved may
    reach the push."""
    unresolved = _git(run_subprocess, checkout,
                      ["diff", "--name-only", "--diff-filter=U"],
                      allow_fail=True).strip()
    if unresolved:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError(f"resolver left unresolved conflicts: "
                          f"{', '.join(unresolved.split())}")
    merge_head = _run(["git", "-C", checkout, "rev-parse", "-q", "--verify",
                       "MERGE_HEAD"], run_subprocess, timeout=_GIT_TIMEOUT)
    if merge_head.returncode == 0:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver did not commit the merge")
    parent2 = _git(run_subprocess, checkout, ["rev-parse", "HEAD^2"],
                   allow_fail=True).strip()
    master = _git(run_subprocess, checkout, ["rev-parse", "origin/master"],
                  allow_fail=True).strip()
    if not parent2 or not master or parent2 != master:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver's HEAD is not a merge of origin/master")
    status = _git(run_subprocess, checkout, ["status", "--porcelain"],
                  allow_fail=True)
    if status.strip():
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver left a dirty tree")


def _restore(run_subprocess: Callable, checkout: str, pre: str) -> None:
    """Put the worktree back exactly where this run found it: abort any
    in-progress merge, then hard-reset to the pre-merge sha and clean."""
    merge_head = _run(["git", "-C", checkout, "rev-parse", "-q", "--verify",
                       "MERGE_HEAD"], run_subprocess, timeout=_GIT_TIMEOUT)
    if merge_head.returncode == 0:
        _run(["git", "-C", checkout, "merge", "--abort"], run_subprocess,
            timeout=_GIT_TIMEOUT)
    _run(["git", "-C", checkout, "reset", "-q", "--hard", pre],
        run_subprocess, timeout=_GIT_TIMEOUT)
    _run(["git", "-C", checkout, "clean", "-fdq"], run_subprocess,
        timeout=_GIT_TIMEOUT)


def _preflight_clean(run_subprocess: Callable, checkout: str) -> None:
    """review fix I4: make this reused worktree pristine before this run's
    fetch/checkout/merge. Two independent hazards, both left by a prior
    claim that didn't finish cleanly:

    1. A dangling `MERGE_HEAD` (the prior run timed out or crashed mid-merge)
       -- abort it, exactly as if this run had hit the conflict itself.
    2. A dirty tree (tracked-file changes or untracked cruft left by a
       crashed compile-css, or anything else) -- this is a mirror worktree,
       nothing of value lives in it outside what's already pushed, so the
       fix is an unconditional hard reset + clean.
    """
    merge_head = _run(["git", "-C", checkout, "rev-parse", "-q", "--verify",
                       "MERGE_HEAD"], run_subprocess, timeout=_GIT_TIMEOUT)
    if merge_head.returncode == 0:
        _run(["git", "-C", checkout, "merge", "--abort"], run_subprocess,
            timeout=_GIT_TIMEOUT)
    status = _git(run_subprocess, checkout, ["status", "--porcelain"],
                  allow_fail=True)
    if status.strip():
        _git(run_subprocess, checkout, ["reset", "-q", "--hard"])
        _git(run_subprocess, checkout, ["clean", "-fdq"])


def _merge_master(run_subprocess: Callable, checkout: str):
    """review fix I4: ANY exception out of the merge itself (a timeout, most
    commonly) must still abort before propagating -- otherwise MERGE_HEAD is
    left behind for the next run to trip over (that's what _preflight_clean
    guards against, but the fix belongs at the source too)."""
    try:
        return _run(["git", "-C", checkout, "merge", "origin/master",
                    "--no-edit"], run_subprocess, timeout=_GIT_TIMEOUT)
    except Exception:
        _run(["git", "-C", checkout, "merge", "--abort"], run_subprocess,
            timeout=_GIT_TIMEOUT)
        raise


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
