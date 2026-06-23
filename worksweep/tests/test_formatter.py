import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem  # noqa: E402
from worksweep.formatter import format_digest, DISCORD_MAX_CHARS  # noqa: E402


def _wi(i, executor="magi-review", why="why"):
    return WorkItem(schema_version=1, id=f"x{i}", repo="pb-www", kind="mr",
                    executor=executor, risk="low", why=why,
                    web_url=f"https://gitlab.com/x/-/merge_requests/{i}", sha="abc")


def test_empty_digest_says_all_clear():
    out = format_digest([])
    assert "nothing needs you" in out.lower()


def test_digest_numbers_items():
    out = format_digest([_wi(1), _wi(2)])
    assert "1." in out and "2." in out


def test_digest_includes_executor_and_why():
    out = format_digest([_wi(1, executor="mr-hygiene", why="missing dev link")])
    assert "mr-hygiene" in out and "missing dev link" in out


def test_digest_capped_to_byte_limit():
    out = format_digest([_wi(i) for i in range(200)])
    assert len(out.encode("utf-8")) <= DISCORD_MAX_CHARS


# FIX 10 — multibyte truncation stays byte-safe, decodes cleanly, ends with "..."
def test_digest_multibyte_truncation_is_byte_safe():
    items = [_wi(i, why="café 🔭 review needed urgently right now please") for i in range(200)]
    out = format_digest(items)
    enc = out.encode("utf-8")
    assert len(enc) <= DISCORD_MAX_CHARS
    # decodes without raising (no partial multibyte tail)
    assert enc.decode("utf-8") == out
    assert out.endswith("...")
