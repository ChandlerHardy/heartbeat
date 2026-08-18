"""M3.5 Task C: LLM curation pass over the non-terminal queue.

`curate(records, now, run_llm)` builds a compact prompt from `records`
(expected to already be non-terminal -- callers pass the same `actionable`
list the raw formatter renders), asks the injected `run_llm(prompt) -> str`
edge for a Discord-ready briefing, and runs it through a deterministic
`validate()` before handing it back. Any failure along the way -- the LLM
call raising/timing out, non-string output, or a validation reject -- is
swallowed and reported as `None` so `__main__.run_sweep` can fall back to
the existing raw multi-part digest. curate() never raises: a flaky or
hallucinating LLM must never take the whole sweep down (never-silent
contract).

The validator is intentionally strict about numbers: every 1-4 digit token
in the LLM's output must be either a queue number or the ref (MR/issue iid)
of one of the records it was given, and every proposed/approved
magi-review item's number must be referenced somewhere. Two noise sources
are stripped before that scan runs (each documented + tested below): age
markers like `(12d)`, and the URL half of a markdown link (the prompt tells
the LLM not to emit links at all, but stripping is cheap insurance against
a URL's incidental digits -- namespace/project ids, dates -- being read as
invented numbers).
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from typing import Callable, List, Optional, Tuple

from .models import QueueRecord

_MAX_OUTPUT_BYTES = 1700
_LLM_TIMEOUT_SECONDS = 120
# Parent of the worksweep/ package -- the heartbeat repo root. Curation has
# no specific per-repo checkout to run in (unlike runner.execute), so the
# LLM edge runs from the repo that owns this code, matching the
# subprocess-edge convention (cwd = a real repo root, not cwd of whatever
# process happens to invoke worksweep).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MAGI_LEAD_STATUSES = ("proposed", "approved")


def _ref_number(web_url: str) -> Optional[int]:
    """Trailing numeric path segment of a web_url (MR/issue iid), else None."""
    if not web_url:
        return None
    seg = web_url.rstrip("/").rsplit("/", 1)[-1]
    return int(seg) if seg.isdigit() else None


def _age_days(first_seen: str, now: str) -> Optional[int]:
    """Whole days between first_seen and now. Tolerant: unparseable or a
    naive/aware mix returns None (matches formatter._compute_age_days and
    queue._older_than_days discipline -- never crash on bad timestamp data)."""
    try:
        ts = datetime.datetime.fromisoformat(first_seen)
        n = datetime.datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return None
    if (ts.tzinfo is None) != (n.tzinfo is None):
        return None
    return (n - ts).days


def _record_line(r: QueueRecord, now: str) -> str:
    ref = _ref_number(r.item.web_url)
    age = _age_days(r.first_seen, now)
    fields = [
        str(r.number), r.item.kind, r.item.executor, r.item.repo,
        str(ref) if ref is not None else "",
        r.item.why,
        str(age) if age is not None else "",
        r.item.status,
        r.item.title,
    ]
    return " | ".join(fields)


_INSTRUCTIONS = """You are curating Chandler's Worksweep digest into a short Discord briefing.

Below is one line per open queue item:
`number | kind | executor | repo | ref | why | age_days | status | title`.

Write the briefing as plain text with these rules, in this order:
1. "Needs your review:" -- one line per item whose executor is `magi-review`
   and whose status is `proposed` or `approved`. Format each as
   `{number}. {repo} !{ref} -- {short title} -- {why}`.
2. "Feedback / CI on your MRs:" -- one line per remaining item whose executor
   is `triage` and whose kind is `feedback` or `ci_red`, same line format.
3. If any item has kind `handoff`, add exactly one trailing informational
   line for all of them together: "Handed off: !{ref} -> {who}, ..." (read
   who it's assigned to from that item's `why` column). Never list a
   handoff item under "Needs your review" -- it's informational, not
   actionable, and its number does not need to appear anywhere at all.
4. Collapse every remaining item (excluding any handoff items, already
   handled by rule 3) into exactly one line:
   "N low-priority items held in queue: <comma-separated queue numbers>"
   where N is the count and the numbers are their exact queue numbers.

Hard rules:
- Only ever cite a queue `number` (leftmost column above) or a `ref` (iid)
  from the table above. Never invent a number.
- Do not include markdown links or raw URLs anywhere in your reply.
- If you mention an item's age, write it as `(Nd)` immediately after that
  item's line, using the age_days column.
- No greeting, no sign-off, no markdown headers (bold with ** is fine).
- Keep the entire reply under 1700 bytes UTF-8.

Queue:
"""


def build_prompt(records: List[QueueRecord], now: str) -> str:
    """The full curator prompt: instructions + one table line per record.

    `records` is expected to already be the non-terminal queue (the same
    list __main__.run_sweep's `actionable` filter produces) -- curate()
    treats it as the complete allowed-numbers universe, so passing a
    smaller/different slice would make the validator reject correct output.
    """
    lines = [_record_line(r, now) for r in records]
    return _INSTRUCTIONS + "\n".join(lines)


# Injection bound: an untrusted MR/issue title riding into the prompt (via
# `why`) can make the LLM emit arbitrary prose, but validate() must ensure it
# can never turn that into a clickable link -- so any URL or markdown link
# syntax is a hard rejection, not just neutralized before the number scan.
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_AGE_TOKEN_RE = re.compile(r"\(\d{1,3}d\)")
# The instructed collapse line's leading count ("N low-priority items held in
# queue: ...") is a tally, not a queue/ref number, and will routinely NOT
# coincide with any real queue number -- strip it by anchoring on the exact
# instructed phrase rather than trying to special-case every small integer.
_HELD_COUNT_RE = re.compile(r"\b\d{1,4}\b(?=\s+low-priority items held in queue)")
_NUMBER_RE = re.compile(r"\b\d{1,4}\b")


def _strip_noise(text: str) -> str:
    """Remove noise sources that legitimately carry digits that are not
    queue/ref numbers, before the validator's number scan runs:
      - age markers like `(12d)` -> removed entirely
      - the held-count tally in the collapsed low-priority line
    (Links are handled separately, by hard rejection in validate() -- not
    stripped-then-allowed, since a URL is itself the thing being disallowed.)
    """
    text = _AGE_TOKEN_RE.sub("", text)
    text = _HELD_COUNT_RE.sub("", text)
    return text


def _allowed_numbers(records: List[QueueRecord]) -> set:
    """Every number the LLM is entitled to output: each record's queue
    number and ref (iid), plus any number already present in a record's own
    `why` text (e.g. "2 unresolved threads") -- the prompt's line format
    invites the LLM to echo `why` verbatim, and a count that already exists
    in our own trusted input is not an invented number, just a faithful
    quote. This is still strict: a number with no origin anywhere in the
    input records is rejected.

    Accepted residual risk: this whitelist is global across all records, not
    scoped per-record -- a number lifted from one record's `why` text would
    also be accepted if it showed up attached to a different record's line.
    Per-record scoping was considered and rejected as not worth the added
    complexity, because the failure mode it would prevent is cosmetic, not a
    security or data-integrity issue: a reference to the wrong (but still
    real) small number just reads oddly -- it doesn't let the LLM invent a
    number that doesn't exist anywhere in the queue (that's still hard
    rejected), it can't forge a link (hard rejected separately, see
    _URL_RE/_MD_LINK_RE), and a `✅ <n>` reply against a number nobody
    actually holds status on is a no-op in intake (apply_approvals only
    matches real queue numbers). The one hard invariant this whitelist must
    never weaken -- every proposed/approved magi-review item's own number is
    referenced somewhere in the output -- is checked independently below and
    is unaffected by how loosely other numbers are sourced.
    `test_validator_accepts_why_digit_reuse_documented_risk` pins this."""
    allowed = {r.number for r in records}
    for r in records:
        ref = _ref_number(r.item.web_url)
        if ref is not None:
            allowed.add(ref)
        allowed.update(int(m) for m in _NUMBER_RE.findall(r.item.why))
    return allowed


def validate(output: str, records: List[QueueRecord]) -> bool:
    """Deterministic accept/reject gate for LLM output. Rejects (returns
    False, logging why to stderr) unless:
      - output is non-empty and <= 1700 bytes UTF-8
      - output contains no URL and no markdown link syntax at all -- this is
        the injection bound: an untrusted title riding into the prompt can
        make the LLM say almost anything, but it can never turn that into a
        clickable link, since the digest is posted straight to Discord
      - every 1-4 digit number referenced (after stripping age markers and
        the held-count tally) is a queue number or ref of a record in
        `records` (or already present in that record's own `why` text)
      - every proposed/approved magi-review record's number is referenced
        somewhere in the output
    """
    if not output or not output.strip():
        print("worksweep: curator validation failed: empty output", file=sys.stderr)
        return False
    size = len(output.encode("utf-8"))
    if size > _MAX_OUTPUT_BYTES:
        print(f"worksweep: curator validation failed: "
              f"{size} bytes > {_MAX_OUTPUT_BYTES} byte cap", file=sys.stderr)
        return False
    if _URL_RE.search(output) or _MD_LINK_RE.search(output):
        print("worksweep: curator validation failed: "
              "output contains a URL or markdown link", file=sys.stderr)
        return False

    stripped = _strip_noise(output)
    allowed = _allowed_numbers(records)
    found = {int(m) for m in _NUMBER_RE.findall(stripped)}
    invented = found - allowed
    if invented:
        print(f"worksweep: curator validation failed: "
              f"invented number(s) {sorted(invented)}", file=sys.stderr)
        return False

    required = {r.number for r in records
                if r.item.executor == "magi-review"
                and r.item.status in _MAGI_LEAD_STATUSES}
    missing = {n for n in required if not re.search(rf"\b{n}\b", stripped)}
    if missing:
        print(f"worksweep: curator validation failed: "
              f"missing magi-review number(s) {sorted(missing)}", file=sys.stderr)
        return False

    return True


def curate(records: List[QueueRecord], now: str,
          run_llm: Callable[[str], str]) -> Optional[str]:
    """Ask `run_llm` to curate `records` into a Discord-ready briefing.

    Never raises. Returns None (fall back to the raw digest) when: there are
    no records, run_llm raises/times out, run_llm returns something that
    isn't a usable string, or the output fails validate()."""
    if not records:
        return None
    try:
        prompt = build_prompt(records, now)
        output = run_llm(prompt)
        if not isinstance(output, str):
            print("worksweep: curator LLM returned non-string output",
                  file=sys.stderr)
            return None
        output = output.strip()
        if not validate(output, records):
            return None
        return output
    except Exception as e:
        print(f"worksweep: curator LLM call failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None


def partition_counts(records: List[QueueRecord]) -> Tuple[int, int]:
    """(n_actionable, m_held) -- the same split the prompt instructs the LLM
    to make (rules 1+2 vs rule 3), computed deterministically from the
    records so the digest header's counts never depend on parsing the LLM's
    own text."""
    n = sum(1 for r in records if
            (r.item.executor == "magi-review" and r.item.status in _MAGI_LEAD_STATUSES)
            or (r.item.executor == "triage" and r.item.kind in ("feedback", "ci_red")))
    return n, len(records) - n


def make_run_llm(cfg, run_subprocess: Callable = subprocess.run
                 ) -> Callable[[str], str]:
    """Production run_llm edge: `<claude_bin> -p <prompt>` run from the
    heartbeat repo root, 120s hard timeout. Mirrors runner.execute's
    injected-subprocess pattern. Raises on a non-zero exit or timeout --
    curate() catches that and treats it as a curation failure."""
    def _run(prompt: str) -> str:
        try:
            proc = run_subprocess(
                [cfg.claude_bin, "-p", prompt], cwd=_REPO_ROOT,
                capture_output=True, text=True, timeout=_LLM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"curator LLM exceeded {_LLM_TIMEOUT_SECONDS}s")
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
            raise RuntimeError(f"curator LLM exited {proc.returncode}: {tail}")
        return proc.stdout
    return _run
