"""Per-issue dossier: the durable "issue expert" every lane reads and writes.

2026-09-09. Each lane used to start cold: the address-feedback round for
!4110 saw the MR description and the threads and nothing of WHY the implement
session chose what it chose, what the tribunal had already acknowledged, or
where the domain gate had been. The implement worktree's RESUME-HERE state
file held some of that, but it lived where only the implement lane looked and
died with the worktree.

The dossier is a small, machine-independent directory per issue:

    <issues_root>/<repo>/<iid>/dossier.md     append-only, one section per event
    <issues_root>/<repo>/<iid>/session.json   {lane, session_id, updated_at}
    <issues_root>/<repo>/by-mr/<mr_iid>       -> "<iid>" (the MR -> issue link)

* `record(...)` appends a titled section (implement outcome, feedback round,
  consult ruling). Every lane appends at its end.
* `read(...)` returns the dossier text, capped, for a prompt.
* `save_session` / `load_session` carry a claude session id between lanes so a
  later lane can `--resume` the same conversation (see feedback._claude). The
  transcript is history, not truth: the loader enforces an age cap and the
  resuming prompt tells the session to re-verify on disk before acting.

Nothing here raises into a lane: a dossier failure is printed and the lane
carries on cold, exactly as before the dossier existed.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Optional

DEFAULT_ROOT = os.path.expanduser("~/.worksweep/issues")
_READ_CAP = 12000
SESSION_MAX_AGE_DAYS = 7


def root(cfg) -> str:
    return (getattr(cfg, "issues_root", "") or "").strip() or DEFAULT_ROOT


def issue_dir(cfg, repo: str, iid: int) -> str:
    return os.path.join(root(cfg), repo, str(int(iid)))


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def record(cfg, repo: str, iid: int, title: str, body: str) -> bool:
    """Append `## <title> — <utc>` + body to the issue's dossier.md."""
    try:
        d = issue_dir(cfg, repo, iid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "dossier.md")
        fresh = not os.path.exists(path)
        with open(path, "a") as f:
            if fresh:
                f.write(f"# {repo} #{int(iid)} — dossier\n\n")
            f.write(f"## {title} — {_now()}\n\n{body.rstrip()}\n\n")
        return True
    except Exception as e:
        print(f"worksweep: dossier record failed for {repo}#{iid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def read(cfg, repo: str, iid: int, cap: int = _READ_CAP) -> str:
    """The dossier text, or "". Over `cap` bytes the HEAD is dropped, not the
    tail: the most recent sections are the ones a lane needs."""
    try:
        path = os.path.join(issue_dir(cfg, repo, iid), "dossier.md")
        with open(path) as f:
            text = f.read()
    except OSError:
        return ""
    except Exception as e:
        print(f"worksweep: dossier read failed for {repo}#{iid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return ""
    if len(text.encode("utf-8")) <= cap:
        return text
    data = text.encode("utf-8")[-cap:]
    trimmed = data.decode("utf-8", errors="ignore")
    cut = trimmed.find("\n## ")
    return "(earlier sections trimmed)\n" + (trimmed[cut + 1:] if cut >= 0 else trimmed)


def link_mr(cfg, repo: str, mr_iid: int, iid: int) -> bool:
    """Remember that MR !mr_iid implements issue #iid."""
    try:
        d = os.path.join(root(cfg), repo, "by-mr")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, str(int(mr_iid))), "w") as f:
            f.write(str(int(iid)))
        return True
    except Exception as e:
        print(f"worksweep: dossier link failed for {repo}!{mr_iid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def issue_for_mr(cfg, repo: str, mr_iid: int) -> Optional[int]:
    try:
        with open(os.path.join(root(cfg), repo, "by-mr", str(int(mr_iid)))) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def save_session(cfg, repo: str, iid: int, session_id: str, lane: str) -> bool:
    if not session_id:
        return False
    try:
        d = issue_dir(cfg, repo, iid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w") as f:
            json.dump({"lane": lane, "session_id": session_id,
                       "updated_at": _now()}, f)
        return True
    except Exception as e:
        print(f"worksweep: dossier session save failed for {repo}#{iid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def load_session(cfg, repo: str, iid: int, now: Optional[str] = None,
                 max_age_days: int = SESSION_MAX_AGE_DAYS) -> Optional[str]:
    """The last lane's session id, or None when absent, unparseable, or older
    than `max_age_days` (a week-old transcript remembers a master and a review
    state that no longer exist)."""
    try:
        with open(os.path.join(issue_dir(cfg, repo, iid), "session.json")) as f:
            data = json.load(f)
        sid = str(data.get("session_id") or "")
        stamp = str(data.get("updated_at") or "")
        if not sid or not stamp:
            return None
        then = datetime.datetime.fromisoformat(stamp)
        ref = (datetime.datetime.fromisoformat(now) if now
               else datetime.datetime.now(datetime.timezone.utc))
        if then.tzinfo is None:
            then = then.replace(tzinfo=datetime.timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=datetime.timezone.utc)
        if (ref - then).total_seconds() > max_age_days * 86400:
            return None
        return sid
    except Exception:
        return None


def prompt_block(text: str) -> str:
    """The dossier as a prompt section. Explicit about what it is: prior
    context to CHECK against the code and the MR, never a source of truth."""
    if not text.strip():
        return ""
    return ("ISSUE DOSSIER (prior lanes' notes on this issue — decisions, "
            "tribunal dispositions, reviewer rounds). It is history, not "
            "truth: master, the branch and the threads may have moved since "
            "it was written. Use it to understand WHY things are the way they "
            "are; verify anything you act on against the code and the MR.\n\n"
            + text.rstrip() + "\n\n")


RESUME_PREAMBLE = (
    "You are being RESUMED from an earlier session on this same issue. Your "
    "transcript is history, not truth: the branch, master and the review "
    "threads may all have moved since. Before acting on anything you "
    "remember, re-verify it on disk and against the MR. Then carry out the "
    "instructions below exactly as written.\n\n")


def iid_for_item(cfg, item) -> Optional[int]:
    """The ISSUE a queue item belongs to: an issue-keyed item's own iid, or
    the issue an MR-keyed item's MR was linked to by the implement lane.
    None when unknown -- the lane then runs cold, as before."""
    try:
        url = str(getattr(item, "web_url", "") or "")
        m = re.search(r"/(?:issues|work_items)/(\d+)", url)
        if m:
            return int(m.group(1))
        m = re.search(r"/merge_requests/(\d+)", url)
        if m:
            return issue_for_mr(cfg, getattr(item, "repo", ""), int(m.group(1)))
    except Exception:
        return None
    return None
