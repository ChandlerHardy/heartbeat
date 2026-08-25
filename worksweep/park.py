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
from .models import WorkItem, has_dev_url
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


def _mr_path(repo: str, iid: int) -> str:
    from .collectors import _project        # local: collectors is a heavier import
    return f"projects/{_project(repo)}/merge_requests/{int(iid)}"


def fetch_description(run_glab: Callable, repo: str, iid: int) -> str:
    raw = run_glab(["api", _mr_path(repo, iid)])
    try:
        return (json.loads(raw) or {}).get("description") or ""
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

    # Syncs, checks for drift, and health-checks the box: returns only on a
    # branch that actually landed AND a 200.
    result_sha = implementer.sync_to_box(slot, branch, run_ssh, http_get)

    updated = False
    description = fetch_description(run_glab, item.repo, iid)
    new_description = prepend_header(description, slot.url)
    if new_description is not None:
        put_description(run_glab, item.repo, iid, new_description)
        updated = True
    return ParkResult(iid=iid, box_name=slot.name, dev_url=slot.url,
                      result_sha=result_sha, description_updated=updated)


def done_message(result: ParkResult) -> str:
    tail = ("description updated" if result.description_updated
            else "description already had a dev link")
    return (f"🅿️ !{result.iid} parked on {result.box_name} (200) · {tail}\n"
            f"<{result.dev_url}>")
