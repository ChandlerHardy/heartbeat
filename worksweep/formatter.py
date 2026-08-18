"""Render WorkItems into Discord digest message(s).

`format_messages` splits the digest into a list of messages each within the
1900-byte Discord cap (matches heartbeat-lib's send_discord) — so a long sweep
is delivered in full across several messages instead of being truncated.
`format_digest` returns the whole digest as one string for stdout/dry-run.
"""
from __future__ import annotations

import datetime
import re
from typing import List, Optional

from .models import QueueRecord, WorkItem

DISCORD_MAX_CHARS = 1900

_HEADER = "🔭 **Worksweep**"
_FOOTER = "Reply e.g. `✅ 1,3` to approve (approved magi-review items run automatically)."
_ALL_CLEAR = "✅ Worksweep: nothing needs you right now."


def _truncate_bytes(s: str, max_bytes: int = DISCORD_MAX_CHARS) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    cut = enc[:max_bytes - 3]
    while cut and (cut[-1] & 0xC0) == 0x80:  # rewind partial multibyte tail
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + "..."


def _compute_age_days(iso_ts: str, iso_now: str) -> Optional[int]:
    """Compute age in whole days. Unparseable -> None (never destroy on bad data)."""
    try:
        ts = datetime.datetime.fromisoformat(iso_ts)
        now = datetime.datetime.fromisoformat(iso_now)
    except (ValueError, TypeError):
        return None
    if (ts.tzinfo is None) != (now.tzinfo is None):  # naive/aware mix -> keep
        return None
    delta = now - ts
    return delta.days


_TITLE_LIMIT = 60

# An MR/issue title is untrusted (author-supplied) text riding straight into
# a Discord message and into the curator LLM's prompt. Neutralize markdown/
# link injection at this render boundary: `[`/`]` are stripped so a
# `[text](url)` link shape can never form (Discord requires the literal `]('
# sequence — dropping `]` alone kills it), backticks are stripped so a title
# can't break out of/reopen a code span, `*`/`_`/`~` runs are stripped so a
# title can't break out of the `*title*` emphasis wrapper or force bold/
# strike formatting, and any `http(s)://` scheme is neutralized so no
# clickable URL can ride through even without link syntax.
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_MD_EMPHASIS_RE = re.compile(r"[*_~]+")


def _sanitize_title(title: str) -> str:
    """Strip markdown/link-injection characters from an untrusted title
    before it's rendered into Discord markdown (or fed to the curator LLM
    -- see curator._record_line, which reuses this). Empty -> empty."""
    if not title:
        return ""
    out = title.replace("[", "").replace("]", "").replace("`", "")
    out = _MD_EMPHASIS_RE.sub("", out)
    out = _SCHEME_RE.sub("hxxp://", out)
    return out


def _truncate_title(title: str, limit: int = _TITLE_LIMIT) -> str:
    """Sanitize, collapse to one line, and cap at `limit` chars, single-`…`
    truncation for readability. Empty title -> empty string (no segment)."""
    if not title:
        return ""
    single = " ".join(_sanitize_title(title).split())
    if len(single) <= limit:
        return single
    return single[:limit].rstrip() + "…"


def _item_line(n: int, it: WorkItem, first_seen: Optional[str] = None,
               now: Optional[str] = None) -> str:
    # One compact line per item. The web_url is a masked link ([#ref](url)) so
    # Discord renders no embed card (25 raw URLs would spawn 25 previews) and the
    # ref number stays visible. The leading `n.` is the approval handle for M2.
    # `handoff` items (ready-to-merge, handed to a maintainer) are informational
    # rather than actionable, so they render with a leading ✅.
    prefix = "✅ " if it.kind == "handoff" else ""
    parts = [f"{prefix}{n}.", f"`{it.executor}`"]
    if it.repo:
        parts.append(it.repo)
    if it.web_url:
        ref = it.web_url.rstrip("/").rsplit("/", 1)[-1]
        parts.append(f"[#{ref}]({it.web_url})")
    title = _truncate_title(it.title)
    if title:
        parts.append(f"*{title}*")
    line = f"{' '.join(parts)} — {it.why}"

    # Add age marker if first_seen is > 5 days old
    if first_seen and now:
        age_days = _compute_age_days(first_seen, now)
        if age_days is not None and age_days > 5:
            line += f" ⏳{age_days}d"

    return line


_HANDOFF_HEADER = "**Handed off (no action):**"


def _group_handoff_last(numbered: List[tuple]) -> List[tuple]:
    """Split (number, item, first_seen?) tuples into (non_handoff, handoff),
    each preserving relative order. Numbers are untouched (stable-numbering
    contract) -- only render order moves handoff items to a trailing group,
    so the curator/reader can tell "ready to merge, no action" apart from
    the actionable list."""
    non_handoff = [t for t in numbered if t[1].kind != "handoff"]
    handoff = [t for t in numbered if t[1].kind == "handoff"]
    return non_handoff, handoff


def _numbered(items: List[WorkItem]) -> List[tuple]:
    """Pair items with sequential numbers (M1 path, no queue)."""
    return list(enumerate(items, 1))


# Bytes reserved on every header-bearing message for a "(i/N)" part label,
# added only once the final message count is known (see below). Comfortably
# covers up to a 3-digit part count ("(999/999)" is 10 bytes).
_LABEL_RESERVE = 12


def _format_messages_numbered(numbered: List[tuple],
                              now: Optional[str] = None,
                              max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Split a list of (number, WorkItem, first_seen?) pairs into capped messages.

    Multi-part digests get a "(i/N)" label on each header line so a reader
    knows a message is a fragment, not the whole sweep. The split itself
    reserves `_LABEL_RESERVE` bytes of headroom up front so adding the label
    afterward can never push a message over `max_bytes`.
    """
    if not numbered:
        return [_ALL_CLEAR]
    cap = max_bytes - _LABEL_RESERVE
    head = f"{_HEADER} — {len(numbered)} item(s) need you:"
    cont = f"{_HEADER} *(cont.)*"
    msgs: List[str] = []
    cur = head

    def _append_line(line: str) -> None:
        nonlocal cur
        candidate = f"{cur}\n{line}"
        if len(candidate.encode("utf-8")) > cap:
            msgs.append(cur)
            cur = f"{cont}\n{line}"
        else:
            cur = candidate

    non_handoff, handoff = _group_handoff_last(numbered)
    for item_tuple in non_handoff:
        n = item_tuple[0]
        it = item_tuple[1]
        first_seen = item_tuple[2] if len(item_tuple) > 2 else None
        # Reserve headroom so even an oversized single line fits under a
        # continuation header without breaching the cap.
        line = _truncate_bytes(_item_line(n, it, first_seen=first_seen, now=now),
                               max_bytes - 80)
        _append_line(line)
    if handoff:
        _append_line(_HANDOFF_HEADER)
        for item_tuple in handoff:
            n = item_tuple[0]
            it = item_tuple[1]
            first_seen = item_tuple[2] if len(item_tuple) > 2 else None
            line = _truncate_bytes(_item_line(n, it, first_seen=first_seen, now=now),
                                   max_bytes - 80)
            _append_line(line)
    with_footer = f"{cur}\n\n{_FOOTER}"
    if len(with_footer.encode("utf-8")) <= cap:
        msgs.append(with_footer)
    else:
        msgs.append(cur)
        msgs.append(_FOOTER)
    if len(msgs) > 1:
        total = len(msgs)
        msgs = [m.replace(_HEADER, f"{_HEADER} ({i}/{total})", 1) if _HEADER in m else m
               for i, m in enumerate(msgs, 1)]
    return msgs


def _format_digest_numbered(numbered: List[tuple], now: Optional[str] = None) -> str:
    if not numbered:
        return _ALL_CLEAR
    lines = [f"{_HEADER} — {len(numbered)} item(s) need you:"]
    non_handoff, handoff = _group_handoff_last(numbered)
    for item_tuple in non_handoff:
        n = item_tuple[0]
        it = item_tuple[1]
        first_seen = item_tuple[2] if len(item_tuple) > 2 else None
        lines.append(_item_line(n, it, first_seen=first_seen, now=now))
    if handoff:
        lines.append(_HANDOFF_HEADER)
        for item_tuple in handoff:
            n = item_tuple[0]
            it = item_tuple[1]
            first_seen = item_tuple[2] if len(item_tuple) > 2 else None
            lines.append(_item_line(n, it, first_seen=first_seen, now=now))
    lines.append(f"\n{_FOOTER}")
    return "\n".join(lines)


def format_messages(items: List[WorkItem],
                    max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Split the digest into messages each <= max_bytes (byte-safe)."""
    return _format_messages_numbered(_numbered(items), max_bytes=max_bytes)


def format_digest(items: List[WorkItem]) -> str:
    """The whole digest as a single string (uncapped) — for stdout/dry-run."""
    return _format_digest_numbered(_numbered(items))


def _records_numbered(records: List[QueueRecord]) -> List[tuple]:
    """Pair each record's WorkItem with its PERSISTED number and first_seen, in number order.

    This is the numbering contract: the digest number a user sees (and replies
    to) is the queue's `number`, not a render-time enumerate — so `✅ 3` maps to
    the same WorkItem the queue knows as #3 even across sweeps where lower
    numbers dropped. Include first_seen for age marker computation.
    """
    ordered = sorted(records, key=lambda r: r.number)
    return [(r.number, r.item, r.first_seen) for r in ordered]


def format_messages_from_records(records: List[QueueRecord],
                                 now: Optional[str] = None,
                                 max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Capped messages rendered from queue records in persisted-number order.

    When now is provided, records with first_seen > 5 days before now render with
    an age marker ⏳{d}d. When now=None, no age markers (backward compatible).
    """
    return _format_messages_numbered(_records_numbered(records), now, max_bytes)


def format_digest_from_records(records: List[QueueRecord],
                               now: Optional[str] = None) -> str:
    """Single-string digest rendered from queue records in number order.

    When now is provided, records with first_seen > 5 days before now render with
    an age marker ⏳{d}d. When now=None, no age markers (backward compatible).
    """
    return _format_digest_numbered(_records_numbered(records), now)
