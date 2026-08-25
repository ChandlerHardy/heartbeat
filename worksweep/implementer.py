"""M4 Task G: the `implement` executor — the only Worksweep path that WRITES.

One approved assigned issue in, one Draft MR out:

    slot pick -> branch -> `claude -p "/rubric:do #<iid>"` -> verify commits
    -> push -> sync the claimed dev box -> Draft MR (with the dev URL)
    -> `/magi:magi-review !<mr> --advisory --draft-findings` (advisory + pending drafts, never auto-fixes)

Everything that touches the world is injected (`run_subprocess`, `run_ssh`,
`http_get`) so the test suite never shells out, never sshs, and never opens a
socket. The module's contract with the runner is deliberately narrow:

* `RunnerError`  -> the item goes `error`, ⚠️ posted, re-proposed next sweep.
* `NeedsInputError` -> the item goes `needs-input`, ❓ posted with the question,
  and stays parked until Chandler's next ✅.
* a returned `ImplementResult` -> the item goes `done` and the 🛠️ post names
  the MR, dev URL, magi verdict and branch.

There is no fourth outcome: every failure path below ends in one of the two
exceptions, so the runner can never fall through to silence.

Ordering matters and is load-bearing: the box sync runs BEFORE `glab mr
create`, so a dead/unhealthy dev box fails the run without leaving a dangling
Draft MR behind; and the MR is opened as a draft (verified, not assumed) so
nothing this executor produces can be merged by accident.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace as _dc_replace
from typing import Callable, FrozenSet, List, Optional, Sequence

from . import checkouts, devslots
from .devslots import DevBox
from .models import MergeRequest, WorkItem
from .runner import NeedsInputError, RunnerError, extract_verdict, find_report

# `/rubric:do` (via the plan-author agent) stops rather than guessing; these
# are the shapes it stops with. A halt is NOT a failure — it is a question.
HALT_MARKERS = ("HALT_INSUFFICIENT_CONTEXT", "HALT_SPEC_AMBIGUITY")
_QUESTION_PREFIX = "QUESTION:"
HALT_EXCERPT_MAX = 700
_HALT_EXCERPT_LINES = 8

_TIER_FREE = "free"
_TIER_HANDED_OFF = "handed_off"

_SLUG_WORDS = 5
_FETCH_TIMEOUT = 120
_GIT_TIMEOUT = 120
_PUSH_TIMEOUT = 300
_GLAB_TIMEOUT = 180
_DESC_TIMEOUT = 60
_MAGI_TIMEOUT = 1800
_TAIL_LINES = 15


@dataclass(frozen=True)
class ImplementResult:
    iid: int              # the issue that was implemented
    mr_iid: int           # the Draft MR opened for it
    mr_url: str
    dev_url: str          # the box the branch is served from
    dev_box: str
    branch: str
    report_path: str      # "" = magi produced no tribunal report (not fatal)
    verdict: str          # "" when there is no report
    result_sha: str       # local HEAD that was pushed + synced
    reassigned_from: str = ""   # old MR iid when a handed-off box was reclaimed
    magi_note: str = ""         # non-fatal magi trouble, surfaced in the post


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def slug_of(title: str) -> str:
    """Kebab of the issue title's first 5 words, restricted to [a-z0-9-].

    The restriction is not cosmetic: the slug becomes a git branch name that
    is interpolated into a remote shell command in `sync_to_box`, so it must
    not be able to carry quotes, spaces, or shell metacharacters.
    """
    words = re.findall(r"[A-Za-z0-9]+", title or "")[:_SLUG_WORDS]
    slug = "-".join(w.lower() for w in words)
    return slug or "issue"


def branch_name(iid: int, title: str) -> str:
    return f"feat/{iid}-{slug_of(title)}"


def issue_iid(item: WorkItem) -> int:
    """Issue iid from the item id (`issue:<repo>#<iid>`), falling back to the
    web_url. Raises RunnerError rather than guessing — a wrong iid would run
    `/rubric:do` against someone else's issue."""
    m = re.search(r"#(\d+)$", item.id or "")
    if m:
        return int(m.group(1))
    m = re.search(r"/issues/(\d+)", item.web_url or "")
    if m:
        return int(m.group(1))
    raise RunnerError(f"cannot find issue iid in {item.id!r} / {item.web_url!r}")


def detect_halt(text: str) -> Optional[str]:
    """The excerpt to show the human when `/do` stopped to ask a question,
    or None when it didn't. Bounded so a runaway transcript can't blow past
    Discord's message limit."""
    if not text:
        return None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(m in line for m in HALT_MARKERS):
            return _excerpt(lines, i)
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_QUESTION_PREFIX):
            return _excerpt(lines, i)
    return None


def _excerpt(lines: List[str], start: int) -> str:
    return "\n".join(lines[start:start + _HALT_EXCERPT_LINES]
                     ).strip()[:HALT_EXCERPT_MAX]


def annotate_boxes(boxes: Sequence[DevBox], all_mrs: List[MergeRequest],
                   username: str,
                   claimed: FrozenSet[str] = frozenset()) -> List[DevBox]:
    """Pure: attach `tier` (devslots.classify) and `mr_iid` (the open MR
    sitting on the box's branch, 0 if none) to each box, preserving config
    order so slot selection stays deterministic."""
    tiers = devslots.classify(list(boxes), all_mrs, username, claimed=claimed)
    by_branch = devslots.mr_by_branch(all_mrs)
    out = []
    for b in boxes:
        mr = by_branch.get(b.branch or "")
        out.append(_dc_replace(b, tier=tiers.get(b.name, "live"),
                               mr_iid=mr.iid if mr else 0))
    return out


def select_slot(boxes: Sequence[DevBox]) -> Optional[DevBox]:
    """First `free` box, else first `handed_off` box, else None — in the
    order given (config order). Mirrors devslots.pick over annotated boxes."""
    for tier in (_TIER_FREE, _TIER_HANDED_OFF):
        for b in boxes:
            if b.tier == tier:
                return b
    return None


# ---------------------------------------------------------------------------
# edges
# ---------------------------------------------------------------------------

def sync_to_box(box: DevBox, branch: str, run_ssh: Callable[[str, str], str],
                http_get: Callable[[str], int], expected_sha: str = "",
                claim_branch: Optional[str] = None,
                claim_sha: Optional[str] = None) -> str:
    """Put `branch` on the dev box and PROVE it landed.

    Ported from ~/bin/git-push-sync (hardened 2026-08-17): `checkout -B` from
    `origin/<branch>` is deterministic whatever the box had checked out, where
    a plain `git pull` merges into the box's CURRENT branch — which is how dev
    boxes ended up on the wrong branch while the script still said "Done".
    Verification is the point of this function: sha equality plus an HTTP 200,
    or RunnerError. Never returns on a half-landed sync.

    Two safety rules the shell script does not have, because this runs
    unattended against boxes other people use:

    1. **Drift check.** The box is re-probed and must still be on the exact
       branch+sha it was classified `free`/`handed_off` on (`claim_branch`/
       `claim_sha`, defaulting to what `devslots.probe` saw). Up to 90 minutes
       pass between the claim and this call; if a human checked something out
       in the meantime the box is theirs, and we abort instead of yanking it.
    2. **Only drop OUR stash.** `git stash -u` is a no-op on a clean tree, so
       an unconditional `git stash drop` would delete whatever stash@{0}
       already belonged to whoever uses that box. The drop is guarded by a
       before/after stash count.
    """
    expect_branch = box.branch if claim_branch is None else claim_branch
    expect_sha = box.sha if claim_sha is None else claim_sha
    if expect_branch:
        try:
            raw = run_ssh(box.host, f"cd '{box.path}' && git branch "
                                    f"--show-current && git rev-parse HEAD")
        except Exception as e:
            raise RunnerError(f"could not re-probe {box.name} ({box.host}) "
                              f"before sync: {e}")
        probe = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
        now_branch = probe[0] if probe else ""
        now_sha = probe[1] if len(probe) > 1 else ""
        drifted = (now_branch != expect_branch
                   or (expect_sha and now_sha and now_sha != expect_sha))
        if drifted:
            raise RunnerError(
                f"{box.name} moved to {now_branch or '(unknown)'}@"
                f"{(now_sha or '?')[:8]} since it was claimed (expected "
                f"{expect_branch}@{(expect_sha or '?')[:8]}) — someone else "
                f"is using it; not taking it over")

    cmd = ("set -e\n"
           f"cd '{box.path}'\n"
           "git merge --abort 2>/dev/null || true\n"
           "stash_before=$(git stash list | wc -l)\n"
           "git stash -u >/dev/null 2>&1 || true\n"
           "stash_after=$(git stash list | wc -l)\n"
           f"git fetch origin '{branch}'\n"
           f"git checkout -q -B '{branch}' 'origin/{branch}'\n"
           # Drop ONLY a stash this run created — never a pre-existing one.
           "if [ \"$stash_after\" -gt \"$stash_before\" ]; then "
           "git stash drop >/dev/null 2>&1 || true; fi\n"
           "git rev-parse HEAD")
    try:
        raw = run_ssh(box.host, cmd)
    except Exception as e:
        raise RunnerError(f"sync to {box.name} ({box.host}) failed: {e}")
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    remote_sha = lines[-1] if lines else ""
    if expected_sha and remote_sha != expected_sha:
        raise RunnerError(
            f"{box.name} HEAD {remote_sha or '(unknown)'} != pushed "
            f"{expected_sha} — sync did NOT land")
    if box.url:
        try:
            code = http_get(box.url)
        except Exception as e:
            raise RunnerError(f"{box.name} health check ({box.url}) failed: {e}")
        if int(code) != 200:
            raise RunnerError(f"{box.name} returned HTTP {code} after sync "
                              f"({box.url}) — check the box's php error log")
    return remote_sha


def build_description(checkout: str, cfg, iid: int, title: str, dev_url: str,
                      branch: str, log: str,
                      run_subprocess: Callable = subprocess.run) -> str:
    """An honest ~10-line MR body from the real commits + diffstat, written by
    `claude -p`. Degrades to a deterministic body on ANY LLM trouble — a
    missing description must never be the reason a finished branch has no MR.
    The `Available on <dev_url>` line is enforced here, not trusted to the LLM
    (it is Chandler's MR convention and the reviewer's entry point)."""
    stat = _git(run_subprocess, checkout, ["diff", "--stat", "origin/master..HEAD"],
                allow_fail=True)
    prompt = (
        f"Write the merge request description for issue #{iid} ({title}).\n"
        f"Base it ONLY on the evidence below — do not invent behaviour, do "
        f"not claim tests or verification that are not visible here.\n"
        f"About 10 lines: what changed and why, then anything a reviewer "
        f"should look at first. Plain markdown, no preamble, no headings "
        f"above level 2.\n"
        f"The LAST line must be exactly: Available on {dev_url}\n\n"
        f"Commits (git log origin/master..HEAD):\n{log}\n\n"
        f"Diffstat (git diff --stat origin/master..HEAD):\n{stat}\n")
    body = ""
    try:
        proc = _run([cfg.claude_bin, "-p", prompt], run_subprocess,
                    cwd=checkout, timeout=_DESC_TIMEOUT)
        if proc.returncode == 0:
            body = (proc.stdout or "").strip()
    except Exception as e:                      # timeout, missing binary, ...
        body = ""
        print(f"worksweep: MR description LLM pass failed: {e}")
    if not body:
        body = (f"Draft MR for #{iid} — {title}\n\n"
                f"Branch `{branch}`, opened by the Worksweep implement "
                f"executor. Description auto-generated (the LLM pass was "
                f"unavailable) — read the diff, not this text.\n\n"
                f"Commits:\n```\n{log.strip()}\n```\n\n"
                f"Diffstat:\n```\n{stat.strip()}\n```")
    if dev_url and f"Available on {dev_url}" not in body:
        body = f"{body}\n\nAvailable on {dev_url}"
    return body


def open_draft_mr(checkout: str, iid: int, title: str, description: str,
                  branch: str, run_subprocess: Callable = subprocess.run,
                  target_branch: str = "master") -> tuple:
    """`glab mr create --draft --yes ...` -> (mr_iid, mr_url).

    glab renders the draft state as the `Draft: ` title prefix (GitLab has no
    separate draft flag), so the created MR reads `Draft: feat(#<iid>):
    <title>`. That is verified, not assumed: if the MR somehow came back
    non-draft we mark it draft, and if that fails we raise — an executor-
    authored MR that is open for merge is exactly the failure this v1 ceiling
    exists to prevent.
    """
    cmd = ["glab", "mr", "create", "--draft", "--yes",
           "--source-branch", branch, "--target-branch", target_branch,
           "--title", f"feat(#{iid}): {title}",
           "--description", description]
    try:
        proc = _run(cmd, run_subprocess, cwd=checkout, timeout=_GLAB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"glab mr create timed out after {_GLAB_TIMEOUT}s")
    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0:
        raise RunnerError(f"glab mr create exited {proc.returncode}: "
                          f"{_tail(out)}")
    # glab prints `!42 <url>`, but the exact line has changed between
    # versions — accept either form and only give up when BOTH miss, since
    # by this point the MR really does exist and losing its iid means losing
    # the tribunal run and the record.
    url_m = re.search(r"https?://\S*/-/merge_requests/(\d+)", out)
    bang_m = re.search(r"!(\d+)", out)
    if bang_m:
        mr_iid = int(bang_m.group(1))
    elif url_m:
        mr_iid = int(url_m.group(1))
    else:
        raise RunnerError(f"glab mr create: could not parse MR iid from "
                          f"output: {_tail(out)}")
    mr_url = url_m.group(0) if url_m else ""
    _ensure_draft(checkout, mr_iid, run_subprocess, mr_url)
    return mr_iid, mr_url


def _ensure_draft(checkout: str, mr_iid: int, run_subprocess: Callable,
                  mr_url: str = "") -> None:
    draft = _is_draft(checkout, mr_iid, run_subprocess)
    if draft is True:
        return
    # Unknown (view/parse failed) or explicitly not draft -> force it.
    try:
        proc = _run(["glab", "mr", "update", str(mr_iid), "--draft", "--yes"],
                    run_subprocess, cwd=checkout, timeout=_GLAB_TIMEOUT)
        ok = proc.returncode == 0
        err = _tail(f"{proc.stdout or ''}\n{proc.stderr or ''}")
    except Exception as e:
        ok, err = False, str(e)
    # `glab mr update --draft` exiting 0 is not proof: read the state back.
    if ok and _is_draft(checkout, mr_iid, run_subprocess) is not True:
        ok, err = False, "update exited 0 but the MR still reads non-draft"
    if not ok:
        raise RunnerError(f"!{mr_iid} is not a draft and could not be marked "
                          f"draft ({err}) — mark it draft by hand before "
                          f"anyone merges it"
                          + (f": {mr_url}" if mr_url else ""))


def _is_draft(checkout: str, mr_iid: int,
              run_subprocess: Callable) -> Optional[bool]:
    """True/False, or None when glab/JSON didn't answer (caller forces draft)."""
    try:
        proc = _run(["glab", "mr", "view", str(mr_iid), "-F", "json"],
                    run_subprocess, cwd=checkout, timeout=_GLAB_TIMEOUT)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("draft", "work_in_progress"):
        if key in data:
            return bool(data[key])
    title = data.get("title")
    if isinstance(title, str):
        return title.strip().lower().startswith("draft:")
    return None


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def execute(item: WorkItem, cfg, boxes: Sequence[DevBox],
            run_subprocess: Callable = subprocess.run,
            run_ssh: Callable[[str, str], str] = None,
            http_get: Callable[[str], int] = None) -> ImplementResult:
    """Implement one approved assigned issue. See the module docstring for the
    three-outcome contract.

    Runs in a DEDICATED git worktree (see checkouts.worktree_for), not the
    shared `<checkouts_root>/<repo>` clone -- a keep-current claim's
    `checkout -B` in that same shared clone could otherwise switch the
    branch out from under this run's live `/rubric:do` (review fix C1,
    2026-08-18)."""
    checkout = checkouts.worktree_for(cfg, item.repo, "implement", run_subprocess)
    if run_ssh is None or http_get is None:
        raise RunnerError("implement executor wired without an ssh/http edge")
    iid = issue_iid(item)
    slot = select_slot(boxes)
    if slot is None:
        raise RunnerError("no dev slot available — free one or reclaim")
    if getattr(cfg, "pipeline_command", ""):
        return _execute_pipeline(cfg, iid, slot, checkout,
                                 run_subprocess, http_get)
    branch = branch_name(iid, item.title or "")

    _git(run_subprocess, checkout, ["fetch", "origin"], timeout=_FETCH_TIMEOUT)
    remote = _git(run_subprocess, checkout,
                  ["ls-remote", "--heads", "origin", branch], allow_fail=True)
    if remote.strip():
        # Reuse the existing branch (a re-run after a halt/fix) — never reset
        # it onto master, that would discard the human's or a prior run's work.
        _git(run_subprocess, checkout, ["checkout", branch])
        _git(run_subprocess, checkout, ["pull", "--ff-only", "origin", branch])
    else:
        _git(run_subprocess, checkout, ["checkout", "-B", branch, "origin/master"])

    # --- the long pole: full Ferdinand ceremony via /rubric:do -------------
    try:
        proc = _run([cfg.claude_bin, "-p", f"/rubric:do #{iid}"],
                    run_subprocess, cwd=checkout,
                    timeout=cfg.implement_timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"/rubric:do #{iid} exceeded "
                          f"{cfg.implement_timeout}s")
    transcript = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    # Halt check comes FIRST: a halting /do may well exit non-zero, and a
    # question must never be reported to the human as a crash.
    halt = detect_halt(transcript)
    if halt:
        raise NeedsInputError(halt)
    if proc.returncode != 0:
        raise RunnerError(f"/rubric:do #{iid} exited {proc.returncode}: "
                          f"{_tail(transcript)}")

    log = _git(run_subprocess, checkout, ["log", "--oneline",
                                          "origin/master..HEAD"])
    if not log.strip():
        raise RunnerError(f"/rubric:do #{iid} produced no commits on {branch}")
    dirty = _git(run_subprocess, checkout, ["status", "--porcelain"])
    if dirty.strip():
        raise RunnerError(f"uncommitted changes left on {branch}: "
                          f"{_tail(dirty, 5)}")
    head = _git(run_subprocess, checkout, ["rev-parse", "HEAD"]).strip()

    _git(run_subprocess, checkout, ["push", "-u", "origin", branch],
         timeout=_PUSH_TIMEOUT)
    # Sync BEFORE the MR: a box that won't serve the branch means no MR at all,
    # rather than a Draft MR advertising a dev URL that 502s.
    # claim_branch/claim_sha are what devslots.probe saw when this box was
    # classified free/handed_off — sync_to_box refuses the box if it moved.
    sync_to_box(slot, branch, run_ssh, http_get, expected_sha=head,
                claim_branch=slot.branch, claim_sha=slot.sha)

    description = build_description(checkout, cfg, iid, item.title or "",
                                    slot.url, branch, log, run_subprocess)
    mr_iid, mr_url = open_draft_mr(checkout, iid, item.title or "", description,
                                   branch, run_subprocess)

    report_path, verdict, magi_note = _magi_advisory(checkout, cfg, mr_iid,
                                                     run_subprocess)
    return ImplementResult(
        iid=iid, mr_iid=mr_iid, mr_url=mr_url, dev_url=slot.url,
        dev_box=slot.name, branch=branch, report_path=report_path,
        verdict=verdict, result_sha=head,
        reassigned_from=(str(slot.mr_iid)
                         if slot.tier == _TIER_HANDED_OFF and slot.mr_iid else ""),
        magi_note=magi_note)


_PIPELINE_CONSTRAINTS = """
Unattended seneschal run — hard constraints on top of the skill:
- FULL MAGI: never pass --lite; let magi-core's auto-gate pick panel mode. \
Less human oversight requires more review, never less.
- The MR must be created as a Draft and stay a Draft.
- Chandler is away: never stop for input — record decisions per the skill \
and keep going.
- git-push-sync reads .vscode/sftp.json from the repo root; if the worktree \
lacks one, create it: host chandlerhardy-dev.aws0.pla-net.cc, protocol \
sftp, port 22, username chandlerhardy, openSsh true, remotePath \
/home/chandlerhardy/{box}.chandlerhardy-dev/pb-www."""


def _execute_pipeline(cfg, iid: int, slot: DevBox, checkout: str,
                      run_subprocess: Callable,
                      http_get: Callable[[str], int]) -> ImplementResult:
    """M5: one claude run drives cfg.pipeline_command (the full pla-pipeline)
    end-to-end; this executor shrinks to claim -> run -> PROVE. The pipeline
    itself implements, ship-gates, runs the full magi fix loop, parks on the
    claimed box, and opens the Draft MR — so this path must never create an
    MR, run magi, or push. It verifies instead: the pipeline's state file
    names an MR, that MR reads back as (or is forced) Draft, and the box
    serves 200."""
    devnum = re.sub(r"\D", "", slot.name) or slot.name
    prompt = (f"{cfg.pipeline_command} #{iid} --dev {devnum}"
              + _PIPELINE_CONSTRAINTS.format(box=slot.name))
    try:
        proc = _run([cfg.claude_bin, "-p", prompt], run_subprocess,
                    cwd=checkout, timeout=cfg.implement_timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"{cfg.pipeline_command} #{iid} exceeded "
                          f"{cfg.implement_timeout}s")
    transcript = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    halt = detect_halt(transcript)
    if halt:
        raise NeedsInputError(halt)
    if proc.returncode != 0:
        raise RunnerError(f"{cfg.pipeline_command} #{iid} exited "
                          f"{proc.returncode}: {_tail(transcript)}")

    state_path, state = _find_pipeline_state(checkout, iid)
    if state is None:
        raise RunnerError(f"pipeline run for #{iid} left no state file under "
                          f".claude/state/pla-pipelines/ — cannot prove an MR "
                          f"exists; inspect the checkout by hand")
    url_m = re.search(r"https?://\S*/-/merge_requests/(\d+)", state)
    bang_m = re.search(r"MR[^\n]*?!(\d+)", state)
    if url_m:
        mr_iid, mr_url = int(url_m.group(1)), url_m.group(0)
    elif bang_m:
        mr_iid, mr_url = int(bang_m.group(1)), ""
    else:
        raise RunnerError(f"pipeline state for #{iid} names no MR "
                          f"({state_path}) — the run did not reach Phase 7")
    _ensure_draft(checkout, mr_iid, run_subprocess, mr_url)
    branch, head = _mr_branch_sha(checkout, mr_iid, run_subprocess)

    note = ""
    try:
        status = http_get(slot.url)
    except Exception as e:
        status, note = 0, f"dev-site probe failed: {e}"
    if status != 200 and not note:
        note = f"dev site {slot.url} returned {status} after pipeline QA"

    magi_line = next((ln.strip() for ln in state.splitlines()
                      if "MAGI" in ln.upper() and ("[x]" in ln or "SHIP" in ln)),
                     "")
    verdict = "SHIP" if re.search(r"SHIP|RESOLVED|review-clean", magi_line) else ""
    return ImplementResult(
        iid=iid, mr_iid=mr_iid, mr_url=mr_url, dev_url=slot.url,
        dev_box=slot.name, branch=branch, report_path=state_path,
        verdict=verdict, result_sha=head,
        reassigned_from=(str(slot.mr_iid)
                         if slot.tier == _TIER_HANDED_OFF and slot.mr_iid else ""),
        magi_note=note or magi_line)


def _find_pipeline_state(checkout: str, iid: int) -> tuple:
    """(path, contents) of the pipeline's state.md for issue `iid` -- state
    dirs are slugged `<iid>-<words>` (pla-pipeline skill convention). ("",
    None) when absent."""
    root = os.path.join(checkout, ".claude", "state", "pla-pipelines")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return "", None
    for name in names:
        if re.match(rf"{iid}(\D|$)", name):
            path = os.path.join(root, name, "state.md")
            try:
                with open(path) as f:
                    return path, f.read()
            except OSError:
                continue
    return "", None


def _mr_branch_sha(checkout: str, mr_iid: int,
                   run_subprocess: Callable) -> tuple:
    """(source_branch, head_sha) read back from the MR; ("", "") on any
    failure -- display fields only, never worth failing a finished run."""
    try:
        proc = _run(["glab", "mr", "view", str(mr_iid), "-F", "json"],
                    run_subprocess, cwd=checkout, timeout=_GLAB_TIMEOUT)
        data = json.loads(proc.stdout or "{}")
        return (str(data.get("source_branch") or ""),
                str(data.get("sha") or ""))
    except Exception:
        return "", ""


def _magi_advisory(checkout: str, cfg, mr_iid: int,
                   run_subprocess: Callable) -> tuple:
    """Advisory tribunal on the Draft MR. `--advisory` is mandatory: Chandler
    authors these MRs, and without it magi-review would run its full auto-fix
    loop on his behalf. NOTHING here is fatal — the MR already exists and is
    the deliverable; a missing verdict is a note in the post, not a failed
    item (which would strand a real Draft MR under an `error` record)."""
    note = ""
    try:
        proc = _run(
            [cfg.claude_bin, "-p", f"/magi:magi-review !{mr_iid} --advisory --draft-findings"],
            run_subprocess, cwd=checkout, timeout=_MAGI_TIMEOUT)
        if proc.returncode != 0:
            note = (f"magi-review !{mr_iid} exited {proc.returncode}: "
                    f"{_tail(f'{proc.stdout or ''}{proc.stderr or ''}', 5)}")
    except Exception as e:
        note = f"magi-review !{mr_iid} could not run: {type(e).__name__}: {e}"
    report = find_report(checkout, mr_iid)
    if report is None:
        return "", "", note or f"magi-review !{mr_iid} produced no report"
    return report, extract_verdict(report), note


def _run(cmd: List[str], run_subprocess: Callable, **kw):
    """The ONLY way this module spawns a process.

    `stdin=subprocess.DEVNULL` is non-negotiable and is why this helper
    exists: under launchd there is no TTY, and an inherited stdin makes
    `claude -p` exit 1 after ~3s and makes `glab` sit on an interactive
    prompt for the whole timeout (fixed once already in c0e7791 — this
    module must not reintroduce it call by call).
    """
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
        raise RunnerError(f"git {' '.join(args)} failed: "
                          f"{_tail(f'{proc.stderr or ''}{proc.stdout or ''}', 5)}")
    return proc.stdout or ""


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])
