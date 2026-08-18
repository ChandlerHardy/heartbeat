"""M4 Task F: dev-slot sensing for the `implement` executor.

`probe` runs one ssh per configured box (edge injected — never raises, an
unreachable/nonzero box just degrades to an unknown branch/sha) and returns
a `DevBox` per entry. `classify` (pure) maps each box's branch onto a tier
using only the OPEN MRs the GraphQL sweep already fetched (review + authored
+ assigned) -- a branch with no open MR in that set reads as `free` (its MR
merged/closed, or it was never pushed), a branch matching an open MR that is
`is_handed_off` (see assessor.is_handed_off) reads as `handed_off`
(reclaimable — the maintainer will merge it), and anything else (a branch
under live review, an unknown/unreachable branch, or a box a queue record
already claims) reads as `live` -- fail-safe: never hand a box to the
implement executor unless we're confident it's free.

`pick`/`summary_line` are pure helpers over the resulting `{box_name: tier}`
dict; both take/derive the box iteration order rather than relying on dict
ordering, so callers control tie-breaking explicitly.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence

from .assessor import is_handed_off
from .models import MergeRequest

_TIER_FREE = "free"
_TIER_HANDED_OFF = "handed_off"
_TIER_LIVE = "live"


@dataclass(frozen=True)
class DevBox:
    name: str
    host: str
    path: str
    url: str
    branch: str = ""   # "" = unknown/unreachable (probe couldn't determine it)
    sha: str = ""


def probe(boxes_cfg: List[dict],
         run_ssh: Callable[[str, str], str]) -> List[DevBox]:
    """Shell edge (injected): one ssh per box config dict
    ({"name","host","path","url"}) running `cd <path> && git branch
    --show-current && git rev-parse HEAD`. `run_ssh(host, command) -> str`
    is expected to raise on an unreachable host or non-zero exit (mirrors
    collectors._run_glab) -- probe() catches that per-box and degrades to
    branch="" sha="" rather than losing the whole sweep to one bad box."""
    out: List[DevBox] = []
    for cfg in boxes_cfg:
        name = cfg.get("name", "")
        host = cfg.get("host", "")
        path = cfg.get("path", "")
        url = cfg.get("url", "")
        branch, sha = "", ""
        try:
            raw = run_ssh(host, f"cd {path} && git branch --show-current "
                                f"&& git rev-parse HEAD")
            lines = (raw or "").strip().splitlines()
            if len(lines) >= 1:
                branch = lines[0].strip()
            if len(lines) >= 2:
                sha = lines[1].strip()
        except Exception as e:
            print(f"worksweep: devslots probe {name!r} ({host}) failed: {e}",
                  file=sys.stderr)
            branch, sha = "", ""
        out.append(DevBox(name=name, host=host, path=path, url=url,
                          branch=branch, sha=sha))
    return out


def classify(boxes: List[DevBox], all_mrs: List[MergeRequest], username: str,
            claimed: FrozenSet[str] = frozenset()) -> Dict[str, str]:
    """Pure: box list + the sweep's OPEN MRs -> {box.name: tier}.

    `all_mrs` should be review-requested + authored + assigned MRs from the
    same sweep (only open MRs) carrying `source_branch`. First MR wins on a
    duplicate source_branch (stable given the sweep's deterministic bucket
    order); a real duplicate is not expected in practice (GitLab does not
    allow two open MRs from the same source branch in one project).
    """
    by_branch: Dict[str, MergeRequest] = {}
    for mr in all_mrs:
        sb = mr.source_branch or ""
        if sb and sb not in by_branch:
            by_branch[sb] = mr

    tiers: Dict[str, str] = {}
    for box in boxes:
        if box.name in claimed:
            tiers[box.name] = _TIER_LIVE
            continue
        if not box.branch:
            tiers[box.name] = _TIER_LIVE
            continue
        mr = by_branch.get(box.branch)
        if mr is None:
            tiers[box.name] = _TIER_FREE
        elif is_handed_off(mr, username):
            tiers[box.name] = _TIER_HANDED_OFF
        else:
            tiers[box.name] = _TIER_LIVE
    return tiers


def pick(tiers: Dict[str, str], order: Sequence[str]) -> Optional[str]:
    """First free box in `order`, else first handed-off box in `order`, else
    None. `order` is the deterministic tie-break (config order)."""
    for name in order:
        if tiers.get(name) == _TIER_FREE:
            return name
    for name in order:
        if tiers.get(name) == _TIER_HANDED_OFF:
            return name
    return None


def summary_line(tiers: Dict[str, str]) -> str:
    """One-line digest preamble, grouped by tier in the box iteration order
    of `tiers` (an ordinary dict preserves insertion order):
    `Dev slots: dev1 free · dev4, dev5 reclaimable (approved, awaiting
    merge) · dev0, dev2, dev3 live`. Empty tiers -> a graceful placeholder
    (never raises, never silent)."""
    if not tiers:
        return "Dev slots: none configured"
    order = list(tiers.keys())
    free = [n for n in order if tiers[n] == _TIER_FREE]
    reclaimable = [n for n in order if tiers[n] == _TIER_HANDED_OFF]
    live = [n for n in order if tiers[n] == _TIER_LIVE]
    parts = []
    if free:
        parts.append(f"{', '.join(free)} free")
    if reclaimable:
        parts.append(f"{', '.join(reclaimable)} reclaimable "
                     f"(approved, awaiting merge)")
    if live:
        parts.append(f"{', '.join(live)} live")
    return "Dev slots: " + " · ".join(parts)
