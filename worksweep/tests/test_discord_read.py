import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import DiscordMessage  # noqa: E402
from worksweep.discord_read import parse_messages  # noqa: E402


def test_parse_well_formed_messages():
    raw = json.dumps([
        {"id": "111", "author": {"id": "user-a"}, "content": "✅ 1,3",
         "timestamp": "2026-06-23T09:00:00.000000+00:00"},
        {"id": "222", "author": {"id": "user-b"}, "content": "nice",
         "timestamp": "2026-06-23T09:01:00.000000+00:00"},
    ])
    out = parse_messages(raw)
    assert out == [
        DiscordMessage(id="111", author_id="user-a", content="✅ 1,3",
                       timestamp="2026-06-23T09:00:00.000000+00:00"),
        DiscordMessage(id="222", author_id="user-b", content="nice",
                       timestamp="2026-06-23T09:01:00.000000+00:00"),
    ]


def test_malformed_json_returns_empty():
    assert parse_messages("{ not json") == []


def test_non_list_returns_empty():
    assert parse_messages(json.dumps({"id": "1"})) == []


def test_message_missing_author_does_not_crash():
    raw = json.dumps([
        {"id": "1", "content": "hi", "timestamp": "t"},                # no author
        {"id": "2", "author": {"id": "u"}, "content": "yo", "timestamp": "t"},
    ])
    out = parse_messages(raw)
    # the author-less message is kept with author_id="" (so it simply can't
    # match the configured user id later) and the good one parses normally
    assert out[0].author_id == ""
    assert out[1].author_id == "u"


def test_missing_content_defaults_to_empty_string():
    raw = json.dumps([{"id": "1", "author": {"id": "u"}, "timestamp": "t"}])
    assert parse_messages(raw)[0].content == ""
