"""Render WorkItems into Discord digest message(s).

`format_messages` splits the digest into a list of messages each within the
1900-byte Discord cap (matches heartbeat-lib's send_discord) — so a long sweep
is delivered in full across several messages instead of being truncated.
`format_digest` returns the whole digest as one string for stdout/dry-run.
"""
from __future__ import annotations

from typing import List

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


def _item_line(n: int, it: WorkItem) -> str:
    # One compact line per item. The web_url is a masked link ([#ref](url)) so
    # Discord renders no embed card (25 raw URLs would spawn 25 previews) and the
    # ref number stays visible. The leading `n.` is the approval handle for M2.
    parts = [f"{n}.", f"`{it.executor}`"]
    if it.repo:
        parts.append(it.repo)
    if it.web_url:
        ref = it.web_url.rstrip("/").rsplit("/", 1)[-1]
        parts.append(f"[#{ref}]({it.web_url})")
    return f"{' '.join(parts)} — {it.why}"


def _numbered(items: List[WorkItem]) -> List[tuple]:
    """Pair items with sequential numbers (M1 path, no queue)."""
    return list(enumerate(items, 1))


# Bytes reserved on every header-bearing message for a "(i/N)" part label,
# added only once the final message count is known (see below). Comfortably
# covers up to a 3-digit part count ("(999/999)" is 10 bytes).
_LABEL_RESERVE = 12


def _format_messages_numbered(numbered: List[tuple],
                              max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Split a list of (number, WorkItem) pairs into capped messages.

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
    for n, it in numbered:
        # Reserve headroom so even an oversized single line fits under a
        # continuation header without breaching the cap.
        line = _truncate_bytes(_item_line(n, it), max_bytes - 80)
        candidate = f"{cur}\n{line}"
        if len(candidate.encode("utf-8")) > cap:
            msgs.append(cur)
            cur = f"{cont}\n{line}"
        else:
            cur = candidate
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


def _format_digest_numbered(numbered: List[tuple]) -> str:
    if not numbered:
        return _ALL_CLEAR
    lines = [f"{_HEADER} — {len(numbered)} item(s) need you:"]
    lines += [_item_line(n, it) for n, it in numbered]
    lines.append(f"\n{_FOOTER}")
    return "\n".join(lines)


def format_messages(items: List[WorkItem],
                    max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Split the digest into messages each <= max_bytes (byte-safe)."""
    return _format_messages_numbered(_numbered(items), max_bytes)


def format_digest(items: List[WorkItem]) -> str:
    """The whole digest as a single string (uncapped) — for stdout/dry-run."""
    return _format_digest_numbered(_numbered(items))


def _records_numbered(records: List[QueueRecord]) -> List[tuple]:
    """Pair each record's WorkItem with its PERSISTED number, in number order.

    This is the numbering contract: the digest number a user sees (and replies
    to) is the queue's `number`, not a render-time enumerate — so `✅ 3` maps to
    the same WorkItem the queue knows as #3 even across sweeps where lower
    numbers dropped.
    """
    ordered = sorted(records, key=lambda r: r.number)
    return [(r.number, r.item) for r in ordered]


def format_messages_from_records(records: List[QueueRecord],
                                 max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Capped messages rendered from queue records in persisted-number order."""
    return _format_messages_numbered(_records_numbered(records), max_bytes)


def format_digest_from_records(records: List[QueueRecord]) -> str:
    """Single-string digest rendered from queue records in number order."""
    return _format_digest_numbered(_records_numbered(records))
