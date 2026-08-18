"""👀 acknowledgement on approval replies (intake)."""
from worksweep.__main__ import _messages_carrying
from worksweep.discord_read import react
from worksweep.models import DiscordMessage


def _msg(id, author, content):
    return DiscordMessage(id=id, author_id=author, content=content, timestamp="t")


def test_messages_carrying_selects_only_users_flipping_replies():
    msgs = [_msg("1", "me", "✅ 153"),          # flipped 153
            _msg("2", "me", "hello there"),     # not an approval
            _msg("3", "other", "✅ 153"),       # wrong author
            _msg("4", "me", "✅ 999")]          # approval of a number that did NOT flip
    got = _messages_carrying(msgs, "me", {153})
    assert [m.id for m in got] == ["1"]


def test_react_success_via_injected_opener():
    class _Resp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
    calls = []
    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url))
        return _Resp()
    assert react("chan", "msg", "👀", "tok", opener=opener) is True
    method, url = calls[0]
    assert method == "PUT"
    assert url.endswith("/channels/chan/messages/msg/reactions/%F0%9F%91%80/@me")


def test_react_failure_returns_false_never_raises():
    def opener(req, timeout):
        raise OSError("boom")
    assert react("chan", "msg", "👀", "tok", opener=opener) is False
