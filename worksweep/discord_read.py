"""Read Discord channel messages via the REST API (stdlib `urllib`).

Webhooks are send-only; reading approval replies needs a bot identity. To keep
worksweep stdlib-only (no discord.py / websocket gateway) the poller issues a
single read — `GET /channels/{id}/messages` — with `Authorization: Bot {token}`,
mirroring __main__._post_discord's urllib discipline.

`parse_messages` is pure and unit-tested (tolerates malformed / partial JSON,
like collectors._loads_list). `fetch_messages` is the thin, untested I/O wrapper.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from typing import Callable, List, Optional
import urllib.parse

from .models import DiscordMessage

_API_BASE = "https://discord.com/api/v10"
_USER_AGENT = "WorksweepBot/1.0 (worksweep intake poller)"


def parse_messages(raw_json: str) -> List[DiscordMessage]:
    """Map a Discord messages JSON array onto DiscordMessage.

    Malformed JSON or a non-list payload -> []. A message missing `author`
    yields author_id="" (so it can never match the configured user id); missing
    content -> "".
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"worksweep: parse_messages decode failed: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"worksweep: parse_messages expected a list, got "
              f"{type(data).__name__}", file=sys.stderr)
        return []
    out: List[DiscordMessage] = []
    for m in data:
        try:
            author = m.get("author") or {}
            out.append(DiscordMessage(
                id=str(m.get("id", "")),
                author_id=str(author.get("id", "")),
                content=m.get("content") or "",
                timestamp=m.get("timestamp", ""),
            ))
        except (AttributeError, TypeError) as e:
            print(f"worksweep: parse_messages skipping bad row: {e}",
                  file=sys.stderr)
    return out


def fetch_messages(channel_id: str, bot_token: str,
                   after: Optional[str] = None, limit: int = 50,
                   timeout: int = 15) -> List[DiscordMessage]:
    """GET the latest channel messages (read-only). Thin I/O wrapper — untested.

    `after` is a Discord message snowflake id (not a timestamp); the poller
    passes the last-seen message id so each poll reads only newer messages.
    """
    url = f"{_API_BASE}/channels/{channel_id}/messages?limit={int(limit)}"
    if after:
        url += f"&after={after}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {bot_token}",
        "User-Agent": _USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return parse_messages(body)


# --- reactions (M4: 👀 acknowledgement on approval replies) -----------------

def react(channel_id: str, message_id: str, emoji: str, bot_token: str,
          timeout: int = 10, opener: Optional[Callable] = None) -> bool:
    """PUT a reaction from the bot onto one message. Best-effort: returns
    False (and logs) on any failure — a missing 👀 must never block intake.
    Requires the bot to have Add Reactions + Read Message History on the
    channel. `opener` is injectable for tests (defaults to urllib.urlopen)."""
    url = (f"{_API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/"
           f"{urllib.parse.quote(emoji, safe='')}/@me")
    req = urllib.request.Request(url, method="PUT", headers={
        "Authorization": f"Bot {bot_token}",
        "User-Agent": _USER_AGENT,
        "Content-Length": "0",
    })
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 204) < 300
    except Exception as e:  # network / 4xx — never raise out of intake
        print(f"worksweep: reaction {emoji} on {message_id} failed: {e}",
              file=sys.stderr)
        return False
