"""The `park` executor: put an authored MR's branch on a dev box and link it.

Chandler's MR convention is that every non-draft MR carries a dev-server link
so a reviewer has somewhere to click. Worksweep already NOTICED when that link
was missing (the `hygiene-devurl` item) but could only nag about it -- the row
sat on the dashboard as inert "manual" work forever. This executor makes it
actionable: claim a free dev box, put the branch on it, prove it serves, and
prepend the header line to the MR description.

Deliberately NOT auto-approved. Parking overwrites whatever a dev box was
serving, so Chandler decides which MR takes a slot -- the item is a normal
`proposed` row, approvable from the dashboard checkbox or a Discord ✅ like any
other.

Every edge is injected (ssh, http, glab), matching keepcurrent/implementer:
this module never shells out or reaches the network on its own, so the tests
never do either.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from . import implementer
from .keepcurrent import iid_of
from .models import WorkItem, dev_urls, has_dev_url, same_dev_url
from .runner import RunnerError

# The exact header Chandler's MRs carry. Masked link so the URL renders as a
# clickable title in GitLab rather than a bare string.
_HEADER = "### Available on [{url}]({url})"


@dataclass(frozen=True)
class ParkResult:
    iid: int                        # the authored MR that got parked
    box_name: str                   # the dev box now serving the branch
    dev_url: str                    # that box's url (health-checked 200)
    result_sha: str = ""            # branch HEAD as it landed on the box
    description_updated: bool = False   # False = a dev link was already there
    http_status: int = 0            # what the box ACTUALLY answered (f-027)
    moved_from: str = ""            # a dev link this park retargeted (f-024)


def header_line(dev_url: str) -> str:
    return _HEADER.format(url=dev_url)


def prepend_header(description: str, dev_url: str) -> Optional[str]:
    """The MR description with the header prepended, or None to leave it alone.

    None when the description ALREADY carries a dev-server link: re-adding one
    would stack duplicate headers on every re-park, and a human may have put
    the link somewhere deliberate. Uses the same detector that decided the MR
    needed parking in the first place, so the two can never disagree.
    """
    if has_dev_url(description):
        return None
    body = (description or "").strip()
    line = header_line(dev_url)
    return f"{line}\n\n{body}" if body else line


def retarget_header(description: str, dev_url: str):
    """The description this park should write, or None to leave it alone.

    Three cases, and the middle one is f-024. `has_dev_url` matches ANY dev
    host, so parking on dev2 while the description named dev5 skipped the PUT
    and reported dev2 in Discord -- leaving the MR advertising a box that no
    longer serves the branch, which is worse than having no link at all.

    * no dev link          -> prepend the header
    * a link to THIS box   -> None (a re-park is a no-op; rewriting every
                              sweep would churn the description for nothing)
    * a link to ANOTHER box -> retarget every dev link in place, so the
                              surrounding markdown and the rest of the
                              description survive
    """
    existing = dev_urls(description)
    if not existing:
        return prepend_header(description, dev_url)
    if any(same_dev_url(u, dev_url) for u in existing):
        return None
    out = description or ""
    for old_url in dict.fromkeys(existing):
        out = out.replace(old_url, dev_url.rstrip("/"))
    return out


def _mr_path(repo: str, iid: int) -> str:
    from .collectors import _project        # local: collectors is a heavier import
    return f"projects/{_project(repo)}/merge_requests/{int(iid)}"


def fetch_description(run_glab: Callable, repo: str, iid: int) -> str:
    return fetch_mr(run_glab, repo, iid)[0]


def fetch_mr(run_glab: Callable, repo: str, iid: int) -> tuple:
    """(description, head_sha) for one MR.

    The sha is what makes park's sync verifiable (f-026): without a
    Python-owned expected value, `sync_to_box`'s sha gate is skipped and
    "the branch landed" is an assumption rather than a check.
    """
    raw = run_glab(["api", _mr_path(repo, iid)])
    try:
        data = json.loads(raw) or {}
        return (data.get("description") or "", str(data.get("sha") or ""))
    except (ValueError, AttributeError) as e:
        raise RunnerError(f"could not read !{iid}'s description: {e}")


def put_description(run_glab: Callable, repo: str, iid: int,
                    description: str) -> None:
    """PUT the new description as a JSON BODY, never as -f fields.

    `glab api --field/--raw-field` does not parse JSON and sends everything as
    a string ("Neither --field nor --raw-field parses JSON arrays or objects"),
    which is the 2026-08 array bug: a description containing newlines, quotes
    or bracketed markdown gets mangled. `--input -` sends the body verbatim.
    """
    run_glab(["api", _mr_path(repo, iid), "-X", "PUT",
              "-H", "Content-Type: application/json", "--input", "-"],
             body=json.dumps({"description": description}))


def execute(item: WorkItem, cfg, boxes: Sequence,
            run_ssh: Callable[[str, str], str] = None,
            http_get: Callable[[str], int] = None,
            run_glab: Callable = None) -> ParkResult:
    """Park one MR's branch on a free dev box and link it from the description.

    Order matters: the description is only touched AFTER the box is proven to
    serve HTTP 200, so a failed sync can never leave the MR advertising a dev
    URL that shows an error page.
    """
    if run_ssh is None or http_get is None or run_glab is None:
        raise RunnerError("park executor is wired without an ssh/http/glab edge")
    iid = iid_of(item)
    branch = item.branch
    if not branch:
        raise RunnerError(f"no source branch recorded for !{iid} "
                          f"(WorkItem.branch was not set by the assessor)")

    slot = implementer.select_slot(boxes)
    if slot is None:
        raise RunnerError(
            f"no free dev slot to park !{iid} on — free one or reclaim a box, "
            f"then re-approve (this item re-proposes itself next sweep)")

    # Read the MR BEFORE the sync: its head sha is the only Python-owned value
    # park can hold sync_to_box to. Reading early is safe -- the description is
    # still only WRITTEN after the box is proven (see the docstring).
    description, mr_sha = fetch_mr(run_glab, item.repo, iid)

    # Syncs, checks for drift, and health-checks the box. f-026: the sha gate
    # inside sync_to_box is conditional on expected_sha, so omitting these
    # arguments skipped the verification this comment claimed. claim_* are what
    # the probe just saw, so the box moving between probe and sync is refused.
    result_sha = implementer.sync_to_box(
        slot, branch, run_ssh, http_get, expected_sha=mr_sha,
        claim_branch=slot.branch, claim_sha=slot.sha)

    # f-027: the done message used to hardcode "(200)". Measure it instead --
    # this repeats sync_to_box's own health check, which is one cheap GET for
    # the difference between reporting a number and inventing one.
    try:
        status = int(http_get(slot.url))
    except Exception as e:
        raise RunnerError(f"{slot.name} health check ({slot.url}) failed "
                          f"after sync: {type(e).__name__}: {e}")
    if status != 200:
        raise RunnerError(f"{slot.name} returned HTTP {status} after sync "
                          f"({slot.url}) — not advertising a broken dev link")

    updated, moved_from = False, ""
    existing = dev_urls(description)
    new_description = retarget_header(description, slot.url)
    if new_description is not None:
        if existing:
            moved_from = existing[0]
        put_description(run_glab, item.repo, iid, new_description)
        # f-027: GitLab accepting the PUT is not the same as the description
        # changing. Read it back, exactly as the feedback executor does.
        after, _ = fetch_mr(run_glab, item.repo, iid)
        if not any(same_dev_url(u, slot.url) for u in dev_urls(after)):
            raise RunnerError(
                f"the dev link for !{iid} did not stick — read back after the "
                f"PUT, {slot.url} is still not in the description")
        updated = True
    return ParkResult(iid=iid, box_name=slot.name, dev_url=slot.url,
                      result_sha=result_sha, description_updated=updated,
                      http_status=status, moved_from=moved_from)


def done_message(result: ParkResult) -> str:
    if result.moved_from:
        # f-024: say so. A silent retarget looks identical to a no-op, and the
        # reviewer following the old link is the person who finds out.
        tail = f"dev link moved from {result.moved_from}"
    elif result.description_updated:
        tail = "description updated"
    else:
        tail = "description already had this dev link"
    return (f"🅿️ !{result.iid} parked on {result.box_name} "
            f"({result.http_status}) · {tail}\n<{result.dev_url}>")
