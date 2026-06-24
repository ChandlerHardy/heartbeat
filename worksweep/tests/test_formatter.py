import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem  # noqa: E402
from worksweep.formatter import (  # noqa: E402
    format_digest, format_messages, DISCORD_MAX_CHARS,
)


def _wi(i, executor="magi-review", why="why"):
    return WorkItem(schema_version=1, id=f"x{i}", repo="pb-www", kind="mr",
                    executor=executor, risk="low", why=why,
                    web_url=f"https://gitlab.com/x/-/merge_requests/{i}", sha="abc")


def test_empty_digest_says_all_clear():
    assert "nothing needs you" in format_digest([]).lower()


def test_empty_messages_is_single_all_clear():
    msgs = format_messages([])
    assert len(msgs) == 1 and "nothing needs you" in msgs[0].lower()


def test_digest_numbers_items():
    out = format_digest([_wi(1), _wi(2)])
    assert "1." in out and "2." in out


def test_digest_includes_executor_and_why():
    out = format_digest([_wi(1, executor="mr-hygiene", why="missing dev link")])
    assert "mr-hygiene" in out and "missing dev link" in out


def test_item_uses_masked_link_no_raw_embed():
    # masked link form [#ref](url) keeps Discord from rendering an embed card
    out = format_digest([_wi(42)])
    assert "[#42](https://gitlab.com/x/-/merge_requests/42)" in out


def test_small_list_is_a_single_message():
    assert len(format_messages([_wi(1), _wi(2)])) == 1


def test_long_digest_splits_into_multiple_messages():
    msgs = format_messages([_wi(i) for i in range(300)])
    assert len(msgs) > 1


def test_every_message_within_byte_cap_multibyte_safe():
    msgs = format_messages([_wi(i, why="café 🔭 needs review urgently now please")
                            for i in range(300)])
    for m in msgs:
        enc = m.encode("utf-8")
        assert len(enc) <= DISCORD_MAX_CHARS
        assert enc.decode("utf-8") == m  # no partial multibyte tail


def test_all_items_present_across_messages():
    n = 80
    joined = "\n".join(format_messages([_wi(i) for i in range(n)]))
    assert "1. " in joined and f"{n}. " in joined  # first and last survive


def test_oversized_single_item_line_truncated_byte_safe():
    huge = "x🔭" * 1000  # > 1900 bytes, multibyte
    msgs = format_messages([_wi(1, why=huge)])
    for m in msgs:
        enc = m.encode("utf-8")
        assert len(enc) <= DISCORD_MAX_CHARS
        assert enc.decode("utf-8") == m
