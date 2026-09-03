import re
import ast, contextlib, dataclasses, http.client, json, os, re, socket, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest  # noqa: E402
from worksweep import dashboard  # noqa: E402
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import load_queue, save_queue  # noqa: E402

T0 = "2026-06-23T08:00:00Z"
NOW = "2026-06-30T08:00:00Z"
# sentinel: "no actor field at all" is a distinct case from actor=None
_UNSET = object()


def _rec(n, status="proposed", executor="magi-review", **kw):
    item = dict(schema_version=1, id=f"id{n}", repo="pb-www", kind="mr",
                executor=executor, risk="low", why="w",
                web_url=f"https://gl/pb-www/-/merge_requests/{4800 + n}",
                sha="abc", status=status, title=f"title {n}")
    item.update(kw)
    return QueueRecord(number=n, first_seen=T0, last_seen=T0, item=WorkItem(**item))


def _ago(days):
    """ISO timestamp `days` before NOW."""
    import datetime
    return (datetime.datetime.fromisoformat(NOW)
            - datetime.timedelta(days=days)).isoformat()


def _page(records=(), now=NOW, mtime=1_750_000_000.0):
    return dashboard.render_page(list(records), now, mtime)


# --- CSS / markup helpers so assertions stay exact, never loose substrings ----

def _style(page):
    m = re.search(r"<style>(.*?)</style>", page, re.S)
    assert m, "page has no inline <style> block"
    return m.group(1)


def _script(page):
    """The page's OWN body script -- never htmx's.

    51KB of vendored htmx is inlined in <head>, and its source contains
    `pushState`, `location.search`, `location.href` and `location.reload`. A
    whole-page `"location.reload" not in page` assertion would therefore be a
    permanent false positive, so every JS-source assertion in this file runs
    against this slice instead. Our script is always the last one on the page.
    """
    i = page.rindex("<script>")
    j = page.index("</script>", i)
    return page[i + len("<script>"):j]


def _markup(page):
    """The page with every <script> block removed.

    For assertions about what the DOM does or does not contain. htmx's inlined
    source is 51KB of minified English-ish identifiers, so a bare
    `"Auto" not in page` would match `-URI-AutoEncoded` and fail forever.
    """
    return re.sub(r"<script>.*?</script>", "", page, flags=re.S)


def _rule(css, selector):
    """Declaration body of the first rule whose selector list contains `selector`."""
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in sel:
            return body
    return None


def _selectors(css):
    return [sel.strip() for sel, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _block(css, opener):
    """Raw text of a brace-counted block whose header contains `opener`."""
    i = css.find(opener)
    assert i != -1, f"no block containing {opener!r}"
    start = css.index("{", i)
    depth, j = 0, start
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1:j]
        j += 1
    raise AssertionError(f"unbalanced block for {opener!r}")


def _checkboxes(page, view):
    out = []
    for tag in re.findall(r"<input[^>]*>", page):
        if 'type="checkbox"' in tag and f'data-view="{view}"' in tag:
            out.append(int(re.search(r'value="(\d+)"', tag).group(1)))
    return out


# --- module-surface introspection: assert on imports and calls, never prose ---

def _dashboard_src():
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "dashboard.py")).read()


def _dashboard_tree():
    return ast.parse(_dashboard_src())


def _imported_modules(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            mods.add("." * (node.level or 0) + (node.module or ""))
    return mods


def _called_names(tree):
    """Every dotted callee name that appears in a Call node."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f, parts = node.func, []
            while isinstance(f, ast.Attribute):
                parts.append(f.attr)
                f = f.value
            if isinstance(f, ast.Name):
                parts.append(f.id)
            if parts:
                out.add(".".join(reversed(parts)))
    return out


# --- server harness: real HTTP on port 0, always shut down --------------------

class _Reader:
    """Reads one blank-line-terminated SSE frame at a time, under a deadline."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def _fill(self, deadline):
        left = deadline - time.monotonic()
        assert left > 0, "timed out waiting on the stream"
        self.sock.settimeout(left)
        chunk = self.sock.recv(4096)
        assert chunk, "stream closed by the server"
        self.buf += chunk

    def _until(self, sep, timeout):
        deadline = time.monotonic() + timeout
        while sep not in self.buf:
            self._fill(deadline)
        head, _, self.buf = self.buf.partition(sep)
        return head.decode()

    def headers(self, timeout=5):
        return self._until(b"\r\n\r\n", timeout)

    def frame(self, timeout=5):
        """One SSE frame: every line up to the terminating blank line."""
        return self._until(b"\n\n", timeout)



class _Server:
    def __init__(self, qpath, post=None, webhook="", now=NOW, sweep=None,
                 mark_todo_done=None, seen_path=""):
        self.httpd = dashboard.make_server(("127.0.0.1", 0), qpath, post=post,
                                           webhook=webhook, now=lambda: now,
                                           sweep=sweep,
                                           mark_todo_done=mark_todo_done,
                                           seen_path=seen_path)
        # poll fast: serve_forever's default 0.5s interval is paid on every
        # shutdown() and would dominate the suite runtime
        self.thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def approve(self, numbers, headers=None, actor=_UNSET):
        h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
        h.update(headers or {})
        body = {"numbers": numbers}
        if actor is not _UNSET:
            body["actor"] = actor
        return self.request("POST", "/approve", json.dumps(body), h)

    def dismiss(self, number, headers=None, body=None):
        h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
        h.update(headers or {})
        if body is None:
            body = json.dumps({"number": number})
        return self.request("POST", "/dismiss", body, h)

    def sweep(self, headers=None):
        h = {"X-Worksweep": "approve"}
        h.update(headers or {})
        return self.request("POST", "/sweep", "", h)

    def approve_all(self, numbers, headers=None, actor=_UNSET):
        """F2: the page sends the proposed+runnable numbers it rendered."""
        h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
        h.update(headers or {})
        body = {"numbers": numbers}
        if actor is not _UNSET:
            body["actor"] = actor
        return self.request("POST", "/approve-all", json.dumps(body), h)

    @contextlib.contextmanager
    def stream(self, path="/events", timeout=5):
        """A raw-socket client for a response that never ends.

        `request()` cannot be reused: getresponse() + read() block until the
        body is complete, and a stream has no end -- it would sit on the fixed
        5s timeout and then raise. Always closed before the server is.
        """
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                "Accept: text/event-stream\r\n\r\n".encode())
            yield _Reader(sock)
        finally:
            sock.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def serve_queue(tmp_path):
    made = []

    def _make(records, post=None, webhook="", now=NOW, sweep=None,
              mark_todo_done=None, seen_path=""):
        qpath = os.path.join(str(tmp_path), "queue.json")
        save_queue(qpath, list(records))
        s = _Server(qpath, post=post, webhook=webhook, now=now, sweep=sweep,
                    mark_todo_done=mark_todo_done, seen_path=seen_path)
        made.append(s)
        return s, qpath
    yield _make
    for s in made:
        s.close()


# =============================================================================
# Part 2 -- read surface
# =============================================================================

def test_get_root_is_200_html(serve_queue):
    """AC #9."""
    s, _ = serve_queue([_rec(1)])
    status, headers, body = s.request("GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"Worksweep" in body


def test_unknown_path_is_404(serve_queue):
    """AC #9: only /, /approve, /approve-all exist."""
    s, _ = serve_queue([_rec(1)])
    for path in ("/nope", "/queue.json", "/../etc/passwd", "/approve/x", "/index.html"):
        status, _, body = s.request("GET", path)
        assert status == 404, path
        assert b"title 1" not in body, path


def test_wrong_method_on_a_known_path_is_404(serve_queue):
    """AC #9: a method a known path does not implement is 404 and renders nothing."""
    s, _ = serve_queue([_rec(1)])
    status, _, body = s.request("GET", "/approve")
    assert status == 404
    assert b"title 1" not in body
    status, _, body = s.request("GET", "/approve-all")
    assert status == 404
    status, _, body = s.request("POST", "/", "", {"X-Worksweep": "approve"})
    assert status == 404
    assert b"title 1" not in body


def test_sections_partition_every_record_exactly_once():
    """AC #10: five sections, each record in exactly one of them."""
    records = [
        _rec(1, status="proposed"),
        _rec(2, status="needs-input"),
        _rec(3, status="running"),
        _rec(4, status="approved"),
        _rec(5, status="done"),
        _rec(6, status="error"),
        _rec(7, status="running", executor="keep-current"),
        _rec(8, status="proposed", executor="keep-current"),
        _rec(9, status="done", executor="keep-current"),
    ]
    sections = dashboard.partition_sections(records)
    assert list(sections) == ["Needs you", "In progress", "Auto",
                             "Recently done", "Errors"]
    placed = [r.number for recs in sections.values() for r in recs]
    assert sorted(placed) == list(range(1, 10))
    assert len(placed) == len(set(placed))   # exactly one section each


def test_section_membership_is_by_status_then_executor():
    """AC #10: the exact bucket for every point in the status x executor space."""
    records = [
        _rec(1, status="proposed"),
        _rec(2, status="needs-input"),
        _rec(3, status="running"),
        _rec(4, status="approved"),
        _rec(5, status="done"),
        _rec(6, status="error"),
        _rec(7, status="running", executor="keep-current"),
        _rec(8, status="proposed", executor="keep-current"),
        _rec(9, status="done", executor="keep-current"),
    ]
    got = {name: sorted(r.number for r in recs)
           for name, recs in dashboard.partition_sections(records).items()}
    assert got == {
        # actionable wins over executor so every approvable record keeps its
        # checkbox (AC #26) -- including a keep-current item still proposed
        "Needs you": [1, 2, 8],
        "In progress": [3, 4],
        "Auto": [7],
        "Recently done": [5, 9],
        "Errors": [6],
    }


def test_unknown_status_is_surfaced_not_swallowed():
    """A hand-edited queue.json must not make a record vanish from the page."""
    got = {name: [r.number for r in recs] for name, recs
           in dashboard.partition_sections([_rec(1, status="banana")]).items()}
    assert got["Errors"] == [1]
    assert sum(len(v) for v in got.values()) == 1


def test_telemetry_header_carries_mtime_counts_and_done_this_week():
    """AC #10: header carries the queue mtime, per-status counts, week count."""
    records = [_rec(1, status="proposed"), _rec(2, status="proposed"),
               _rec(3, status="running"),
               QueueRecord(number=4, first_seen=T0, last_seen=_ago(2),
                           item=_rec(4, status="done").item)]
    page = _page(records, mtime=1_750_000_000.0)
    head = page[:page.index("Needs you")]
    assert "proposed 2" in head
    assert "running 1" in head
    assert "done 1" in head
    assert "done this week: 1" in head
    import datetime
    stamp = datetime.datetime.fromtimestamp(1_750_000_000.0).strftime("%Y-%m-%d %H:%M")
    assert stamp in head


def test_done_this_week_counts_six_days_and_not_eight():
    """AC #11 -- the exact window boundary."""
    six = QueueRecord(number=1, first_seen=T0, last_seen=_ago(6),
                      item=_rec(1, status="done").item)
    eight = QueueRecord(number=2, first_seen=T0, last_seen=_ago(8),
                        item=_rec(2, status="done").item)
    assert dashboard.done_this_week([six, eight], NOW) == 1
    assert dashboard.done_this_week([six], NOW) == 1
    assert dashboard.done_this_week([eight], NOW) == 0
    # a done record with an unparseable timestamp is not counted, never raises
    bad = QueueRecord(number=3, first_seen=T0, last_seen="not-a-date",
                      item=_rec(3, status="done").item)
    assert dashboard.done_this_week([bad], NOW) == 0


def test_recently_done_keeps_the_twenty_most_recent():
    """AC #10: last 20 done records by last_seen descending."""
    records = [QueueRecord(number=n, first_seen=T0, last_seen=_ago(30 - n),
                           item=_rec(n, status="done").item)
               for n in range(1, 26)]
    done = dashboard.partition_sections(records)["Recently done"]
    assert [r.number for r in done] == list(range(25, 5, -1))


def test_empty_queue_renders_the_all_clear_page(serve_queue):
    """AC #12."""
    page = _page([])
    assert "Nothing needs you right now" in page
    assert "<table" not in page
    for name in ("Needs you", "In progress", "Auto", "Recently done", "Errors"):
        assert name not in _markup(page)
    s, _ = serve_queue([])
    status, _, body = s.request("GET", "/")
    assert status == 200
    assert b"Nothing needs you right now" in body


def test_dashboard_escapes_titles():
    """AC #13 (falsifying): remove the html.escape call and this goes red."""
    page = _page([_rec(1, title="<script>alert(1)</script>")])
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert" not in page


def test_dashboard_escapes_whys_and_hrefs():
    """Decision 3 hardening: web_url rides into an ATTRIBUTE, so quote=True."""
    page = _page([_rec(1, why='<img src=x onerror="alert(1)">',
                       web_url='https://gl/x" onmouseover="alert(1)')])
    assert "<img src=x" not in page
    assert 'onmouseover="alert(1)' not in page
    assert "&quot;" in page


def test_get_performs_zero_writes_to_the_queue_file(serve_queue):
    """AC #14: read-only GET contract -- bytes and mtime both unchanged."""
    s, qpath = serve_queue([_rec(1), _rec(2, status="needs-input")])
    before_bytes = open(qpath, "rb").read()
    before_mtime = os.stat(qpath).st_mtime_ns
    _page(load_queue(qpath))
    assert s.request("GET", "/")[0] == 200
    assert open(qpath, "rb").read() == before_bytes
    assert os.stat(qpath).st_mtime_ns == before_mtime
    assert not os.path.exists(qpath + ".tmp")


@pytest.mark.parametrize("payload", [
    None,                       # file missing entirely
    "not json at all",
    '{"not": "a list"}',
    '[{"number": "x"}]',        # unparsable record
    "[]",
])
def test_malformed_queue_still_serves_200(tmp_path, payload):
    """AC #17: KeepAlive turns any raise into a crash loop."""
    qpath = os.path.join(str(tmp_path), "queue.json")
    if payload is not None:
        open(qpath, "w").write(payload)
    s = _Server(qpath)
    try:
        status, headers, body = s.request("GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"Worksweep" in body
    finally:
        s.close()


def test_render_page_is_pure_and_does_not_mutate_its_records():
    """AC #40: render_page is a pure function of its arguments."""
    records = [_rec(1), _rec(2, status="done")]
    snapshot = list(records)
    a = _page(records)
    b = _page(records)
    assert a == b
    assert records == snapshot


# --- bind resolution + CLI (AC #15) ------------------------------------------

class _Completed:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def test_resolve_bind_auto_uses_first_tailscale_address():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Completed(0, "100.64.0.5\nfd7a:115c::1\n")
    assert dashboard.resolve_bind("auto", run_subprocess=fake_run) == "100.64.0.5"
    assert calls == [["tailscale", "ip", "-4"]]


@pytest.mark.parametrize("fake", [
    lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("tailscale")),
    lambda cmd, **kw: _Completed(1, ""),
    lambda cmd, **kw: _Completed(0, ""),
    lambda cmd, **kw: _Completed(0, "   \n\n"),
    lambda cmd, **kw: (_ for _ in ()).throw(OSError("boom")),
])
def test_resolve_bind_auto_is_unresolved_not_loopback(fake):
    """F4 (supersedes the old loopback fallback): `auto` that cannot resolve
    returns "" so the caller RETRIES. Falling back to 127.0.0.1 would leave the
    dashboard silently unreachable from the phone until someone restarted it."""
    assert dashboard.resolve_bind("auto", run_subprocess=fake) == ""


def test_resolve_bind_tries_the_app_bundle_when_tailscale_is_not_on_path():
    """F4: under launchd the mini's PATH does not always have the CLI shim."""
    tried = []

    def fake(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] == "tailscale":
            raise FileNotFoundError("tailscale")
        return _Completed(0, "100.64.0.5\n")
    assert dashboard.resolve_bind("auto", run_subprocess=fake) == "100.64.0.5"
    assert tried == ["tailscale",
                     "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]


def test_resolve_bind_ignores_a_non_tailnet_address_from_tailscale():
    """F4: whatever tailscale says, only a tailnet address is acceptable."""
    assert dashboard.resolve_bind(
        "auto", run_subprocess=lambda cmd, **kw: _Completed(0, "192.168.1.5\n")) == ""


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.0.0.5", "::1", "100.64.0.5", "100.127.255.254"])
def test_resolve_bind_allows_loopback_and_tailnet(address):
    def boom(cmd, **kw):
        raise AssertionError("must not shell out for an explicit bind")
    assert dashboard.resolve_bind(address, run_subprocess=boom) == address
    assert dashboard.is_allowed_bind(address) is True


@pytest.mark.parametrize("address", [
    "0.0.0.0", "10.0.0.9", "192.168.1.5", "8.8.8.8", "100.63.255.255",
    "100.128.0.0", "not-an-ip", ""])
def test_resolve_bind_refuses_a_non_tailnet_explicit_bind(address):
    """F4 (falsifying): the dashboard has no auth and serves private MR titles.
    An explicit LAN bind or 0.0.0.0 must be a hard error, never a silent
    publish to the whole network."""
    if address == "":
        # "" is not an explicit bind -- it means auto
        assert dashboard.is_allowed_bind(address) is False
        return
    assert dashboard.is_allowed_bind(address) is False
    with pytest.raises(ValueError) as e:
        dashboard.resolve_bind(address, run_subprocess=lambda *a, **k: None)
    assert "refusing to bind" in str(e.value)


def test_cli_accepts_dashboard_with_port_and_bind_defaults(monkeypatch, tmp_path):
    """AC #15: `dashboard` in the positional choices, --port 8787, --bind auto."""
    import worksweep.__main__ as wsmain
    from worksweep.config import WorksweepConfig
    cfg = WorksweepConfig(repos=("pb-www",), username="c",
                          discord_webhook="https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    qpath = os.path.join(str(tmp_path), "queue.json")
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qpath)
    seen = {}

    def fake_serve(queue_path, port, bind, post=None, webhook="", **kw):
        seen.update(queue_path=queue_path, port=port, bind=bind,
                    post=post, webhook=webhook, sweep=kw.get("sweep"),
                    mark_todo_done=kw.get("mark_todo_done"))
        return 0
    monkeypatch.setattr(dashboard, "serve", fake_serve)

    assert wsmain.main(["dashboard"]) == 0
    assert seen["port"] == 8787
    assert seen["bind"] == "auto"
    assert seen["queue_path"] == qpath
    # the Discord poster arrives by INJECTION -- dashboard.py imports no __main__
    assert seen["post"] is wsmain._post_discord
    # the sweep edge is injected too -- dashboard.py never learns about launchctl
    assert seen["sweep"] is wsmain._kickstart_sweep
    assert seen["mark_todo_done"] is wsmain._mark_todo_done
    assert seen["webhook"] == cfg.discord_webhook

    assert wsmain.main(["dashboard", "--port", "9001", "--bind", "127.0.0.1"]) == 0
    assert seen["port"] == 9001
    assert seen["bind"] == "127.0.0.1"


def test_dashboard_module_never_imports_main():
    """The audit poster is injected; importing __main__ would be circular."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "dashboard.py")).read()
    assert "__main__" not in src
    assert "_post_discord" not in src


def test_dashboard_plist_contract():
    """AC #16: the committed launchd agent, checked against the runner plist."""
    import plistlib
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "etc", "mini",
                        "com.chandlerhardy.worksweep-dashboard.plist")
    with open(path, "rb") as f:
        pl = plistlib.load(f)
    with open(os.path.join(root, "etc", "mini",
                           "com.chandlerhardy.worksweep-runner.plist"), "rb") as f:
        runner = plistlib.load(f)

    assert pl["Label"] == "com.chandlerhardy.worksweep-dashboard"
    assert pl["KeepAlive"] is True
    assert pl["RunAtLoad"] is True
    assert "StartInterval" not in pl          # not a periodic job
    assert pl["ProgramArguments"][-1] == "dashboard"
    assert pl["ProgramArguments"][1].endswith("bin/worksweep.sh")
    assert pl["EnvironmentVariables"]["PATH"] == runner["EnvironmentVariables"]["PATH"]
    assert pl["EnvironmentVariables"]["HOME"] == runner["EnvironmentVariables"]["HOME"]
    assert pl["StandardOutPath"].endswith("heartbeat-reports/worksweep-dashboard.log")
    assert pl["StandardErrorPath"].endswith("heartbeat-reports/worksweep-dashboard.err")


# =============================================================================
# Part 3 -- write surface
# =============================================================================

def test_post_approve_flips_selected_proposed_and_needs_input(serve_queue):
    """AC #18: the numbered route keeps the _APPROVABLE semantics."""
    s, qpath = serve_queue([_rec(1, status="proposed"),
                            _rec(2, status="needs-input"),
                            _rec(3, status="proposed"),
                            _rec(4, status="running"),
                            _rec(5, status="done")])
    status, _, body = s.approve([1, 2, 4, 5, 99])
    assert status == 200
    assert json.loads(body)["approved"] == [1, 2]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "approved", 3: "proposed",
                   4: "running", 5: "done"}


def test_post_approve_ignores_unmatched_numbers_without_erroring(serve_queue):
    """AC #18: a number matching no record is a no-op, not a 400."""
    s, qpath = serve_queue([_rec(1)])
    status, _, body = s.approve([404, 999])
    assert status == 200
    assert json.loads(body)["approved"] == []
    assert {r.number: r.item.status for r in load_queue(qpath)} == {1: "proposed"}


def test_post_approve_all_is_proposed_only(serve_queue):
    """AC #19 (falsifying): point the route at _APPROVABLE and this goes red."""
    s, qpath = serve_queue([_rec(1, status="proposed"),
                            _rec(2, status="needs-input"),
                            _rec(3, status="proposed"),
                            _rec(4, status="running"),
                            _rec(5, status="approved"),
                            _rec(6, status="done"),
                            _rec(7, status="error")])
    status, _, body = s.approve_all([1, 2, 3, 4, 5, 6, 7])
    assert status == 200
    assert json.loads(body)["approved"] == [1, 3]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "needs-input", 3: "approved",
                   4: "running", 5: "approved", 6: "done", 7: "error"}


def test_post_approve_all_skips_non_runnable_executors(serve_queue):
    """F1 (falsifying): a blanket-approved triage/mr-hygiene/none record is a
    permanently-stuck zombie -- nothing claims it and there is no un-approve."""
    s, qpath = serve_queue([_rec(1, executor="magi-review"),
                            _rec(2, executor="keep-current"),
                            _rec(3, executor="implement"),
                            _rec(4, executor="triage"),
                            _rec(5, executor="mr-hygiene"),
                            _rec(6, executor="none")])
    status, _, body = s.approve_all([1, 2, 3, 4, 5, 6])
    assert status == 200
    assert json.loads(body)["approved"] == [1, 2, 3]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "approved", 3: "approved",
                   4: "proposed", 5: "proposed", 6: "proposed"}


def test_post_approve_all_only_flips_the_numbers_the_page_rendered(serve_queue):
    """F2 (falsifying): an item that landed between render and tap was never
    shown to the user, so it must not be swept in by their tap."""
    s, qpath = serve_queue([_rec(1), _rec(2), _rec(3)])
    # the page the user is looking at rendered only 1 and 2
    status, _, body = s.approve_all([1, 2])
    assert status == 200
    assert json.loads(body)["approved"] == [1, 2]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "approved", 3: "proposed"}


def test_post_approve_all_intersects_with_current_eligibility(serve_queue):
    """F2: the client's set is a scope, never an authority -- the server
    re-checks every number against fresh disk state."""
    s, qpath = serve_queue([_rec(1), _rec(2, status="running"),
                            _rec(3, executor="triage"), _rec(4)])
    status, _, body = s.approve_all([1, 2, 3, 99])
    assert status == 200
    assert json.loads(body)["approved"] == [1]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "running", 3: "proposed", 4: "proposed"}


def test_dashboard_holds_no_status_rules_of_its_own():
    """AC #20: the status rules live in exactly one place -- approvals.py."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "dashboard.py")).read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "dataclasses.replace" not in code
    assert re.search(r"status\s*=\s*[\"']", code) is None
    # no queue-status TUPLE anywhere: the approvable set is imported, never re-declared
    assert re.search(
        r"\(\s*[\"'](?:proposed|needs-input|approved|running|done|error)[\"']\s*,",
        code) is None
    assert "_APPROVABLE" in code            # imported from approvals
    assert "approve_numbers" in code and "approve_all" in code


def test_post_reloads_the_queue_from_disk_before_flipping(serve_queue):
    """AC #21 (falsifying): flip a cached render and the runner's newer claim is
    resurrected as `proposed` by save_queue's whole-file replace."""
    s, qpath = serve_queue([_rec(1, status="proposed"), _rec(2, status="proposed")])
    assert s.request("GET", "/")[0] == 200          # the page the user is looking at

    # ... meanwhile the runner claims #2 and the sweep adds #3
    save_queue(qpath, [_rec(1, status="proposed"), _rec(2, status="running"),
                       _rec(3, status="proposed")])

    assert s.approve([1])[0] == 200
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "running", 3: "proposed"}


def test_post_writes_through_the_atomic_save_queue(serve_queue):
    """AC #21: no bespoke write mechanics -- the temp file is always renamed away."""
    s, qpath = serve_queue([_rec(1)])
    assert s.approve([1])[0] == 200
    assert not os.path.exists(qpath + ".tmp")
    assert json.loads(open(qpath).read())[0]["item"]["status"] == "approved"


def test_post_without_custom_header_is_403(serve_queue):
    """AC #22 (falsifying): delete the header check and this goes red on BOTH
    the status code and the queue bytes."""
    s, qpath = serve_queue([_rec(1), _rec(2)])
    before = open(qpath, "rb").read()
    for path, body in (("/approve", json.dumps({"numbers": [1]})),
                       ("/approve-all", "")):
        status, _, _ = s.request("POST", path, body,
                                 {"Content-Type": "application/json"})
        assert status == 403, path
        assert open(qpath, "rb").read() == before, path
    # an empty header value is not a header
    status, _, _ = s.request("POST", "/approve", json.dumps({"numbers": [1]}),
                             {"X-Worksweep": ""})
    assert status == 403
    assert open(qpath, "rb").read() == before


def test_post_with_mismatched_origin_is_403(serve_queue):
    """AC #23: a PRESENT Origin that disagrees with Host is rejected."""
    s, qpath = serve_queue([_rec(1)])
    before = open(qpath, "rb").read()
    for origin in ("http://evil.example", "https://127.0.0.1:1",
                   "http://127.0.0.1"):
        status, _, _ = s.approve([1], headers={"Origin": origin})
        assert status == 403, origin
        assert open(qpath, "rb").read() == before, origin


def test_post_with_absent_origin_is_processed(serve_queue):
    """AC #23: same-origin fetch on a plain page may omit Origin entirely."""
    s, qpath = serve_queue([_rec(1)])
    status, _, _ = s.approve([1])
    assert status == 200
    assert load_queue(qpath)[0].item.status == "approved"


def test_post_with_matching_origin_is_processed(serve_queue):
    s, qpath = serve_queue([_rec(1)])
    status, _, _ = s.approve([1], headers={"Origin": f"http://{s.host}:{s.port}"})
    assert status == 200
    assert load_queue(qpath)[0].item.status == "approved"


def test_server_answers_no_preflight_and_sends_no_cors_headers(serve_queue):
    """The custom-header defense only works while the server refuses preflight."""
    s, _ = serve_queue([_rec(1)])
    status, headers, _ = s.request("OPTIONS", "/approve")
    assert status in (404, 501)
    for _, hdrs, _ in (s.request("GET", "/"), s.approve([1])):
        assert not [k for k in hdrs if k.lower().startswith("access-control-")]


@pytest.mark.parametrize("body", [
    "", None, "not json", "[]", '"numbers"', "{}",
    '{"numbers": "1,2"}', '{"numbers": {"a": 1}}', '{"numbers": [1, "2"]}',
    '{"numbers": [1, null]}', '{"numbers": [1, 2.5]}', '{"numbers": [true]}',
])
def test_malformed_post_body_is_400_and_persists_nothing(serve_queue, body):
    """AC #34: reject the malformed envelope without rejecting unmatched numbers."""
    s, qpath = serve_queue([_rec(1)])
    before = open(qpath, "rb").read()
    status, _, _ = s.request("POST", "/approve", body,
                             {"X-Worksweep": "approve",
                              "Content-Type": "application/json"})
    assert status == 400
    assert open(qpath, "rb").read() == before


@pytest.mark.parametrize("body", ["", None, "not json", '{"numbers": "1"}'])
def test_approve_all_rejects_a_malformed_body(serve_queue, body):
    """F2 supersedes the old no-body contract: /approve-all now carries the
    client's rendered numbers, so a missing envelope is a 400, not a blanket."""
    s, qpath = serve_queue([_rec(1)])
    before = open(qpath, "rb").read()
    status, _, _ = s.request("POST", "/approve-all", body,
                             {"X-Worksweep": "approve",
                              "Content-Type": "application/json"})
    assert status == 400
    assert open(qpath, "rb").read() == before


def test_dashboard_approval_posts_one_discord_confirmation(serve_queue):
    """AC #24: never-silent -- the channel stays the single history."""
    posted = []
    s, _ = serve_queue([_rec(1), _rec(2), _rec(3, status="running")],
                       post=lambda hook, content: posted.append((hook, content)),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve([1, 2])[0] == 200
    assert len(posted) == 1
    hook, content = posted[0]
    assert hook == "https://discord.com/api/webhooks/1/x"
    assert "(dashboard)" in content
    assert "1 (magi-review pb-www)" in content
    assert "2 (magi-review pb-www)" in content
    assert "3 (" not in content


def test_approve_all_posts_its_own_confirmation(serve_queue):
    posted = []
    s, _ = serve_queue([_rec(1), _rec(2, status="needs-input")],
                       post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve_all([1, 2])[0] == 200
    assert len(posted) == 1
    assert "(dashboard)" in posted[0] and "1 (" in posted[0]
    assert "2 (" not in posted[0]


def test_no_confirmation_when_nothing_flipped(serve_queue):
    """AC #24 fires only when an approval PERSISTS."""
    posted = []
    s, _ = serve_queue([_rec(1, status="running")],
                       post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve([1])[0] == 200
    assert s.approve_all([1])[0] == 200
    assert posted == []


def test_discord_failure_does_not_fail_or_roll_back_the_approval(serve_queue, capsys):
    """AC #25: the write is durable BEFORE the post is attempted."""
    def boom(hook, content):
        raise RuntimeError("discord post failed: 500")
    s, qpath = serve_queue([_rec(1)], post=boom,
                           webhook="https://discord.com/api/webhooks/1/x")
    status, _, _ = s.approve([1])
    assert status == 200
    assert load_queue(qpath)[0].item.status == "approved"


# =============================================================================
# Part 3/4 -- UI contract
# =============================================================================

def test_checkbox_for_every_actionable_record_with_44px_targets():
    """AC #26."""
    page = _page([_rec(1, status="proposed"), _rec(2, status="needs-input"),
                  _rec(3, status="running"), _rec(4, status="done"),
                  _rec(5, status="error"), _rec(6, status="approved")])
    assert sorted(_checkboxes(page, "sections")) == [1, 2]
    body = _rule(_style(page), ".check")
    assert body is not None, "no .check rule"
    assert re.search(r"min-width:\s*(\d+)px", body).group(1) == "44"
    assert re.search(r"min-height:\s*(\d+)px", body).group(1) == "44"


def test_sticky_bottom_bar_holds_exactly_the_two_buttons():
    """AC #26."""
    page = _page([_rec(1)])
    body = _rule(_style(page), ".bar")
    assert body is not None
    assert re.search(r"position:\s*(sticky|fixed)", body)
    bar = page[page.index('class="bar"'):]
    bar = bar[:bar.index("</div>")]
    labels = re.findall(r"<button[^>]*>(.*?)</button>", bar, re.S)
    assert len(labels) == 2
    assert "Approve selected" in labels[0]
    assert "Approve all" in labels[1]


def test_approve_all_confirms_with_the_count_of_the_set_it_sends():
    """No un-approve path exists, so the bulk action must not be a stray tap.

    F2: the count and the POSTed numbers both come from the rendered
    `data-blanket` rows, so they cannot disagree. A server-rendered count would
    go stale and tell the user they were approving N while sending a different
    set -- so it must NOT be in the markup at all.
    """
    page = _page([_rec(1), _rec(2), _rec(3, status="needs-input"),
                  _rec(4, status="running"), _rec(5, executor="triage")])
    assert "data-proposed-count" not in page
    assert re.search(r"confirm\(\s*['\"]Approve all", page)
    assert "proposed items?" in page
    # exactly the proposed AND runnable rows are marked sweepable
    blanket = [int(re.search(r'value="(\d+)"', t).group(1))
               for t in re.findall(r"<input[^>]*>", page)
               if 'data-blanket="1"' in t and 'data-view="sections"' in t]
    assert sorted(blanket) == [1, 2]
    # and the button sends that set, not a server-side notion of "all"
    assert "send('/approve-all',{numbers:n})" in _script(page).replace(" ", "")


def test_desktop_media_query_declares_a_panel_grid_with_gap():
    """AC #27."""
    block = _block(_style(_page([_rec(1)])), "@media (min-width: 900px)")
    assert re.search(r"display:\s*(grid|flex)", block)
    gap = re.search(r"gap:\s*([\d.]+)(px|rem|em)", block)
    assert gap and float(gap.group(1)) > 0


def test_media_query_never_overrides_a_stored_layout():
    """AC #30 (falsifying): every rule inside the breakpoint must be scoped by
    the data-layout attribute, or the toggle is overridden at one width."""
    block = _block(_style(_page([_rec(1)])), "@media (min-width: 900px)")
    selectors = _selectors(block)
    assert selectors
    for sel in selectors:
        assert "[data-layout=" in sel, f"unscoped rule inside the breakpoint: {sel}"


def test_page_is_self_contained():
    """AC #28: zero external assets."""
    page = _page([_rec(1)])
    assert "<script src=" not in page
    # Self-contained still allows a data:-URI favicon -- the ban is on
    # anything the browser would fetch over the network.
    for m in re.finditer(r"<link[^>]*>", page):
        tag = m.group(0)
        ok_favicon = 'rel="icon"' in tag and 'href="data:' in tag
        ok_touch = ('rel="apple-touch-icon"' in tag
                    and 'href="/apple-touch-icon.png"' in tag)
        assert ok_favicon or ok_touch, tag
    css = _style(page)
    assert "@import" not in css
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", css)
    for m in re.finditer(r'\bsrc\s*=\s*"([^"]*)"', page):
        assert not m.group(1).lower().startswith(("http", "//"))
    # the only http-scheme URLs on the page are the queue's own web_url links
    for m in re.finditer(r'<(\w+)[^>]*\shref="(https?://[^"]*)"', page):
        assert m.group(1) == "a", m.group(0)


def test_layout_toggle_offers_exactly_three_views():
    """AC #29 and AC #35: exactly three views, in switcher order."""
    page = _page([_rec(1)])
    views = re.findall(r'data-set-layout="([a-z]+)"', page)
    assert views == ["checklist", "panels", "branches"]
    css = _style(page)
    for view in views:
        assert f'[data-layout="{view}"]' in css


def test_layout_is_restored_from_localstorage_before_the_first_section():
    """AC #31: the restore script must run BEFORE any section renders, or the
    60s auto-refresh flashes the wrong layout every minute."""
    page = _page([_rec(1)])
    restore = page.index("localStorage.getItem")
    assert restore < page.index("<body")
    assert restore < page.index("Needs you")
    assert 'data-layout=' in page[:page.index("<head")]     # server-side initial
    assert "localStorage.setItem" in page
    # F7: the meta refresh is gone -- it could reload mid-POST or discard a
    # selection. Nothing reloads the page at all now; a JS timer polls and
    # swaps the changed regions in place.
    assert "http-equiv=\"refresh\"" not in page
    assert "setTimeout(poll," in _script(page).replace(" ", "")
    # AC #35: `branches` persists exactly like the other two -- the restore
    # script must accept all three stored values, not just the original pair
    restore_src = page[restore - 400:page.index("</script>", restore)]
    for view in ("checklist", "panels", "branches"):
        assert f"'{view}'" in restore_src, view


def test_layout_state_never_rides_in_the_url():
    """AC #32."""
    page = _page([_rec(1)])
    js = _script(page)
    assert "pushState" not in js
    assert "location.search" not in js
    assert "location.href" not in js
    for tag in re.findall(r"<a[^>]*>", page):
        assert "layout" not in tag


def test_query_string_renders_identically_to_bare_root(serve_queue):
    """AC #32: /?layout=panels is not a layout selector."""
    s, _ = serve_queue([_rec(1)])
    a = s.request("GET", "/")
    b = s.request("GET", "/?layout=panels")
    assert a[0] == b[0] == 200
    assert a[2] == b[2]


def test_palette_is_one_root_block_and_nothing_else_names_a_colour():
    """AC #33."""
    css = _style(_page([_rec(1)]))
    assert len(re.findall(r"(?<![\w-]):root\s*\{", css)) == 1
    root = _block(css, ":root")
    assert len(re.findall(r"--[\w-]+:", root)) >= 6
    rest = css.replace(root, "")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", rest)
    assert not re.findall(r"\b(?:rgba?|hsla?)\(", rest)
    assert not re.findall(r":\s*(?:white|black|red|green|blue)\b", rest)


def test_every_control_has_hover_and_active_states():
    """AC #33."""
    css = _style(_page([_rec(1)]))
    for control in (".bar .btn", ".toggle-btn"):
        assert _rule(css, f"{control}:hover") is not None, f"{control}:hover"
        assert _rule(css, f"{control}:active") is not None, f"{control}:active"


# =============================================================================
# Part 5 -- Branches view
# =============================================================================

def _br(n, branch="", mr_iid=0, web_url="", repo="pb-www", **kw):
    return _rec(n, branch=branch, mr_iid=mr_iid, web_url=web_url, repo=repo, **kw)


def test_mr_iid_of_reads_the_web_url_then_falls_back_to_the_field():
    """AC #37 / S12: copy keepcurrent.iid_of's regex, NOT its raise."""
    assert dashboard.mr_iid_of(
        _br(1, web_url="https://gl/g/pb-www/-/merge_requests/4821").item) == 4821
    assert dashboard.mr_iid_of(_br(1, web_url="", mr_iid=4830).item) == 4830
    # the url wins when both are present
    assert dashboard.mr_iid_of(
        _br(1, web_url="https://gl/g/pb-www/-/merge_requests/1", mr_iid=9).item) == 1


@pytest.mark.parametrize("item_kw", [
    dict(web_url="", mr_iid=0),
    dict(web_url="https://gl/g/pb-www/-/issues/1588", mr_iid=0),
    dict(web_url="https://gl/dashboard/todos", mr_iid=0),
    dict(web_url="not a url", mr_iid=0),
])
def test_mr_iid_of_returns_zero_and_never_raises(item_kw):
    """AC #37: keepcurrent.iid_of RAISES here -- under KeepAlive that is a
    crash loop the moment the queue holds an issue or todo record."""
    assert dashboard.mr_iid_of(_br(1, **item_kw).item) == 0


def test_records_sharing_a_branch_land_in_one_card():
    """AC #36."""
    recs = [_br(1, branch="chardy/1588-x"), _br(2, branch="chardy/1588-x"),
            _br(3, branch="chardy/other")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    assert [sorted(r.number for r in g.records) for g in groups] == [[1, 2], [3]]
    assert groups[0].title == "chardy/1588-x"


def test_mr_ref_and_branch_are_two_edges_of_one_equivalence():
    """AC #36 (falsifying): bucket by item.branch alone and this splits into
    two cards instead of one."""
    recs = [
        _br(1, branch="chardy/1588-x"),                       # branch token only
        _br(2, branch="chardy/1588-x",
            web_url="https://gl/g/pb-www/-/merge_requests/4821"),   # BOTH tokens
        _br(3, web_url="https://gl/g/pb-www/-/merge_requests/4821"),  # mr token only
    ]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    assert len(groups) == 1
    assert sorted(r.number for r in groups[0].records) == [1, 2, 3]


def test_mr_affinity_is_scoped_by_repo():
    """AC #36: iids are per-project -- pb-www!4821 is not pb-api!4821."""
    recs = [_br(1, repo="pb-www", web_url="https://gl/g/pb-www/-/merge_requests/4821"),
            _br(2, repo="pb-api", web_url="https://gl/g/pb-api/-/merge_requests/4821")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert [sorted(r.number for r in g.records) for g in groups] == [[1], [2]]


def test_records_with_no_affinity_go_ungrouped():
    """AC #37."""
    recs = [_br(1, branch="chardy/x"),
            _br(2, web_url="https://gl/g/pb-www/-/issues/1588"),
            _br(3, web_url="", mr_iid=0)]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert [r.number for r in groups[0].records] == [1]
    assert sorted(r.number for r in ungrouped) == [2, 3]


def test_cards_are_ordered_deterministically_with_ungrouped_last():
    """AC #37: a 60s auto-refresh must not reshuffle the page under a thumb."""
    recs = [_br(9, branch="zeta"), _br(2, branch="alpha"),
            _br(5, web_url=""), _br(3, branch="zeta")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert [g.title for g in groups] == ["alpha", "zeta"]     # by lowest number
    assert [r.number for r in ungrouped] == [5]
    page = _page(recs)
    assert page.index("alpha") < page.index("zeta") < page.index("Ungrouped")


def test_card_header_links_the_mr_and_every_issue():
    """AC #38."""
    recs = [_br(1, branch="chardy/1588-x",
                web_url="https://gl/g/pb-www/-/merge_requests/4821"),
            _br(2, branch="chardy/1588-x",
                web_url="https://gl/g/pb-www/-/issues/1588"),
            _br(3, branch="chardy/1588-x",
                web_url="https://gl/g/pb-www/-/issues/1601")]
    groups, _ = dashboard.group_by_workstream(recs)
    g = groups[0]
    assert g.title == "chardy/1588-x"
    assert g.mr_links == ("https://gl/g/pb-www/-/merge_requests/4821",)
    assert g.issue_links == ("https://gl/g/pb-www/-/issues/1588",
                             "https://gl/g/pb-www/-/issues/1601")
    assert g.bare_mr_refs == ()
    page = _page(recs)
    assert '<a href="https://gl/g/pb-www/-/merge_requests/4821"' in page
    assert ">!4821<" in page
    assert ">#1588<" in page and ">#1601<" in page


def test_a_bare_mr_iid_renders_as_unlinked_text():
    """AC #38: never invent a URL from repo + iid."""
    recs = [_br(1, branch="chardy/x", mr_iid=4830, web_url="")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert groups[0].mr_links == ()
    assert groups[0].bare_mr_refs == (4830,)
    page = _page(recs)
    assert "!4830" in page
    assert "merge_requests/4830" not in page
    assert 'href="!4830"' not in page


def test_card_is_named_after_a_human_title_not_an_iid():
    """Addendum 4: most records carry no branch, so naming cards after the ref
    alone left the Branches view reading "!4821" instead of what the work IS.
    An MR's title wins over an issue's -- it describes the change under way."""
    recs = [_br(1, web_url="https://gl/g/pb-www/-/merge_requests/4821",
                kind="issue", title="Ranch data: investigate"),
            _br(2, web_url="https://gl/g/pb-www/-/merge_requests/4821",
                kind="mr", title="fix(yardage): authorize the yardage endpoint")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert groups[0].title == "fix(yardage): authorize the yardage endpoint"
    # the refs stay in the header as secondary metadata, not as the name
    assert groups[0].mr_links == ("https://gl/g/pb-www/-/merge_requests/4821",)
    page = _page(recs)
    assert ">fix(yardage): authorize the yardage endpoint<" in page
    assert ">!4821<" in page


def test_a_branch_name_still_wins_over_a_title():
    recs = [_br(1, branch="chardy/1588-x", title="some MR title", kind="mr")]
    assert dashboard.group_by_workstream(recs)[0][0].title == "chardy/1588-x"


def test_long_card_titles_are_truncated():
    long_title = "fix(yardage): " + "authorize the yardage endpoint " * 5
    recs = [_br(1, kind="mr", title=long_title,
                web_url="https://gl/g/pb-www/-/merge_requests/4821")]
    title = dashboard.group_by_workstream(recs)[0][0].title
    assert len(title) <= 60
    assert title.endswith("…")


def test_card_title_falls_back_to_the_mr_ref_when_nothing_is_named():
    """AC #38: with no branch AND no titles at all, the ref is all there is."""
    recs = [_br(1, title="", web_url="https://gl/g/pb-www/-/merge_requests/4821")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert groups[0].title == "!4821"


def test_branch_rows_carry_number_executor_status_and_why():
    """AC #38: compact rows."""
    recs = [_br(1, branch="chardy/x", status="needs-input",
                executor="implement", why="awaiting answer")]
    page = _page(recs)
    card = page[page.index('class="card"'):]
    assert "#1" in card and "implement" in card
    assert "needs-input" in card and "awaiting answer" in card


def test_branches_view_keeps_the_checkboxes_and_the_bar():
    """AC #39: approval controls stay functional inside the Branches view."""
    recs = [_br(1, branch="b1", status="proposed"),
            _br(2, branch="b1", status="needs-input"),
            _br(3, branch="b1", status="running"),
            _br(4, web_url="", status="proposed")]
    page = _page(recs)
    assert sorted(_checkboxes(page, "branches")) == [1, 2, 4]
    assert sorted(_checkboxes(page, "sections")) == [1, 2, 4]
    assert 'class="bar"' in page
    assert "/approve-all" in page and "/approve'" in page


def test_grouping_is_pure_and_makes_no_network_call():
    """AC #40: affinity comes from branch/mr_iid/web_url/repo alone.

    Pinned as an EXACT import allowlist rather than a substring scan: this is
    the whole dependency surface of the module, so any future network import
    (or an import of the CLI, keepcurrent or runner) breaks the test.
    """
    assert _imported_modules(_dashboard_tree()) == {
        "datetime", "html", "ipaddress", "json", "os",
        "pathlib", "re", "subprocess",
        "sys", "threading", "time",
        "urllib.parse",          # pure string parsing, not network
        "dataclasses", "http.server", "typing", "__future__",
        ".approvals", ".formatter", ".models", ".queue",
        # 2026-08-28: dismissing a feedback row records which notes it covered.
        # Pure file I/O in ~/.worksweep, no network -- but the allowlist is
        # exact on purpose, so it is listed rather than waved through.
        ".seennotes",
    }
    recs = [_br(1, branch="b"), _br(2, web_url="")]
    snapshot = list(recs)
    a = dashboard.group_by_workstream(recs)
    b = dashboard.group_by_workstream(recs)
    assert a == b
    assert recs == snapshot


def test_dashboard_does_not_call_the_raising_iid_helpers():
    """AC #37: keepcurrent.iid_of / runner._iid_of raise BY DESIGN. Neither is
    imported nor called here -- under KeepAlive a raise is a crash loop."""
    tree = _dashboard_tree()
    mods = _imported_modules(tree)
    assert ".keepcurrent" not in mods
    assert ".runner" not in mods
    called = _called_names(tree)
    assert "keepcurrent.iid_of" not in called
    assert "runner._iid_of" not in called
    assert "iid_of" not in called          # no bare re-export either
    # the tolerant local helper is what the module actually defines
    assert any(isinstance(n, ast.FunctionDef) and n.name == "mr_iid_of"
               for n in ast.walk(tree))


# =============================================================================
# Fix Mode: Attempt 1 -- review findings
# =============================================================================

def test_non_runnable_actionable_rows_get_dismiss_not_a_checkbox():
    """F1 + addendum 1: nothing claims triage/mr-hygiene/none, so an approve
    control would strand them. Their resolution is Dismiss instead."""
    page = _page([_rec(1, executor="magi-review"),
                  _rec(2, executor="keep-current"),
                  _rec(3, executor="implement"),
                  _rec(4, executor="triage"),
                  _rec(5, executor="mr-hygiene", status="needs-input"),
                  _rec(6, executor="none")])
    assert sorted(_checkboxes(page, "sections")) == [1, 2, 3]
    assert sorted(_checkboxes(page, "branches")) == [1, 2, 3]
    # a Dismiss button for each non-runnable row, in each of the two row views
    dismissable = sorted(int(m) for m in
                         re.findall(r'data-dismiss="(\d+)"', page))
    assert dismissable == [4, 4, 5, 5, 6, 6]
    for n in ("#4", "#5", "#6"):                   # still visible on the page
        assert n in page
    body = _rule(_style(page), ".btn-dismiss")
    assert body is not None
    assert re.search(r"min-height:\s*44px", body)   # keeps the row rhythm


def test_needs_input_is_selectable_but_never_blanket_sweepable():
    """F1 + decision 1: a checked needs-input box is a deliberate human 'go
    again'; a blanket sweep must never release it."""
    page = _page([_rec(1, status="proposed"), _rec(2, status="needs-input")])
    tags = [t for t in re.findall(r"<input[^>]*>", page)
            if 'data-view="sections"' in t]
    by_value = {int(re.search(r'value="(\d+)"', t).group(1)): t for t in tags}
    assert 'data-blanket="1"' in by_value[1]
    assert 'data-blanket="1"' not in by_value[2]


class _Explosive:
    """A record whose item raises on every attribute read."""
    number = 7

    @property
    def item(self):
        raise RuntimeError("corrupt record")


def test_one_unrenderable_record_costs_one_row_not_the_page(capsys):
    """F5: a hand-edited or half-typed record must not blank the dashboard --
    under KeepAlive a blank page gives the human nothing to act on."""
    good = _rec(1, title="still here")
    page = dashboard.render_page([good, _Explosive()], NOW, 1_750_000_000.0)
    assert "still here" in page
    assert "unrenderable" in page
    assert "#7" in page
    assert "Approve selected" in page          # the bar still renders
    assert "corrupt record" in capsys.readouterr().err


def test_tolerant_sorts_survive_junk_timestamps_and_numbers():
    """F5: sort keys are coerced, so a junk last_seen cannot raise out of a
    sort and take the whole page with it."""
    recs = [QueueRecord(number=1, first_seen=T0, last_seen=None,
                        item=_rec(1, status="done").item),
            QueueRecord(number=2, first_seen=T0, last_seen=_ago(1),
                        item=_rec(2, status="done").item)]
    done = dashboard.partition_sections(recs)["Recently done"]
    assert sorted(r.number for r in done) == [1, 2]
    assert "Recently done" in dashboard.render_page(recs, NOW, None)


def test_page_encodes_undecodable_content_without_raising(serve_queue):
    """F5: the final encode uses errors='replace'."""
    s, _ = serve_queue([_rec(1, title="lone surrogate: \ud800 tail")])
    status, headers, body = s.request("GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"tail" in body


def test_audit_post_is_clamped_under_the_discord_cap():
    """F6: 'Approve all' is the highest-blast-radius action in worksweep, so it
    must ALWAYS leave a channel record -- an over-long message would be
    rejected and the audit trail would vanish for exactly that approval."""
    from worksweep.formatter import DISCORD_MAX_CHARS
    records = [_rec(n, repo="pb-www-with-a-long-name",
                    executor="magi-review") for n in range(1, 301)]
    msg = dashboard._audit_message(list(range(1, 301)), records)
    assert len(msg.encode("utf-8")) <= DISCORD_MAX_CHARS
    assert msg.startswith("✅ Approved: 1 (")
    assert "(dashboard)" in msg
    more = re.search(r"\(\+(\d+) more\)", msg)
    assert more and int(more.group(1)) > 0
    # the summary accounts for every approved item
    named = len(re.findall(r"\d+ \(magi-review", msg))
    assert named + int(more.group(1)) == 300


def test_short_audit_post_is_not_truncated():
    records = [_rec(1), _rec(2)]
    msg = dashboard._audit_message([1, 2], records)
    assert msg == ("✅ Approved: 1 (magi-review pb-www), "
                   "2 (magi-review pb-www) (dashboard)")
    assert "more)" not in msg


def test_concurrent_posts_do_not_lose_an_approval(serve_queue):
    """F3 (falsifying): ThreadingHTTPServer runs each request on its own
    thread, so without the module lock two taps interleave their
    load->flip->save and one whole set is silently lost."""
    s, qpath = serve_queue([_rec(n) for n in range(1, 21)])
    results = []

    def approve(numbers):
        results.append(s.approve(numbers)[0])

    threads = [threading.Thread(target=approve, args=(list(range(1, 11)),)),
               threading.Thread(target=approve, args=(list(range(11, 21)),))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == [200, 200]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {n: "approved" for n in range(1, 21)}


def test_log_lines_strip_control_bytes():
    """F9: the request line is attacker-controlled and lands in a file a human
    later tails -- raw \\r or \\x1b could forge log lines or drive a terminal."""
    h = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
    h.client_address = ("127.0.0.1", 5555)
    line = h._log_line('"%s" 200', ('GET /a\x00\x1b[2J\rFAKE LOG LINE b',))
    assert "\x00" not in line and "\x1b" not in line and "\r" not in line
    assert "\n" not in line
    assert "FAKE LOG LINE" in line          # kept as visible text, defanged
    assert line.startswith("worksweep-dashboard: 127.0.0.1 - ")


def test_log_error_is_sanitized_too():
    """F9: BaseHTTPRequestHandler routes malformed-request errors to log_error,
    which would otherwise echo the raw request line to stderr."""
    assert "log_error" in _dashboard_src()
    h = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
    h.client_address = ("127.0.0.1", 5555)
    assert "\x1b" not in h._log_line("bad request \x1b[2J %s", ("x",))


def test_every_attribute_interpolation_is_escaped():
    """F9: no raw f-string value may land inside a quoted attribute."""
    src = _dashboard_src()
    # every `="{...}"` interpolation in an HTML attribute goes through _e(
    for m in re.finditer(r'="\{([^}]+)\}"', src):
        expr = m.group(1)
        assert expr.startswith("_e(") or expr.startswith("int("), expr


def test_serve_retries_resolution_and_bind_then_serves(monkeypatch):
    """F4: the agent starts at boot before tailscaled has an address, and the
    port is briefly held after a restart. Both are transient, so serve() retries
    in-process instead of crash-looping under KeepAlive."""
    calls = {"bind": 0}
    probed = []
    unresolved_attempts = [2]        # first two resolutions find nothing
    slept = []

    def fake_run(cmd, **kw):
        probed.append(cmd[0])
        if unresolved_attempts[0] > 0:
            # a full resolution probes BOTH binaries before giving up, so
            # decrement only when the app-bundle fallback has also been tried
            if cmd[0] != "tailscale":
                unresolved_attempts[0] -= 1
            return _Completed(0, "")
        return _Completed(0, "100.64.0.5\n")

    class _Fake:
        def serve_forever(self, *a, **k):
            raise KeyboardInterrupt
        def server_close(self):
            pass

    def fake_make(addr, qpath, **kw):
        calls["bind"] += 1
        assert addr == ("100.64.0.5", 8787)
        if calls["bind"] < 2:
            raise OSError("address already in use")
        return _Fake()

    monkeypatch.setattr(dashboard, "make_server", fake_make)
    # max_attempts bounds the loop: without it a regression that never
    # resolves would spin this test forever instead of failing it
    rc = dashboard.serve("/tmp/q.json", bind="auto", run_subprocess=fake_run,
                         sleep=slept.append, max_attempts=10)
    assert rc == 0
    assert calls["bind"] == 2         # first bind raised, second succeeded
    # two unresolved attempts (each probing both binaries) + one failed bind
    assert slept == [30, 30, 30]      # 30s backoff between every attempt
    # the PATH binary is tried first, the app bundle only as a fallback
    assert probed[:2] == ["tailscale",
                          "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]


def test_serve_refuses_a_bad_explicit_bind_immediately(monkeypatch):
    """F4: a bad explicit bind is a config error -- retrying would never fix it
    and would bury the message in a restart storm."""
    slept = []
    monkeypatch.setattr(dashboard, "make_server",
                        lambda *a, **k: pytest.fail("must not bind"))
    rc = dashboard.serve("/tmp/q.json", bind="0.0.0.0",
                         run_subprocess=lambda *a, **k: None, sleep=slept.append)
    assert rc == 1
    assert slept == []


def test_serve_gives_up_after_max_attempts(monkeypatch):
    slept = []
    rc = dashboard.serve("/tmp/q.json", bind="auto",
                         run_subprocess=lambda *a, **k: _Completed(1, ""),
                         sleep=slept.append, max_attempts=3)
    assert rc == 1
    assert slept == [30, 30]


def test_refresh_re_enables_both_buttons():
    """F7: send() disables both for the round trip, so a FAILED post must not
    leave the page permanently inert."""
    page = _page([_rec(1)])
    js = _script(page).replace(" ", "").replace("\n", "")
    assert "go.disabled=inflight||n===0" in js
    assert "all.disabled=inflight||blanket().length===0" in js
    # every failure path clears the in-flight flag and refreshes
    assert js.count("inflight=false;") >= 2


def test_branch_affinity_is_repo_scoped():
    """F8 (falsifying): short conventional branch names collide across repos --
    `chardy/fix-login` in pb-www is not the same workstream as the identically
    named branch in pb-api. Drop the repo from the branch token and these
    collapse into one card."""
    recs = [_br(1, repo="pb-www", branch="chardy/fix-login"),
            _br(2, repo="pb-www", branch="chardy/fix-login"),
            _br(3, repo="pb-api", branch="chardy/fix-login")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    assert [sorted(r.number for r in g.records) for g in groups] == [[1, 2], [3]]
    assert [g.title for g in groups] == ["chardy/fix-login", "chardy/fix-login"]


def test_a_record_with_no_repo_contributes_no_mr_token():
    """F8 (falsifying): an iid with no repo has no namespace to scope it to, so
    it must not merge with a real repo's MR of the same number. Todo-derived
    records carry no repo."""
    recs = [_br(1, repo="pb-www",
                web_url="https://gl/g/pb-www/-/merge_requests/4821"),
            _br(2, repo="",
                web_url="https://gl/g/other/-/merge_requests/4821")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert [sorted(r.number for r in g.records) for g in groups] == [[1]]
    assert [r.number for r in ungrouped] == [2]


def test_a_record_with_no_repo_still_groups_by_branch():
    """F8: losing the MR token must not also cost it branch affinity."""
    recs = [_br(1, repo="", branch="chardy/x"), _br(2, repo="", branch="chardy/x")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    assert [sorted(r.number for r in g.records) for g in groups] == [[1, 2]]


def test_every_group_belongs_to_exactly_one_repo():
    """F8's real invariant: once BOTH token kinds embed the repo, a connected
    component can no longer span repos at all -- so a workstream card is always
    one repo's work. This is what stops pb-www and pb-api collapsing together
    through either a shared branch name or a shared iid."""
    recs = [_br(1, repo="pb-www", branch="shared",
                web_url="https://gl/g/pb-www/-/merge_requests/4821"),
            _br(2, repo="pb-api", branch="shared", mr_iid=4821, web_url=""),
            _br(3, repo="pb-www", branch="shared"),
            _br(4, repo="pb-api", branch="shared")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    for g in groups:
        assert len({r.item.repo for r in g.records}) == 1
    by_repo = {g.records[0].item.repo: sorted(r.number for r in g.records)
               for g in groups}
    assert by_repo == {"pb-www": [1, 3], "pb-api": [2, 4]}


def test_bare_iid_suppression_within_a_repo():
    """An iid that DOES have a URL among the group's records is linked, not
    bare; one that does not is rendered as unlinked text."""
    recs = [_br(1, repo="pb-www", branch="b",
                web_url="https://gl/g/pb-www/-/merge_requests/4821"),
            _br(2, repo="pb-www", branch="b", mr_iid=4821, web_url=""),
            _br(3, repo="pb-www", branch="b", mr_iid=4999, web_url="")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert len(groups) == 1
    assert groups[0].mr_links == ("https://gl/g/pb-www/-/merge_requests/4821",)
    # 4821 is already linked; 4999 has no URL anywhere -> bare
    assert groups[0].bare_mr_refs == (4999,)


def test_an_auto_refresh_is_held_while_a_selection_or_post_is_live():
    """F7 (falsifying), unchanged in substance: an update landing mid-POST
    tears an approval, and one landing with boxes ticked moves rows out from
    under the user's thumb. Only the CONSEQUENCE of the guard changed -- from
    reloading later to swapping in place, and from holding silently to holding
    with the chip up."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    # One shared guard still covers every automatic refresh path: an in-flight
    # POST, an open confirm dialog, or any ticked checkbox.
    assert "functionbusy(){returninflight||confirming||selected().length>0;}" in js
    assert "if(inflight||(!force&&busy())){pending=true;chip(true);return;}" in js
    assert "location.reload" not in js


# =============================================================================
# Fix Mode: Attempt 2 -- GitLab now serves issues as /-/work_items/<iid>
# =============================================================================

# The exact shape Chandler hit on the deployed dashboard: an assigned-issue
# record whose web_url came back from the GitLab API as a work item, not an
# issue. `implement` rows are ALL issue-kind (assessor.py:216), so this was
# every implement row on the page rendering with no link at all.
_WORK_ITEM_URL = "https://gitlab.com/performancelivestock/pb-www/-/work_items/869"
_ISSUE_URL = "https://gitlab.com/performancelivestock/pb-www/-/issues/869"


@pytest.mark.parametrize("url", [_WORK_ITEM_URL, _ISSUE_URL])
def test_issue_iid_is_read_from_both_url_forms(url):
    """Both spellings are the same issue -- GitLab just changed which one it
    hands back. Mirrors curator.py:377, which was already tolerant."""
    assert dashboard.issue_iid_of(_rec(1, web_url=url).item) == 869
    assert dashboard.ref_of(_rec(1, web_url=url).item) == "#869"


@pytest.mark.parametrize("url", [_WORK_ITEM_URL, _ISSUE_URL])
def test_issue_row_renders_a_linked_ref(url):
    """Falsifying: revert _ISSUE_URL_RE to /-/issues/ only and the work_items
    case renders no ref and no anchor -- the live bug."""
    page = _page([_rec(1, kind="issue", executor="implement", web_url=url)])
    assert f'<a href="{url}">#869</a>' in page
    assert ">#869<" in page


@pytest.mark.parametrize("url", [_WORK_ITEM_URL, _ISSUE_URL])
def test_issue_url_links_in_a_branches_card_header(url):
    """The Branches card header links every issue in the workstream (AC #38)."""
    recs = [_br(1, branch="chardy/869-x", web_url=url),
            _br(2, branch="chardy/869-x",
                web_url="https://gitlab.com/performancelivestock/pb-www/"
                        "-/merge_requests/4821")]
    groups, ungrouped = dashboard.group_by_workstream(recs)
    assert ungrouped == []
    assert len(groups) == 1
    assert groups[0].issue_links == (url,)
    assert groups[0].mr_links == (
        "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/4821",)
    page = _page(recs)
    assert f'<a href="{url}">#869</a>' in page
    assert ">!4821<" in page


def test_a_work_item_url_is_not_mistaken_for_an_mr():
    """The two ref kinds must stay distinct: a work item is `#`, never `!`."""
    item = _rec(1, web_url=_WORK_ITEM_URL).item
    assert dashboard.mr_iid_of(item) == 0
    assert dashboard.ref_of(item) == "#869"


def test_work_item_records_still_group_and_stay_ungrouped_correctly():
    """A work_items URL carries no MR affinity, so a record with one and no
    branch still lands in Ungrouped (AC #37) rather than inventing a token."""
    groups, ungrouped = dashboard.group_by_workstream(
        [_br(1, branch="", web_url=_WORK_ITEM_URL)])
    assert groups == []
    assert [r.number for r in ungrouped] == [1]


@pytest.mark.parametrize("url,expect_ref", [
    # a project whose NAME is issues/work_items is still an MR URL: the regex
    # requires digits immediately after the segment, so `/issues/-/merge...`
    # cannot match. Guards the widened pattern against over-matching.
    ("https://gitlab.com/group/issues/-/merge_requests/5", "!5"),
    ("https://gitlab.com/group/work_items/-/merge_requests/7", "!7"),
    ("https://gitlab.com/group/issues-tracker/-/merge_requests/5", "!5"),
    ("https://gitlab.com/performancelivestock/pb-www/-/merge_requests/4821", "!4821"),
    ("https://gitlab.com/dashboard/todos", ""),
    ("", ""),
])
def test_widened_issue_regex_does_not_over_match(url, expect_ref):
    assert dashboard.ref_of(_rec(1, web_url=url).item) == expect_ref


def test_issue_ref_survives_a_url_with_a_trailing_path():
    """A designs/notes sub-path still resolves to the parent issue."""
    item = _rec(1, web_url="https://gl/g/p/-/work_items/869/designs/a.png").item
    assert dashboard.ref_of(item) == "#869"


# =============================================================================
# Fix Mode: Attempt 3 -- "Sync" kicks a sweep so the page is not stuck on the
# 9am/1pm queue snapshot
# =============================================================================

class _Sweeper:
    """Records calls to the injected sweep edge; optionally fails."""

    def __init__(self, fail=None):
        self.calls = 0
        self.fail = fail

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise self.fail


def test_post_sweep_kicks_the_injected_edge_exactly_once(serve_queue):
    sweeper = _Sweeper()
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    status, headers, body = s.sweep()
    assert status == 202
    assert json.loads(body) == {"started": True}
    assert sweeper.calls == 1


def test_post_sweep_without_the_custom_header_is_403(serve_queue):
    """Falsifying: drop the CSRF check on /sweep and any page in a tailnet
    browser could make the mini sweep and post a Discord digest on demand."""
    sweeper = _Sweeper()
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    status, _, _ = s.request("POST", "/sweep", "", {})
    assert status == 403
    assert sweeper.calls == 0
    # an empty header value is not a header
    status, _, _ = s.request("POST", "/sweep", "", {"X-Worksweep": ""})
    assert status == 403
    assert sweeper.calls == 0


def test_post_sweep_with_a_mismatched_origin_is_403(serve_queue):
    sweeper = _Sweeper()
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    status, _, _ = s.sweep(headers={"Origin": "http://evil.example"})
    assert status == 403
    assert sweeper.calls == 0


def test_get_sweep_is_404(serve_queue):
    """The sweep is a POST-only side effect; GET must not trigger it."""
    sweeper = _Sweeper()
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    status, _, _ = s.request("GET", "/sweep")
    assert status == 404
    assert sweeper.calls == 0


def test_second_sweep_within_the_window_is_429(serve_queue):
    """Falsifying: remove the throttle and a held button (or an impatient tap)
    fires a sweep -- and a Discord digest -- per tap."""
    sweeper = _Sweeper()
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    assert s.sweep()[0] == 202

    status, headers, body = s.sweep()
    assert status == 429
    assert sweeper.calls == 1                      # the edge was NOT called again
    payload = json.loads(body)
    assert payload["started"] is False
    assert 0 < payload["retry_after"] <= 61
    assert int(headers["Retry-After"]) == payload["retry_after"]


def test_a_failed_sweep_does_not_hold_the_user_off(serve_queue):
    """A kickstart that failed started nothing and posted no digest, so the
    throttle must not lock the user out for a minute over it."""
    sweeper = _Sweeper(fail=RuntimeError("launchctl kickstart exited 3"))
    s, _ = serve_queue([_rec(1)], sweep=sweeper)
    status, _, body = s.sweep()
    assert status == 500
    assert json.loads(body)["started"] is False
    assert "launchctl" in json.loads(body)["error"]

    sweeper.fail = None                            # launchd recovers
    status, _, body = s.sweep()
    assert status == 202                           # retryable immediately
    assert json.loads(body) == {"started": True}
    assert sweeper.calls == 2


def test_sweep_returns_500_when_the_edge_is_not_wired(serve_queue):
    s, _ = serve_queue([_rec(1)])                  # no sweep injected
    status, _, body = s.sweep()
    assert status == 500
    assert json.loads(body)["started"] is False
    # and it must stay retryable, not burn the throttle window
    status, _, _ = s.sweep()
    assert status == 500


def test_sweep_never_runs_in_process(serve_queue):
    """The sweep belongs to its own agent: this module must hold no code path
    that sweeps locally (different env, double queue writer, ~90s blocking)."""
    tree = _dashboard_tree()
    called = _called_names(tree)
    for banned in ("run_sweep", "reconcile", "collect_issues", "_run_intake"):
        assert banned not in called, banned
    assert "run_sweep" not in _dashboard_src()


def test_sweep_response_is_fast_and_does_not_block_approvals(serve_queue):
    """The throttle lock is held only across the check-and-set, never across
    the edge, so a slow kickstart cannot stall an approval."""
    import time as _t
    gate = threading.Event()

    def slow_sweep():
        gate.wait(timeout=5)

    s, qpath = serve_queue([_rec(1)], sweep=slow_sweep)
    t = threading.Thread(target=lambda: s.sweep(), daemon=True)
    t.start()
    _t.sleep(0.1)                                  # sweep is mid-flight
    started = _t.monotonic()
    assert s.approve([1])[0] == 200                # not blocked behind it
    assert _t.monotonic() - started < 2.0
    gate.set()
    t.join(timeout=5)
    assert load_queue(qpath)[0].item.status == "approved"


# --- GET /mtime --------------------------------------------------------------

def test_mtime_returns_the_queue_file_mtime(serve_queue):
    s, qpath = serve_queue([_rec(1)])
    status, headers, body = s.request("GET", "/mtime")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    st = os.stat(qpath)
    assert body.decode() == dashboard.mtime_token(st.st_mtime, st.st_size)


def test_mtime_changes_when_the_queue_is_rewritten(serve_queue):
    """This is the whole signal the Sync flow waits on."""
    s, qpath = serve_queue([_rec(1)])
    before = s.request("GET", "/mtime")[2]
    os.utime(qpath, (1_800_000_000, 1_800_000_000))
    after = s.request("GET", "/mtime")[2]
    assert after != before
    assert after.decode() == dashboard.mtime_token(1_800_000_000,
                                                   os.path.getsize(qpath))
    # ...and a rewrite that lands inside the SAME filesystem tick still moves
    # it, which mtime alone could not do on a one-second-granularity volume
    save_queue(qpath, [_rec(1), _rec(2)])
    os.utime(qpath, (1_800_000_000, 1_800_000_000))
    same_tick = s.request("GET", "/mtime")[2]
    assert same_tick != after


def test_mtime_needs_no_csrf_header(serve_queue):
    """Read-only and side-effect free: it leaks only when the queue last
    changed, which the page already displays."""
    s, _ = serve_queue([_rec(1)])
    assert s.request("GET", "/mtime", None, {})[0] == 200


def test_mtime_is_get_only(serve_queue):
    s, _ = serve_queue([_rec(1)])
    status, _, _ = s.request("POST", "/mtime", "", {"X-Worksweep": "approve"})
    assert status == 404


def test_mtime_is_zero_when_the_queue_is_missing(tmp_path):
    s = _Server(os.path.join(str(tmp_path), "gone.json"))
    try:
        status, _, body = s.request("GET", "/mtime")
        assert status == 200
        assert body.decode() == "0"
    finally:
        s.close()


# --- the Sync control --------------------------------------------------------

def test_sync_button_is_in_the_header_for_every_layout():
    """It lives in the header, outside the per-view containers, so all three
    layouts get it without duplicating the control."""
    page = _page([_rec(1)])
    btn = re.search(r'<button[^>]*id="sync"[^>]*>.*?</button>', page, re.S)
    assert btn is not None
    assert page.index('id="sync"') < page.index('class="sections"')
    assert page.count('id="sync"') == 1
    assert "Sync" in btn.group(0)


def test_sync_button_carries_the_rendered_mtime():
    """The page compares this against GET /mtime, so it must be the token the
    page was actually rendered from -- not a fresh read."""
    page = _page([_rec(1)], mtime=1_750_000_000.0)
    btn = re.search(r'<button[^>]*id="sync"[^>]*>', page).group(0)
    assert f'data-mtime="{dashboard.mtime_token(1_750_000_000.0)}"' in btn
    assert 'data-mtime="0"' in re.search(
        r'<button[^>]*id="sync"[^>]*>', _page([_rec(1)], mtime=None)).group(0)


def test_sync_posts_to_sweep_and_leaves_the_reload_to_the_live_poll():
    """Addendum 3: Sync no longer owns a private poll -- the always-on one
    reloads when the sweep lands, so there is one refresh path, not two."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "fetch('/sweep',{method:'POST',headers:{'X-Worksweep':'approve'}})" in js
    assert ("varsync=document.getElementById('sync');"
            "if(!sync||syncing||sync.disabled){return;}"
            "syncing=true;sync.disabled=true;sync.textContent='syncing…';") in js
    assert "if(r.status===429){syncDone('justsynced');return;}" in js
    assert "pollMtime" not in js                    # the private poll is gone
    # the button still un-spins if the sweep dies without moving the queue
    assert "setTimeout(function(){syncDone('Sync');},SYNC_MAX_MS);" in js
    assert "SYNC_MAX_MS=120000" in js


def test_sync_button_has_hover_and_active_states():
    css = _style(_page([_rec(1)]))
    assert _rule(css, ".btn-sync:hover") is not None
    assert _rule(css, ".btn-sync:active") is not None


# --- staleness telemetry -----------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "synced just now"),
    (30, "synced just now"),
    (14 * 60, "synced 14 min ago"),
    (59 * 60, "synced 59 min ago"),
    (3 * 3600, "synced 3 hr ago"),
    (23 * 3600, "synced 23 hr ago"),
    (86400, "synced 1 day ago"),
    (3 * 86400, "synced 3 days ago"),
])
def test_relative_age_text(seconds, expected):
    import datetime
    base = datetime.datetime.fromisoformat(NOW).timestamp()
    assert dashboard.relative_age(base - seconds, NOW) == expected


def test_relative_age_handles_missing_and_skewed_mtimes():
    import datetime
    base = datetime.datetime.fromisoformat(NOW).timestamp()
    assert dashboard.relative_age(None, NOW) == "never synced"
    assert dashboard.relative_age(0, NOW) == "never synced"
    # a clock-skewed future mtime must not render "synced -3 min ago"
    assert dashboard.relative_age(base + 600, NOW) == "synced just now"
    assert dashboard.relative_age(base, "not-a-timestamp") == "synced"


def test_header_shows_staleness_at_a_glance():
    import datetime
    base = datetime.datetime.fromisoformat(NOW).timestamp()
    page = _page([_rec(1)], mtime=base - 14 * 60)
    head = page[:page.index("Needs you")]
    assert "synced 14 min ago" in head
    # the absolute stamp stays for precision
    stamp = datetime.datetime.fromtimestamp(base - 14 * 60).strftime("%Y-%m-%d %H:%M")
    assert stamp in head


def test_header_says_never_synced_with_no_queue_file():
    page = _page([], mtime=None)
    assert "never synced" in page


# =============================================================================
# Fix Mode: Attempt 4 -- dismiss, pill filters, live polling, card naming
# =============================================================================

class _Marker:
    def __init__(self, fail=None):
        self.ids, self.fail = [], fail

    def __call__(self, todo_id):
        self.ids.append(todo_id)
        if self.fail:
            raise self.fail


def _todo(n, todo_id=0, status="proposed"):
    """A todo record. `todo_id` 0 models a LEGACY record -- one written before
    the field existed -- which dismisses locally but cannot be cleared in
    GitLab. The id STRING is unchanged either way: it is the queue's identity
    key, so carrying the todo id there would renumber every todo."""
    return _rec(n, kind="todo", executor="triage", status=status,
                id="todo:assigned:https://gl/x/-/work_items/1719",
                web_url="https://gl/x/-/work_items/1719", todo_id=todo_id)


def test_dismiss_flips_a_non_runnable_row_to_done(serve_queue):
    s, qpath = serve_queue([_rec(1, executor="triage"), _rec(2, executor="triage")])
    status, _, body = s.dismiss(1)
    assert status == 200
    assert json.loads(body) == {"dismissed": True, "number": 1}
    out = {r.number: (r.item.status, r.item.done_reason) for r in load_queue(qpath)}
    assert out == {1: ("done", "dismissed"), 2: ("proposed", "")}


def test_dismiss_refuses_a_runnable_row(serve_queue):
    """Falsifying: drop the non-runnable gate and Dismiss silently throws away
    work the runner was about to do -- with no un-dismiss path."""
    s, qpath = serve_queue([_rec(1, executor="magi-review"),
                            _rec(2, executor="keep-current"),
                            _rec(3, executor="implement")])
    before = open(qpath, "rb").read()
    for n in (1, 2, 3):
        status, _, body = s.dismiss(n)
        assert status == 400, n
        assert json.loads(body)["dismissed"] is False
    assert open(qpath, "rb").read() == before


def test_dismiss_refuses_an_already_terminal_row(serve_queue):
    s, qpath = serve_queue([_rec(1, executor="triage", status="done"),
                            _rec(2, executor="triage", status="error")])
    before = open(qpath, "rb").read()
    assert s.dismiss(1)[0] == 400
    assert s.dismiss(2)[0] == 400
    assert open(qpath, "rb").read() == before


def test_dismiss_of_an_unknown_number_is_400(serve_queue):
    s, qpath = serve_queue([_rec(1, executor="triage")])
    before = open(qpath, "rb").read()
    status, _, body = s.dismiss(99)
    assert status == 400
    assert "99" in json.loads(body)["error"]
    assert open(qpath, "rb").read() == before


def test_dismiss_without_the_custom_header_is_403(serve_queue):
    """Falsifying: without the CSRF guard any tailnet page could retire rows."""
    s, qpath = serve_queue([_rec(1, executor="triage")])
    before = open(qpath, "rb").read()
    status, _, _ = s.request("POST", "/dismiss", json.dumps({"number": 1}), {})
    assert status == 403
    assert open(qpath, "rb").read() == before
    assert s.dismiss(1, headers={"Origin": "http://evil.example"})[0] == 403
    assert open(qpath, "rb").read() == before


def test_get_dismiss_is_404(serve_queue):
    s, _ = serve_queue([_rec(1, executor="triage")])
    assert s.request("GET", "/dismiss")[0] == 404


@pytest.mark.parametrize("body", [
    "", "not json", "[]", "{}", '{"number": "1"}', '{"number": true}',
    '{"number": null}', '{"number": 1.5}', '{"numbers": [1]}',
])
def test_malformed_dismiss_body_is_400(serve_queue, body):
    s, qpath = serve_queue([_rec(1, executor="triage")])
    before = open(qpath, "rb").read()
    assert s.dismiss(1, body=body)[0] == 400
    assert open(qpath, "rb").read() == before


def test_dismiss_marks_the_gitlab_todo_done_once(serve_queue):
    marker = _Marker()
    s, qpath = serve_queue([_todo(1, todo_id=4242)], mark_todo_done=marker)
    assert s.dismiss(1)[0] == 200
    assert marker.ids == [4242]
    assert load_queue(qpath)[0].item.status == "done"


def test_a_glab_failure_still_dismisses_locally(serve_queue, capsys):
    """Falsifying: clearing the GitLab todo is a courtesy on top of the local
    dismiss. If glab failing blocked it, a GitLab outage would freeze the page."""
    marker = _Marker(fail=RuntimeError("glab exited 1: 404 not found"))
    s, qpath = serve_queue([_todo(1, todo_id=4242)], mark_todo_done=marker)
    status, _, body = s.dismiss(1)
    assert status == 200
    assert json.loads(body)["dismissed"] is True
    assert marker.ids == [4242]
    out = load_queue(qpath)[0].item
    assert (out.status, out.done_reason) == ("done", "dismissed")


def test_a_legacy_todo_record_dismisses_locally_and_says_why_not_in_gitlab(
        serve_queue, capsys):
    """A todo record written before `todo_id` existed carries 0, so the GitLab
    edge cannot fire for it. It still dismisses locally, and the miss must be
    LOUD rather than silent -- these refresh on the next sweep."""
    marker = _Marker()
    s, qpath = serve_queue([_todo(1)], mark_todo_done=marker)
    assert s.dismiss(1)[0] == 200
    assert marker.ids == []                        # nothing to call it with
    assert load_queue(qpath)[0].item.status == "done"
    err = capsys.readouterr().err
    assert "was NOT marked done" in err
    assert "no todo id" in err


def test_todo_id_of_reads_the_persisted_field():
    assert dashboard.todo_id_of(_todo(1, todo_id=4242).item) == 4242
    # a legacy todo record, written before the field existed
    assert dashboard.todo_id_of(_todo(1).item) == 0
    # every non-todo kind, and anything unparseable, is 0 and never raises
    assert dashboard.todo_id_of(_rec(1, id="issue:pb-www#869").item) == 0
    assert dashboard.todo_id_of(_rec(1, kind="mr").item) == 0

    class _Junk:
        todo_id = "not-a-number"
    assert dashboard.todo_id_of(_Junk()) == 0
    assert dashboard.todo_id_of(object()) == 0


def test_dismiss_posts_a_discord_audit(serve_queue):
    posted = []
    s, _ = serve_queue([_rec(1, executor="triage")],
                       post=lambda hook, c: posted.append(c),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.dismiss(1)[0] == 200
    assert len(posted) == 1
    assert posted[0].startswith("🗑️ dismissed 1")
    assert "(dashboard)" in posted[0]


def test_a_failed_dismiss_audit_does_not_undo_the_dismiss(serve_queue):
    def boom(hook, content):
        raise RuntimeError("discord down")
    s, qpath = serve_queue([_rec(1, executor="triage")], post=boom,
                           webhook="https://discord.com/api/webhooks/1/x")
    assert s.dismiss(1)[0] == 200
    assert load_queue(qpath)[0].item.status == "done"


def test_dismiss_does_not_hold_the_write_lock_across_glab(serve_queue):
    """A 30s glab timeout must not stall approvals behind it."""
    import time as _t
    gate = threading.Event()
    s, qpath = serve_queue([_rec(1, executor="triage"), _rec(2)],
                           mark_todo_done=lambda i: gate.wait(timeout=5))
    t = threading.Thread(target=lambda: s.dismiss(1), daemon=True)
    t.start()
    _t.sleep(0.1)
    started = _t.monotonic()
    assert s.approve([2])[0] == 200
    assert _t.monotonic() - started < 2.0
    gate.set()
    t.join(timeout=5)


# --- pills as filters --------------------------------------------------------

def test_rows_carry_their_status_for_filtering():
    """Both row renderers must tag rows, or the filter silently half-works:
    the Branches view would filter and the Checklist/Panels views would not."""
    page = _page([_rec(1, status="proposed"), _rec(2, status="running"),
                  _rec(3, status="done"), _rec(4, status="error")])
    sections = page[page.index('class="sections"'):page.index('class="branches"')]
    branches = page[page.index('class="branches"'):]
    for view, name in ((sections, "sections"), (branches, "branches")):
        found = sorted(set(re.findall(r'<div class="row" data-st="([a-z-]+)"',
                                      view)))
        assert found == ["done", "error", "proposed", "running"], name
    # every rendered row is taggedatag-less row could never be filtered out
    assert page.count('<div class="row"') == page.count('<div class="row" data-st=')


def test_status_pills_are_filter_buttons():
    page = _page([_rec(1, status="proposed"), _rec(2, status="running")])
    pills = re.findall(r'<button[^>]*data-filter="([a-z-]+)"[^>]*>', page)
    assert sorted(pills) == ["proposed", "running"]
    for tag in re.findall(r'<button[^>]*data-filter="[a-z-]+"[^>]*>', page):
        assert 'aria-pressed="false"' in tag       # fresh page = no filter
    css = _style(page)
    assert _rule(css, "button.cnt:hover") is not None
    assert _rule(css, "button.cnt:active") is not None
    assert _rule(css, 'button.cnt[aria-pressed="true"]') is not None


def test_done_this_week_is_informational_not_a_filter():
    """It must not invite tapping: a span, no data-filter, no hover rule."""
    page = _page([_rec(1, status="done")])
    week = re.search(r'<span class="cnt cnt-week">[^<]*</span>', page)
    assert week is not None
    assert "data-filter" not in week.group(0)
    css = _style(page)
    assert _rule(css, ".cnt-week:hover") is None
    assert "cursor:default" in _rule(css, ".cnt-week")


def test_filter_toggle_logic_is_emitted():
    """Falsifying: strip the toggle and the pills become inert decoration."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    # exactly one active at a time, and tapping the active one clears it
    assert ("root.setAttribute('data-filter',"
            "(root.getAttribute('data-filter')||'')===v?'':v);") in js
    assert "rows[i].style.display=(!f||rows[i].getAttribute('data-st')===f)?'':'none';" in js
    assert "vart=e.target.closest('[data-set-layout]');" in js
    assert "varf=e.target.closest('[data-filter]');" in js
    assert "toggleFilter(f.getAttribute('data-filter'))" in js
    # not persisted anywhere
    assert "localStorage.setItem('worksweep-filter'" not in js
    assert "data-filter" not in _page([_rec(1)])[:_page([_rec(1)]).index("<head")]


def test_dismiss_button_is_wired_in_the_page():
    js = _script(_page([_rec(1, executor="triage")])).replace(" ", "").replace("\n", "")
    assert "vard=e.target.closest('[data-dismiss]');" in js
    assert "send('/dismiss',{number:parseInt(d.getAttribute('data-dismiss'),10)})" in js


# --- always-on live polling --------------------------------------------------

def test_the_mtime_poll_is_the_degraded_path_never_gated_on_a_tap():
    """Addendum 3, still binding in its real substance: the poll must never be
    something the user has to trigger. It is no longer the primary path -- the
    event stream is -- but when it runs it is armed by the transport failing,
    and it is armed exactly once however many times that failure repeats."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "POLL_MS=10000" in js
    assert "fetch('/mtime',{cache:'no-store'})" in js
    assert ("functionarmPoll(){if(polling){return;}"
            "polling=true;setTimeout(poll,POLL_MS);}") in js
    # and never armed at top level any more, at any indent
    script = _script(_page([_rec(1)]))
    assert not re.search(r"^  setTimeout\(poll,POLL_MS\);$", script, re.M)


def test_live_poll_hands_the_busy_decision_to_the_swap_path():
    """Falsifying: the poll used to weigh busy() itself and then reload. If it
    still did, it would drop the change on the floor -- lastMtime is reassigned
    on every response, so the poll would never look at that change again and
    the update would be lost rather than held."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "if(lastMtime&&t!==lastMtime){applyFragments();}" in js
    body = re.search(r"^  function poll\(\)\{$.*?^  \}$",
                     _script(_page([_rec(1)])), re.S | re.M)
    assert body and "busy()" not in body.group(0)
    # and it keeps polling either way, so it resumes rather than giving up
    assert "setTimeout(poll,POLL_MS);" in js


# --- Branches ordering (addendum 5) -----------------------------------------

def _act(n, branch, last_seen, status="proposed"):
    return QueueRecord(number=n, first_seen=T0, last_seen=last_seen,
                       item=_rec(n, status=status, branch=branch,
                                 title=f"work {n}", kind="mr").item)


def test_cards_order_active_by_recency_then_completed_last():
    """Addendum 5 (falsifying): ascending-by-number put long-merged workstreams
    at the top of the page. Live work first, most recently touched first."""
    recs = [_act(1, "b-stale", "2026-06-01T00:00:00Z"),
            _act(2, "b-recent", "2026-06-29T00:00:00Z"),
            _act(3, "b-done", "2026-06-30T00:00:00Z", status="done"),
            _act(4, "b-error", "2026-06-28T00:00:00Z", status="error")]
    groups, _ = dashboard.group_by_workstream(recs)
    # branch wins over title as the card name, so cards read by branch here
    assert [g.title for g in groups] == ["b-recent", "b-stale", "b-done", "b-error"]
    assert [g.active for g in groups] == [True, True, False, False]


def test_a_card_is_active_when_any_member_is_non_terminal():
    recs = [_act(1, "b", "2026-06-01T00:00:00Z", status="done"),
            _act(2, "b", "2026-06-02T00:00:00Z", status="proposed")]
    groups, _ = dashboard.group_by_workstream(recs)
    assert len(groups) == 1
    assert groups[0].active is True
    assert groups[0].last_activity == "2026-06-02T00:00:00Z"


def test_completed_cards_render_under_a_quiet_divider_and_before_ungrouped():
    recs = [_act(1, "b-live", "2026-06-29T00:00:00Z"),
            _act(2, "b-done", "2026-06-30T00:00:00Z", status="done"),
            _br(3, branch="", web_url="")]
    page = _page(recs)
    assert page.index("b-live") < page.index('class="divider"')
    assert page.index('class="divider"') < page.index("b-done")
    assert page.index("b-done") < page.index("Ungrouped")
    assert "card card-done" in page
    css = _style(page)
    assert _rule(css, ".card-done") is not None
    assert _rule(css, ".divider") is not None


def test_no_divider_when_every_card_is_active():
    page = _page([_act(1, "b", "2026-06-29T00:00:00Z")])
    assert 'class="divider"' not in page


# =============================================================================
# Fix Mode: Attempt 7 -- the sticky bar hides when nothing visible is approvable
# =============================================================================

def test_bar_is_hidden_when_no_row_is_approvable():
    """Falsifying: a queue of only running/done rows rendered a bar offering
    "Approve selected" over a page with nothing selectable -- dead chrome across
    the bottom of a phone screen."""
    page = _page([_rec(1, status="running"), _rec(2, status="done"),
                  _rec(3, status="error"), _rec(4, status="approved")])
    assert re.search(r'<div class="bar" hidden>', page)
    assert _checkboxes(page, "sections") == []


def test_bar_is_hidden_when_every_actionable_row_is_non_runnable():
    """triage/mr-hygiene rows render Dismiss, never a checkbox, so the approve
    bar has nothing to act on."""
    page = _page([_rec(1, executor="triage"), _rec(2, executor="mr-hygiene"),
                  _rec(3, executor="none")])
    assert re.search(r'<div class="bar" hidden>', page)


@pytest.mark.parametrize("status", ["proposed", "needs-input"])
def test_bar_is_visible_when_something_is_approvable(status):
    page = _page([_rec(1, status=status), _rec(2, status="done")])
    assert '<div class="bar">' in page
    assert "hidden" not in re.search(r'<div class="bar"[^>]*>', page).group(0)


def test_bar_hidden_state_matches_the_rows_that_got_a_checkbox():
    """The initial state and the rendered controls share one predicate, so they
    cannot disagree."""
    for recs in ([_rec(1, status="running")],
                 [_rec(1, executor="triage")],
                 [_rec(1, status="proposed")],
                 [_rec(1, status="running"), _rec(2, status="needs-input")]):
        page = _page(recs)
        any_checkbox = bool(_checkboxes(page, "sections"))
        bar = re.search(r'<div class="bar"[^>]*>', page).group(0)
        assert ("hidden" in bar) is (not any_checkbox), (bar, recs[0].item.status)
        assert any(dashboard.has_checkbox(r.item) for r in recs) is any_checkbox


def test_hidden_bar_is_actually_removed_from_layout():
    """`display:none`, not merely disabled. A class rule outranks the UA's
    [hidden] style, so without this the hidden bar would still lay out."""
    css = _style(_page([_rec(1)]))
    assert ".bar[hidden]{display:none}" in css.replace(" ", "")
    # and the rule must come after the .bar layout rule to win
    assert css.index(".bar{") < css.index(".bar[hidden]")


def test_bar_visibility_is_recomputed_on_every_filter_and_view_change():
    """Falsifying: drop the recompute and the bar stays up under a `done`
    filter even though every visible row lost its checkbox."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "varbar=document.querySelector('.bar');" in js
    assert "if(bar){bar.hidden=boxes('input[type=checkbox]').length===0;}" in js
    # refresh() is the single place it happens, and both change paths end there
    assert "marks();applyFilter();refresh();" in js
    assert js.count("applyFilter();") >= 2          # setLayout + init
    assert "refresh();}" in js                      # applyFilter ends in refresh


def test_only_visible_rows_count_as_selectable():
    """A row filtered out by a status pill is not on offer: it must not be
    counted, submitted, or keep the bar up. This is also what stops a stranded
    invisible selection wedging the live-poll guard."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "varrow=b.closest?b.closest('.row'):null;" in js
    assert "return!row||row.style.display!=='none';" in js
    # selected(), blanket() and the bar count all go through boxes()
    assert "returnnums(boxes('input[type=checkbox]')" in js
    assert "returnnums(boxes('input[type=checkbox][data-blanket=\"1\"]'));" in js


# --- actor-attributed approvals (Decision 8, 2026-08-25) -------------------
#
# Chandler sometimes tells Claude "✅ all, then move on" rather than opening
# the dashboard himself. The ✅ gate is still a human-consent gate either way,
# so the channel record has to stay legible about which hand pressed the
# button -- otherwise the audit trail cannot tell the two apart at all.

def test_approve_actor_attribution():
    """FALSIFYING (AC #14). Exactly two rendered outcomes exist, and the field
    is optional, so nothing the browser sends today can change.

    Mutation: apply the suffix unconditionally and the absent-field and
    non-`claude` assertions go red.
    """
    records = [_rec(1), _rec(2)]
    assert dashboard._audit_message([1, 2], records, actor="claude") == (
        "✅ Approved: 1 (magi-review pb-www), "
        "2 (magi-review pb-www) (dashboard · claude)")
    assert dashboard._audit_message([1, 2], records).endswith(" (dashboard)")
    assert dashboard._audit_message([1, 2], records,
                                    actor="mallory").endswith(" (dashboard)")


def test_the_actor_suffix_is_accounted_for_by_the_clamp():
    """The suffix is computed BEFORE the message is measured -- otherwise the
    longer one pushes an approve-all past the Discord cap and the audit trail
    vanishes for exactly the highest-blast-radius action."""
    from worksweep.formatter import DISCORD_MAX_CHARS
    records = [_rec(n, repo="pb-www-with-a-long-name", executor="magi-review")
               for n in range(1, 301)]
    msg = dashboard._audit_message(list(range(1, 301)), records,
                                   actor="claude")
    assert len(msg.encode("utf-8")) <= DISCORD_MAX_CHARS
    assert msg.endswith(" (dashboard · claude)")


def test_a_claude_approve_post_is_attributed_in_the_channel(serve_queue):
    posted = []
    s, _ = serve_queue([_rec(1), _rec(2)],
                       post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve([1, 2], actor="claude")[0] == 200
    assert len(posted) == 1
    assert posted[0].endswith(" (dashboard · claude)")


def test_approve_all_carries_the_actor_too(serve_queue):
    posted = []
    s, _ = serve_queue([_rec(1)],
                       post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve_all([1], actor="claude")[0] == 200
    assert posted[0].endswith(" (dashboard · claude)")


def test_the_browser_body_still_renders_the_bare_suffix(serve_queue):
    """The page's own JS deliberately keeps sending {numbers:[...]} with no
    actor, so a human tap must be byte-identical to what it posted before."""
    posted = []
    s, _ = serve_queue([_rec(1)],
                       post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.approve([1])[0] == 200
    assert posted[0] == "✅ Approved: 1 (magi-review pb-www) (dashboard)"


def test_approve_actor_rejects_hostile_values(serve_queue):
    """The actor flows into a Discord post, so it is a whitelist of one and
    the submitted text is NEVER reflected. Every rejected value still
    approves normally and still returns 200 -- this is an attribution field,
    not an authorisation one."""
    for actor in ("x" * 5000, "@everyone https://evil.example/pwn",
                  "claude ", "Claude", 7, True, None, ["claude"],
                  {"name": "claude"}, ""):
        posted = []
        s, _ = serve_queue([_rec(1)],
                           post=lambda hook, content: posted.append(content),
                           webhook="https://discord.com/api/webhooks/1/x")
        status, _, _ = s.approve([1], actor=actor)
        assert status == 200, actor
        assert posted == ["✅ Approved: 1 (magi-review pb-www) (dashboard)"], actor
        assert "everyone" not in posted[0]
        assert "xxxx" not in posted[0]


def test_valid_actor_is_a_whitelist_of_one():
    assert dashboard._valid_actor({"numbers": [1], "actor": "claude"}) == "claude"
    for payload in ({"numbers": [1]}, {"numbers": [1], "actor": "mallory"},
                    {"numbers": [1], "actor": 7},
                    {"numbers": [1], "actor": True},
                    {"numbers": [1], "actor": "x" * 5000}, [1], None):
        assert dashboard._valid_actor(payload) == "", payload


# --- the auto-approved re-review on the page (2026-08-26) -----------------

def test_the_dashboard_shows_why_an_approved_review_needed_no_tick(serve_queue):
    """An `approved` row nobody remembers ticking is alarming. The row has to
    carry its own explanation, the same way the digest line does."""
    from worksweep.runner import AUTO_MAGI_WHY
    rec = _rec(13, status="approved")
    rec = QueueRecord(number=13, first_seen=NOW, last_seen=NOW,
                      item=dataclasses.replace(
                          rec.item, id="magi:pb-www!3997@newsha123",
                          why=AUTO_MAGI_WHY, status="approved"))
    s, _ = serve_queue([rec])
    status, _, body = s.request("GET", "/")
    assert status == 200
    html = body.decode("utf-8")
    assert "post-feedback re-review (auto)" in html


def test_an_auto_approved_row_offers_no_approve_checkbox(serve_queue):
    """It is already approved -- a checkbox would invite a no-op tick, and
    `is_actionable` is what keeps the page honest about that."""
    from worksweep.runner import AUTO_MAGI_WHY
    item = dataclasses.replace(_rec(13).item, id="magi:pb-www!3997@newsha123",
                               why=AUTO_MAGI_WHY, status="approved")
    assert dashboard.is_actionable(item) is False
    assert dashboard.has_checkbox(item) is False


# --- f-015: dismiss is attributed too (tribunal, 2026-08-26) --------------

def test_a_claude_dismiss_is_attributed_in_the_channel(serve_queue):
    """Approve carried actor attribution; dismiss did not. It is the other
    half of the same decision -- "I handled this" -- and the audit trail
    should tell the two hands apart on both."""
    posted = []
    rec = QueueRecord(number=1, first_seen=NOW, last_seen=NOW,
                      item=dataclasses.replace(_rec(1).item,
                                               executor="triage",
                                               status="proposed"))
    s, _ = serve_queue([rec], post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    body = json.dumps({"number": 1, "actor": "claude"})
    assert s.dismiss(1, body=body)[0] == 200
    assert [p for p in posted if p.startswith("🗑️")][0].endswith(
        " (dashboard · claude)")


def test_a_plain_dismiss_still_reads_exactly_as_before(serve_queue):
    posted = []
    rec = QueueRecord(number=1, first_seen=NOW, last_seen=NOW,
                      item=dataclasses.replace(_rec(1).item,
                                               executor="triage",
                                               status="proposed"))
    s, _ = serve_queue([rec], post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    assert s.dismiss(1)[0] == 200
    assert [p for p in posted if p.startswith("🗑️")][0].endswith(" (dashboard)")


def test_a_hostile_dismiss_actor_is_ignored_like_an_approve_one(serve_queue):
    posted = []
    rec = QueueRecord(number=1, first_seen=NOW, last_seen=NOW,
                      item=dataclasses.replace(_rec(1).item,
                                               executor="triage",
                                               status="proposed"))
    s, _ = serve_queue([rec], post=lambda hook, content: posted.append(content),
                       webhook="https://discord.com/api/webhooks/1/x")
    body = json.dumps({"number": 1, "actor": "@everyone " + "x" * 4000})
    assert s.dismiss(1, body=body)[0] == 200
    assert [p for p in posted if p.startswith("🗑️")][0].endswith(" (dashboard)")


# =============================================================================
# htmx live updates, Phase 1 -- every listener is delegated (decision 6)
#
# Direct `getElementById(...).addEventListener` bindings survive exactly one
# page load: the moment a region is swapped out from under them the new node
# has no listener and the control is dead chrome. Delegating on `document`
# makes the page swap-safe BEFORE any swap exists, so the refactor ships as its
# own behaviour-neutral commit.
# =============================================================================

def test_all_click_handlers_are_delegated():
    """Falsifying: re-introduce any direct binding and the count goes to 3."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert js.count("addEventListener(") == 2
    assert "document.addEventListener('click'," in js
    assert "document.addEventListener('change'," in js
    # the three ex-direct bindings now arrive through the delegated handler,
    # appended BELOW the pre-existing layout/filter/dismiss branches
    assert "e.target.closest('#approve-selected')" in js
    assert "e.target.closest('#approve-all')" in js
    assert "e.target.closest('#sync')" in js
    assert js.index("closest('[data-dismiss]')") < js.index("closest('#sync')")


def test_sync_done_requeries_the_button():
    """Falsifying: close over the button at load time and a swapped page can
    never un-spin it -- the Sync control stays 'syncing…' forever."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    body = js[js.index("functionsyncDone(label){"):]
    body = body[:body.index("functionkickSweep(")]
    assert "document.getElementById('sync')" in body
    # and the 3s un-spin timer re-queries too: 3s is long enough for a swap
    assert body.count("document.getElementById('sync')") == 2
    assert "varsync=document.getElementById('sync');" not in js[:js.index("functionscope(")]


def test_a_sync_click_without_the_button_is_a_no_op():
    """Falsifying: drop the guard and a click delegated after the button has
    been swapped away throws and POSTs nothing but a console error."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "if(!sync||syncing||sync.disabled){return;}" in js
    guard = js.index("if(!sync||syncing||sync.disabled){return;}")
    assert guard < js.index("fetch('/sweep'")


def test_dead_refresh_constants_are_gone():
    """Falsifying: both were unreferenced anywhere in the package -- leaving
    them invites a future reader to wire the wrong cadence back up."""
    assert not hasattr(dashboard, "_REFRESH_SECONDS")
    assert not hasattr(dashboard, "_MTIME_POLL_SECONDS")
    # the live cadences that ARE real stay
    assert dashboard._POLL_SECONDS == 10


# =============================================================================
# htmx live updates, Phase 2 -- the vendored library is inlined, not referenced
#
# A CDN tag would be a second network dependency for a page whose whole point
# is being one file; a same-origin `<script src>` would be a second deploy
# surface. The library is a repo file, read once at import and emitted inline,
# which keeps `test_page_is_self_contained` true verbatim.
# =============================================================================

_HTMX_SHA256 = "60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9"


def _static(name):
    return os.path.join(os.path.dirname(os.path.abspath(dashboard.__file__)),
                        "static", name)


def test_vendored_htmx_integrity():
    """Falsifying: flip one byte of either file and this fails. A vendored
    dependency nobody can verify is just a large unreviewed diff."""
    import hashlib
    digest = hashlib.sha256(open(_static("htmx.min.js"), "rb").read()).hexdigest()
    pin = dict(line.split(None, 1) for line in
               open(_static("htmx.version")).read().splitlines() if line.strip())
    assert pin["sha256"] == _HTMX_SHA256
    assert digest == pin["sha256"]
    assert pin["htmx.org"] == "2.0.7"


def test_htmx_is_inlined_not_referenced():
    page = _page([_rec(1)])
    src = open(_static("htmx.min.js")).read()
    assert src in page                                  # byte-for-byte, inline
    assert 'src="/static/' not in page
    assert "htmx.min.js" not in page
    # and it can never break out of the <script> element that carries it
    assert "</script" not in src


def test_htmx_is_emitted_after_the_layout_restore_script():
    """AC #31 is a 400-character lookback around `localStorage.getItem`;
    emitting 51KB of htmx ahead of it would push the restore script's own
    source out of that window and the test would pass for the wrong reason."""
    page = _page([_rec(1)])
    assert page.index("localStorage.getItem") < page.index("htmx")
    assert page.index("htmx") < page.index("<body")     # still before paint
    head = page[:page.index("</head>")]
    assert head.count("<script>") == 2


def test_js_assertions_are_scoped_below_htmx():
    """Falsifying in BOTH directions: it fails if htmx is not really inlined,
    and it fails if `_script()` stops scoping. Every `not in` assertion about
    our own JS depends on this -- htmx's source contains `pushState`,
    `location.search`, `location.href` and `location.reload` of its own."""
    page = _page([_rec(1)])
    for token in ("pushState", "location.search", "location.href"):
        assert token in page, token                     # htmx really is there
        assert token not in _script(page), token        # and we scope past it
    assert _script(page).endswith("})();\n")
    # Our own block must never contain the literal opening tag -- not because
    # HTML minds (only `</script` closes an element) but because _script()
    # finds the LAST one, so a stray mention in a comment would silently scope
    # every JS assertion in this file to a fragment of itself.
    assert "<script" not in _script(page)
    assert "<script" not in dashboard._BODY_SCRIPT
    assert "<script" not in dashboard._HEAD_SCRIPT


def test_missing_htmx_asset_fails_at_import(tmp_path):
    """Falsifying: degrade to a warning and a deploy that forgot `static/`
    serves a page whose live updates silently do nothing -- the worst possible
    failure for a page whose job is showing you what changed. KeepAlive turns
    a raised import into a visible restart loop in the .err file."""
    import importlib.util

    def _load(static_files):
        pkg = tmp_path / str(len(list(tmp_path.iterdir())))
        (pkg / "static").mkdir(parents=True)
        for name, body in static_files.items():
            (pkg / "static" / name).write_text(body)
        mod_path = pkg / "dashboard.py"
        mod_path.write_text(open(dashboard.__file__).read())
        name = "worksweep.dashboard_asset_probe"
        spec = importlib.util.spec_from_file_location(name, str(mod_path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod          # dataclasses resolve via sys.modules
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(name, None)

    with pytest.raises(FileNotFoundError):
        _load({})                                        # asset missing
    with pytest.raises(RuntimeError):
        _load({"htmx.min.js": "   \n"})                  # asset empty
    # control: the same loader succeeds when the asset is really there
    _load({"htmx.min.js": open(_static("htmx.min.js")).read()})


# =============================================================================
# htmx live updates, Phase 3 -- one fragment endpoint, five out-of-band regions
#
# `location.reload()` was the old refresh: it discards a selection, tears an
# open dialog and repaints 51KB of page to change one row. GET /fragments
# returns the five dynamic regions marked for out-of-band swap, so one round
# trip updates the whole page atomically and in place.
# =============================================================================

_REGIONS = ("telemetry", "sync-region", "sections", "branches", "bar")


def _region(doc, rid):
    """Inner HTML of `<div id="rid" ...>`, matched by counting div tags.

    Deliberately independent of how the composer builds the container: it
    re-derives the boundary from the document, so the byte-comparison below is
    a real comparison and not the composer agreeing with itself.
    """
    m = re.search(r'<div id="%s"[^>]*>' % re.escape(rid), doc)
    assert m, f"no container #{rid}"
    i = j = m.end()
    depth = 1
    while depth:
        nxt = re.compile(r"<div\b|</div>").search(doc, j)
        assert nxt, f"unbalanced #{rid}"
        depth += 1 if nxt.group(0) == "<div" else -1
        j = nxt.end()
    return doc[i:j - len("</div>")]


def test_fragments_match_page_regions():
    """Falsifying: let the two composers drift by one byte and this goes red.
    A fragment endpoint that renders a region differently from the page is a
    page that changes appearance every time it updates."""
    recs = [_rec(1), _rec(2, status="running"), _rec(3, status="done"),
            _rec(4, executor="triage")]
    page = _page(recs)
    frag = dashboard.render_fragments(recs, NOW, 1_750_000_000.0)
    for rid in _REGIONS:
        assert _region(page, rid) == _region(frag, rid), rid
    # and the fragment response is ONLY those five, each marked for oob swap
    assert re.findall(r'<div id="([a-z-]+)" class="oob" hx-swap-oob="true">',
                      frag) == list(_REGIONS)
    assert 'hx-swap-oob' not in _markup(page)  # the page itself swaps nothing
    assert frag.count("hx-swap-oob") == 5


def test_fragment_targets_exist_in_every_state():
    """Falsifying: without a container that survives the transition, approving
    the LAST item leaves the page showing rows that no longer exist -- a
    sharper version of the stale-page bug this build exists to kill."""
    for label, recs in (("empty", []), ("full", [_rec(1), _rec(2)])):
        page, frag = _page(recs), dashboard.render_fragments(recs, NOW, 1.0)
        for rid in _REGIONS:
            assert f'<div id="{rid}" class="oob"' in page, (label, rid)
            assert f'<div id="{rid}" class="oob" hx-swap-oob="true">' in frag, (label, rid)
    # the empty page's all-clear text lives INSIDE #sections, so the swap that
    # empties the queue also replaces the rows with it
    assert "Nothing needs you right now" in _region(_page([]), "sections")
    assert _region(_page([]), "branches") == ""
    assert _region(_page([]), "bar") == ""
    # a container is a swap target, never a layout box: .head is flex and .bar
    # is position:sticky, both of which a real wrapper would quietly change
    assert ".oob{display:contents}" in _style(_page([])).replace(" ", "")


def test_fragments_needs_no_csrf_header(serve_queue):
    """Mirrors /mtime: a read that leaks only what the page already shows."""
    s, _ = serve_queue([_rec(1)])
    status, headers, body = s.request("GET", "/fragments", None, {})
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b'hx-swap-oob="true"' in body


def test_fragments_is_get_only(serve_queue):
    s, _ = serve_queue([_rec(1)])
    status, _, _ = s.request("POST", "/fragments", "", {"X-Worksweep": "approve"})
    assert status == 404


def test_fragment_render_failure_is_a_500(serve_queue, monkeypatch):
    """The launchd agent is KeepAlive: an exception escaping the handler is a
    restart loop. Falsifying: drop the catch and the server dies mid-suite."""
    s, _ = serve_queue([_rec(1)])
    monkeypatch.setattr(dashboard, "render_fragments",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    status, headers, body = s.request("GET", "/fragments")
    assert status == 500
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert b"boom" not in body                  # never leak the traceback
    # and the same server is still serving on the next request
    assert s.request("GET", "/mtime")[0] == 200


def test_actions_refresh_fragments_not_the_page():
    """Falsifying in both halves: `location.reload` reappearing in send() fails
    the first, and losing the fragment call fails the second. The `not in` half
    is only sound because _script() scopes past htmx's own two reloads."""
    page = _page([_rec(1)])
    js = _script(page).replace(" ", "").replace("\n", "")
    assert "location.reload" not in js
    assert "location.reload" in page             # ...but htmx's own still is
    assert ("if(r.status===200){inflight=false;"
            "try{clearSelection();applyFragments();}") in js
    assert "htmx.ajax('GET','/fragments',{target:'body',swap:'none'})" in js


def test_deferred_swap_holds_and_drains():
    """Falsifying, four ways. The hold stops rows shifting under a half-built
    selection; the chip stops the hold being SILENT (the original bug); and the
    drain paths stop the hold being PERMANENT (the same bug, reborn). Deleting
    any one fails exactly one of these assertions.

    Two of the three drains are live. The confirm-close one is a belt:
    confirm() blocks the JS thread, so nothing can set `pending` while the
    dialog is open. It is pinned anyway because that is a property of
    confirm(), not of our code -- swap in a non-blocking dialog and it becomes
    the only thing standing between a held update and a permanent hold."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    # hold + visible chip
    assert "if(inflight||(!force&&busy())){pending=true;chip(true);return;}" in js
    assert "functionchip(on){varc=document.getElementById('pending');if(c){c.hidden=!on;}}" in js
    # drain 1: the last checkbox came off
    assert "if(pending&&selected().length===0){applyFragments();}" in js
    # drain 2 (belt): the confirm dialog closed, either way
    assert "confirming=false;if(pending){applyFragments();}" in js
    # drain 3: the user tapped the chip -- which forces past the selection
    # guard, because tapping it IS the request to update now
    assert "if(e.target.closest('#pending')){applyFragments(true);return;}" in js


def test_the_pending_chip_is_rendered_hidden_in_every_state():
    for recs in ([], [_rec(1)]):
        page = _page(recs)
        chip = re.search(r'<button[^>]*id="pending"[^>]*>', page)
        assert chip is not None
        assert "hidden" in chip.group(0)
        assert "queue changed" in _region(page, "sync-region")
    css = _style(_page([])).replace(" ", "")
    assert ".pending[hidden]{display:none}" in css


def test_post_swap_reapplies_selection_and_filters():
    """AC #8: a swap must not silently drop a selection the user can still see,
    and the bar's disabled/hidden/count state must be recomputed against the
    rows that actually arrived."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    # captured immediately BEFORE the request, re-applied after the swap
    assert "carried=selected();" in js
    assert js.index("carried=selected();") < js.index("htmx.on('htmx:afterSwap'")
    assert ("vartwins=document.querySelectorAll"
            "('input[type=checkbox][value=\"'+carried[i]+'\"]');") in js
    assert "applyFilter();refresh();marks();" in js
    # and nothing is bound with addEventListener: htmx.on keeps the page's
    # listener budget at the two delegated ones
    assert js.count("addEventListener(") == 2


def test_live_poll_refreshes_fragments_and_never_reloads():
    """The 10s mtime poll is the fallback path now, so it must refresh in place
    exactly like the SSE path does -- and keep polling forever either way."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "tick" not in js
    assert "FALLBACK_MS" not in js
    assert "baseMtime" not in js
    assert "if(lastMtime&&t!==lastMtime){applyFragments();}" in js
    assert "lastMtime=t;" in js
    assert js.count("setTimeout(poll,POLL_MS);") == 3   # armed + both settles


def test_last_mtime_is_reseeded_from_every_swap():
    """Falsifying: leave it as the load-time DOM read and the poll either fires
    on every tick forever or never fires again -- the captured token cannot
    survive its own region being re-rendered."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    hook = js[js.index("htmx.on('htmx:afterSwap'"):]
    assert "vars=document.getElementById('sync');" in hook
    assert "lastMtime=s?(s.getAttribute('data-mtime')||'').trim():lastMtime;" in hook


def test_the_emitted_javascript_actually_parses():
    """The one failure no string assertion in this file can see.

    Every other JS test here asserts that some substring is present; a stray
    brace would satisfy all of them and still take the entire page down, since
    both blocks are one <script> each. Skipped rather than vendored: the repo
    is stdlib-only by policy, so this is a local sharpener, not a dependency.
    """
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("no node on PATH")
    for name, source in (("head", dashboard._HEAD_SCRIPT),
                         ("body", dashboard._BODY_SCRIPT)):
        path = os.path.join(os.path.dirname(dashboard.__file__), f".{name}.check.js")
        try:
            with open(path, "w") as fh:
                fh.write(source)
            r = subprocess.run([node, "--check", path], capture_output=True)
            assert r.returncode == 0, (name, r.stderr.decode())
        finally:
            if os.path.exists(path):
                os.remove(path)


# =============================================================================
# htmx live updates, Phase 4 -- GET /events, and the poll demoted to a fallback
#
# The 10s poll was one round trip every ten seconds per open tab forever, to
# learn "no, still nothing" almost every time. One held connection per viewer
# says it once, when it happens. The poll stays, as the degraded path.
# =============================================================================

def test_events_emits_on_mtime_change(serve_queue):
    """Falsifying: remove the stat loop or malform the framing and this fails.
    Deliberately run at the REAL 1s cadence against AC1's 2s budget, not a
    monkeypatched one -- the budget is the thing being tested."""
    s, qpath = serve_queue([_rec(1)])
    st = os.stat(qpath)
    with s.stream() as r:
        head = r.headers()
        assert head.splitlines()[0].startswith("HTTP/1.1 200")
        assert "Content-Type: text/event-stream" in head
        # every stream opens by announcing where the queue stands (see
        # test_events_announces_the_current_token_on_connect)
        assert r.frame(timeout=2).splitlines()[1] == "data: " + \
            dashboard.mtime_token(st.st_mtime, st.st_size)
        os.utime(qpath, (1_800_000_000, 1_800_000_000))
        lines = r.frame(timeout=2).splitlines()
    assert lines[0] == "event: queue"
    assert lines[1] == "data: " + dashboard.mtime_token(1_800_000_000,
                                                        os.path.getsize(qpath))
    assert len(lines) == 2                       # ...and then the blank line


def test_events_sends_heartbeat_comments(serve_queue, monkeypatch):
    """A comment line is invisible to EventSource but it is still a WRITE, so a
    client that went away surfaces here as an error instead of pinning a
    connection and a thread until the process restarts."""
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.05)
    s, _ = serve_queue([_rec(1)])
    with s.stream() as r:
        r.headers()
        assert r.frame(timeout=3).startswith("event: queue")   # the connect one
        assert r.frame(timeout=3) == ": heartbeat"
        assert r.frame(timeout=3) == ": heartbeat"


def test_events_holds_no_write_lock():
    """Falsifying: a stream is open for hours. Taking either lock inside it
    would park every approve on the page behind a reader that never finishes --
    the exact inversion the short-window lock discipline exists to prevent."""
    fn = next(n for n in ast.walk(ast.parse(_dashboard_src()))
              if isinstance(n, ast.FunctionDef) and n.name == "_events")
    used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    for forbidden in ("_WRITE_LOCK", "write_lock", "load_queue", "save_queue"):
        assert forbidden not in used, forbidden
    assert "_queue_stamp" in used                # it stats a path, nothing more


def test_approve_during_open_stream_completes(serve_queue):
    """The behavioural half of the assertion above: if the stream held a lock,
    this approve would sit on the harness's 5s timeout and raise."""
    s, _ = serve_queue([_rec(1), _rec(2)])
    with s.stream() as r:
        r.headers()
        status, _, body = s.approve([1])
        assert status == 200
        assert json.loads(body)["approved"] == [1]
        assert s.request("GET", "/fragments")[0] == 200


def test_http11_responses_carry_content_length(serve_queue):
    """`protocol_version = "HTTP/1.1"` is class-wide, so every route switched to
    keep-alive at once. It is safe only because _send always sets a
    Content-Length -- a response without one hangs a keep-alive client until it
    times out. This makes that a regression test, not a standing assumption."""
    assert dashboard.DashboardHandler.protocol_version == "HTTP/1.1"
    s, _ = serve_queue([_rec(1)])
    for path in ("/", "/mtime", "/fragments", "/definitely-not-a-route"):
        status, headers, body = s.request("GET", path)
        assert "Content-Length" in headers, path
        assert int(headers["Content-Length"]) == len(body), path
    with s.stream("/mtime") as r:
        assert r.headers().splitlines()[0].startswith("HTTP/1.1 200")


def test_a_rejected_post_closes_the_connection(serve_queue):
    """Falsifying, and only reachable under HTTP/1.1: a 403 or 404 that never
    read the request body would leave that body on the socket to be parsed as
    the NEXT request line -- so a CSRF rejection would be followed by a mystery
    400 on a connection the browser thought was clean."""
    s, _ = serve_queue([_rec(1)])
    payload = json.dumps({"numbers": [1]}).encode()
    for path, code in (("/approve", b"403"), ("/definitely-not-a-route", b"404")):
        sock = socket.create_connection((s.host, s.port), timeout=5)
        try:
            sock.sendall(
                f"POST {path} HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            got = _Reader(sock).headers()
        finally:
            sock.close()
        assert code.decode() in got.splitlines()[0], (path, got.splitlines()[0])
        assert "Connection: close" in got, path
        assert "Content-Length:" in got, path


def test_events_thread_exits_on_client_disconnect(tmp_path, monkeypatch):
    """Falsifying: without a write in the loop, a vanished client leaves a
    thread spinning on a socket nobody is reading until the agent restarts.
    Also pins that close() never blocks on it -- daemon_threads keeps the
    request thread out of the list server_close() joins."""
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.02)
    qpath = os.path.join(str(tmp_path), "queue.json")
    save_queue(qpath, [_rec(1)])
    s = _Server(qpath)
    before = set(threading.enumerate())
    sock = socket.create_connection((s.host, s.port), timeout=5)
    sock.sendall(f"GET /events HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n\r\n".encode())
    _Reader(sock).headers()                      # the loop is definitely running
    spawned = [t for t in threading.enumerate() if t not in before]
    assert spawned, "no thread was serving the stream"
    sock.close()
    started = time.monotonic()
    s.close()
    assert time.monotonic() - started < 5, "close() blocked on the stream thread"
    for t in spawned:
        t.join(timeout=5)
        assert not t.is_alive(), t.name


def test_sse_error_falls_back_to_fragment_poll():
    """Falsifying: without the fallback, a proxy that refuses to stream or a
    browser quirk leaves the page frozen with no refresh path at all -- silent,
    which is the failure mode this whole build exists to remove."""
    page = _page([_rec(1)])
    js = _script(page).replace(" ", "").replace("\n", "")
    assert "vares=newEventSource('/events');" in js
    assert "htmx.on(es,'queue',function(){applyFragments();});" in js
    # CLOSED (2) only: EventSource retries CONNECTING by itself, and arming the
    # poll on every transient blip would give the page two refresh paths at once
    assert "es.onerror=function(){if(es.readyState===2){armPoll();}};" in js
    assert "if(!window.EventSource){armPoll();return;}" in js
    # the two transport-failure arms, plus the durable-approve degradation
    assert js.count("armPoll();") == 3
    assert "location.reload" not in js
    assert "location.reload" in page             # ...still htmx's own
    assert js.count("addEventListener(") == 2    # htmx.on keeps the budget



# =============================================================================
# Review round 2 -- hardening found by the security and edge-case passes
# =============================================================================

def test_mtime_token_carries_the_size_so_a_same_tick_rewrite_is_seen():
    """Falsifying: drop the size and a queue rewritten twice inside one
    filesystem tick is invisible. APFS has nanosecond mtimes, but HFS+ and most
    network filesystems have one-second granularity, and a sweep landing twice
    in a second is exactly the busy moment the page most needs to keep up with.
    """
    assert dashboard.mtime_token(1_750_000_000.0, 40) == "1750000000.000000:40"
    # same instant, different content -> a different token
    assert (dashboard.mtime_token(1_750_000_000.0, 40)
            != dashboard.mtime_token(1_750_000_000.0, 41))
    # a missing queue is still the one distinguished value
    assert dashboard.mtime_token(None, None) == "0"
    assert dashboard.mtime_token(0, 12) == "0"


def test_queue_stamp_is_one_stat_and_degrades_to_none(tmp_path, monkeypatch):
    """One stat, not two: reading mtime and size separately could straddle a
    rewrite and mint a token for a state that never existed on disk."""
    qpath = os.path.join(str(tmp_path), "queue.json")
    save_queue(qpath, [_rec(1)])
    st = os.stat(qpath)
    assert dashboard._queue_stamp(qpath) == (st.st_mtime, st.st_size)
    assert dashboard._queue_stamp(os.path.join(str(tmp_path), "gone.json")) == (None, None)
    # ...and it really is one call. Two lookups could straddle a rewrite and
    # pair an old mtime with a new size -- a token for a state never on disk.
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    monkeypatch.setattr(os.path, "getmtime",
                        lambda *a: pytest.fail("_queue_stamp made a second lookup"))
    monkeypatch.setattr(os.path, "getsize",
                        lambda *a: pytest.fail("_queue_stamp made a second lookup"))
    dashboard._queue_stamp(qpath)
    assert len(calls) == 1


def test_the_page_and_the_mtime_route_mint_the_same_token(serve_queue):
    """Falsifying: if the page embedded a token of a different SHAPE from the
    one /mtime returns, the very first poll would see a mismatch and refresh --
    then do it again ten seconds later, forever."""
    s, qpath = serve_queue([_rec(1)])
    page = s.request("GET", "/")[2].decode()
    embedded = re.search(r'<button[^>]*id="sync"[^>]*data-mtime="([^"]+)"', page).group(1)
    served = s.request("GET", "/mtime")[2].decode()
    assert embedded == served
    st = os.stat(qpath)
    assert served == dashboard.mtime_token(st.st_mtime, st.st_size)
    # and the fragment response carries that same token, so a swap re-seeds
    # lastMtime with a value the poll can compare against
    frag = s.request("GET", "/fragments")[2].decode()
    assert f'data-mtime="{served}"' in frag


def test_events_announces_the_current_token_on_connect(serve_queue):
    """Falsifying: without this, a stream seeds `last` from the file as it is
    NOW, so nothing that happened before the connection opened is ever
    announced. It also has to be IMMEDIATE -- inside the loop it would be one
    stat interval late on every single page load."""
    s, qpath = serve_queue([_rec(1)])
    st = os.stat(qpath)
    with s.stream() as r:
        r.headers()
        lines = r.frame(timeout=0.8).splitlines()   # well inside one stat pass
    assert lines == ["event: queue",
                     "data: " + dashboard.mtime_token(st.st_mtime, st.st_size)]


def test_a_change_made_while_the_stream_was_down_is_announced_on_reconnect():
    """THE reconnect gap, and the reason this is a blocker: EventSource
    reconnects by itself after an agent restart or a tailnet blip, and every
    change that landed in that window used to be swallowed -- silent-stale,
    reborn, precisely when it matters most."""
    import tempfile
    d = tempfile.mkdtemp()
    qpath = os.path.join(d, "queue.json")
    save_queue(qpath, [_rec(1)])
    s = _Server(qpath)
    try:
        with s.stream() as r:
            r.headers()
            first = r.frame(timeout=2)
        # the stream is now closed; the queue moves while nobody is listening
        save_queue(qpath, [_rec(1), _rec(2), _rec(3)])
        with s.stream() as r:
            r.headers()
            second = r.frame(timeout=2)
        assert second.startswith("event: queue")
        assert second != first, "the reconnect announced a stale token"
        st = os.stat(qpath)
        assert second.splitlines()[1] == "data: " + dashboard.mtime_token(
            st.st_mtime, st.st_size)
    finally:
        s.close()


def test_events_says_nothing_on_connect_when_there_is_no_queue_file(tmp_path,
                                                                    monkeypatch):
    """Falsifying: announcing the "0" token would make every page that opened
    before the first sweep flash the all-clear at itself for no reason. No
    queue file is not a change, it is an absence."""
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.05)
    s = _Server(os.path.join(str(tmp_path), "never-swept.json"))
    try:
        with s.stream() as r:
            r.headers()
            assert r.frame(timeout=3) == ": heartbeat"
    finally:
        s.close()


def test_events_caps_concurrent_streams(serve_queue, monkeypatch):
    """Bounded by code, not by trusting that only Chandler ever opens tabs.
    Every stream costs a thread and a socket for as long as it is held; without
    a cap, a tab-opening loop is a trivial resource exhaustion on the agent."""
    monkeypatch.setattr(dashboard, "_MAX_EVENT_STREAMS", 2)
    monkeypatch.setattr(dashboard, "_EVENT_SLOTS", threading.BoundedSemaphore(2))
    # so a dropped client is noticed (and its slot freed) in milliseconds
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.02)
    s, _ = serve_queue([_rec(1)])
    with s.stream() as a, s.stream() as b:
        a.headers()
        b.headers()
        status, headers, _ = s.request("GET", "/events")
        assert status == 503
        assert "Content-Length" in headers        # still a well-framed refusal
        # the rest of the dashboard is entirely unaffected by a full stream pool
        assert s.request("GET", "/")[0] == 200
        assert s.request("GET", "/fragments")[0] == 200
    # ...and a slot is released the moment a stream ends, so the cap bounds
    # CONCURRENCY and is never a lifetime budget
    deadline = time.monotonic() + 5
    while True:
        with s.stream() as c:
            head = c.headers()
        if head.splitlines()[0].startswith("HTTP/1.1 200"):
            break
        assert time.monotonic() < deadline, "a stream slot was never released"
        time.sleep(0.05)


def test_a_stream_releases_the_slot_it_actually_took(monkeypatch, tmp_path):
    """Falsifying: release the module global instead of the object acquired
    from, and a pool swapped underneath a live stream over-releases -- which
    BoundedSemaphore raises on, inside a `finally`, in a handler where nothing
    is allowed to raise. Exactly the shape a test double creates."""
    fn = next(n for n in ast.walk(ast.parse(_dashboard_src()))
              if isinstance(n, ast.FunctionDef) and n.name == "_events")
    releases = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "release"]
    assert len(releases) == 1
    assert isinstance(releases[0].func.value, ast.Name)
    assert releases[0].func.value.id != "_EVENT_SLOTS", (
        "released the global, not the object the slot was taken from")
    # and it behaves: a pool swapped mid-stream does not blow up the handler
    monkeypatch.setattr(dashboard, "_EVENT_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.02)
    qpath = os.path.join(str(tmp_path), "queue.json")
    save_queue(qpath, [_rec(1)])
    s = _Server(qpath)
    try:
        with s.stream() as r:
            r.headers()
            monkeypatch.setattr(dashboard, "_EVENT_SLOTS",
                                threading.BoundedSemaphore(1))
        deadline = time.monotonic() + 5
        while True:
            with s.stream() as c:
                head = c.headers()
            if head.splitlines()[0].startswith("HTTP/1.1 200"):
                break
            assert time.monotonic() < deadline, "the original slot never came back"
            time.sleep(0.05)
    finally:
        s.close()


def test_an_idle_keep_alive_connection_is_reaped(tmp_path, monkeypatch):
    """Finding 2. HTTP/1.1 made every connection reusable, and a reused
    connection with nobody on it holds a thread and a socket until the process
    restarts. A browser tab left open all weekend is not an attack, it is
    Tuesday. stdlib's handle_one_request turns the read timeout into a close."""
    assert dashboard.DashboardHandler.timeout == 65
    monkeypatch.setattr(dashboard.DashboardHandler, "timeout", 0.2)
    qpath = os.path.join(str(tmp_path), "queue.json")
    save_queue(qpath, [_rec(1)])
    s = _Server(qpath)
    try:
        before = set(threading.enumerate())
        sock = socket.create_connection((s.host, s.port), timeout=5)
        try:
            sock.sendall(f"GET /mtime HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n\r\n"
                         .encode())
            r = _Reader(sock)
            head = r.headers()
            assert head.splitlines()[0].startswith("HTTP/1.1 200")
            assert "Connection: close" not in head      # genuinely keep-alive
            spawned = [t for t in threading.enumerate() if t not in before]
            assert spawned, "no thread served the request"
            # now go idle and say nothing at all
            sock.settimeout(5)
            rest = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break                              # the server hung up
                rest += chunk
        finally:
            sock.close()
        for t in spawned:
            t.join(timeout=5)
            assert not t.is_alive(), t.name
    finally:
        s.close()


def test_an_open_stream_outlives_the_idle_timeout(serve_queue, monkeypatch):
    """The other half of finding 2, and the risk in it: the handler timeout is
    a SOCKET timeout, so setting it carelessly would cut every event stream at
    65s. It does not, because the stream writes far more often than that -- and
    this proves it with the ratio preserved, not just the numbers shrunk."""
    # the production relationship, asserted before it is scaled down
    assert (dashboard.DashboardHandler.timeout
            > dashboard._EVENT_HEARTBEAT_SECONDS * 2)
    monkeypatch.setattr(dashboard.DashboardHandler, "timeout", 0.4)
    monkeypatch.setattr(dashboard, "_EVENT_STAT_SECONDS", 0.01)
    monkeypatch.setattr(dashboard, "_EVENT_HEARTBEAT_SECONDS", 0.05)
    s, _ = serve_queue([_rec(1)])
    with s.stream() as r:
        r.headers()
        assert r.frame(timeout=2).startswith("event: queue")
        # comfortably past the timeout, several heartbeat intervals in
        for _ in range(6):
            assert r.frame(timeout=2) == ": heartbeat"


def test_a_chunked_post_is_refused_rather_than_desyncing(serve_queue):
    """Finding 3. _body_bytes reads exactly Content-Length bytes, so a chunked
    request reads ZERO and leaves the entire body on the wire -- which, on a
    connection HTTP/1.1 says is reusable, becomes the next request line. This
    server has no reason to accept chunked from its own page, so it refuses it
    and closes rather than half-understanding it."""
    s, _ = serve_queue([_rec(1)])
    body = b"1a\r\n{\"numbers\": [1], \"a\": 1}\r\n0\r\n\r\n"
    sock = socket.create_connection((s.host, s.port), timeout=5)
    try:
        sock.sendall(
            f"POST /approve HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n"
            "X-Worksweep: approve\r\nContent-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n\r\n".encode() + body)
        head = _Reader(sock).headers()
    finally:
        sock.close()
    assert "400" in head.splitlines()[0], head.splitlines()[0]
    assert "Connection: close" in head
    # and the half-read request approved exactly nothing
    frag = s.request("GET", "/fragments")[2].decode()
    assert 'data-st="proposed"' in frag
    assert 'data-st="approved"' not in frag


@pytest.mark.parametrize("body,label", [
    (b"not json at all", "unparseable"),
    (b'{"numbers": "one"}', "bad numbers envelope"),
    (b'{"nope": 1}', "wrong envelope"),
])
def test_every_400_closes_the_connection(serve_queue, body, label):
    """Finding 3, the general case: a refusal is the one moment the server and
    the client most disagree about what was sent, so it is the worst possible
    moment to keep the socket and guess. Every 400 closes."""
    s, _ = serve_queue([_rec(1)])
    sock = socket.create_connection((s.host, s.port), timeout=5)
    try:
        sock.sendall(
            f"POST /approve HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n"
            "X-Worksweep: approve\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        head = _Reader(sock).headers()
    finally:
        sock.close()
    assert "400" in head.splitlines()[0], (label, head.splitlines()[0])
    assert "Connection: close" in head, label
    assert "Content-Length:" in head, label


def test_a_dismiss_with_a_bad_number_also_closes(serve_queue):
    s, _ = serve_queue([_rec(1, executor="triage")])
    sock = socket.create_connection((s.host, s.port), timeout=5)
    body = b'{"number": true}'                 # bool is not an int, per _valid_number
    try:
        sock.sendall(
            f"POST /dismiss HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n"
            "X-Worksweep: approve\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        head = _Reader(sock).headers()
    finally:
        sock.close()
    assert "400" in head.splitlines()[0]
    assert "Connection: close" in head


def test_a_chunked_sweep_is_refused_and_kicks_nothing(serve_queue):
    """The sharp edge of finding 3, and the reason the Transfer-Encoding check
    is not merely belt to _reject's braces: /sweep takes NO body, so a chunked
    POST would read zero bytes, look perfectly valid, kick a real sweep, and
    leave its body on the wire to be read as the next request line."""
    kicks = []
    s, _ = serve_queue([_rec(1)], sweep=lambda: kicks.append(1))
    body = b"4\r\nnoop\r\n0\r\n\r\n"
    sock = socket.create_connection((s.host, s.port), timeout=5)
    try:
        sock.sendall(
            f"POST /sweep HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n"
            "X-Worksweep: approve\r\nTransfer-Encoding: chunked\r\n\r\n"
            .encode() + body)
        head = _Reader(sock).headers()
    finally:
        sock.close()
    assert "400" in head.splitlines()[0], head.splitlines()[0]
    assert "Connection: close" in head
    assert kicks == [], "a chunked request kicked a real sweep"
    # the ordinary Content-Length sweep still works, so this refuses the
    # framing and not the route
    assert s.sweep()[0] == 202
    assert kicks == [1]


def test_refreshes_are_single_flight():
    """Finding 6. Two /fragments requests in the air at once race, and the page
    settles on whichever RESPONSE lands last -- which is not necessarily the
    one that read the queue last. A burst (stream event, then a poll, then an
    action) would leave the page showing the older of two states, permanently."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "functionswapInFlight(){returncycle!==settled;}" in js
    assert "if(swapInFlight()){pending=true;return;}" in js
    # the guard is claimed BEFORE the request goes out, or it guards nothing
    i = js.index("if(swapInFlight()){pending=true;return;}")
    assert i < js.index("htmx.ajax('GET','/fragments'")
    assert "varmine=++cycle;" in js
    # settled can only ever move FORWARD to the cycle that finished, so a late
    # settle from an old request cannot release a newer one's guard
    assert "if(settled<mine){settled=mine;}" in js


def test_the_after_swap_hook_applies_once_per_refresh():
    """Finding 7. The vendored htmx 2.0.7 source fires afterSwap exactly once,
    on the ajax target, even for swap:'none' -- settleInfo is {tasks:[],
    elts:[target]}. That stays the primary contract. This guard is what makes
    being WRONG about it merely wasteful instead of silently corrupting: a
    second application would re-apply a stale `carried` over a page the user
    has since touched, and double-drain `pending`."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    hook = js[js.index("htmx.on('htmx:afterSwap',function(){"):]
    assert hook.startswith("htmx.on('htmx:afterSwap',function(){"
                           "if(!swapInFlight()){return;}settled=cycle;"), hook[:120]


def test_a_failed_refresh_is_visible_and_retryable():
    """Finding 5. A refresh that never lands used to leave the page silently
    stale AND wedge the single-flight guard shut. Now it puts the chip up,
    which is both the notification and the retry."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert ("functionswapFailed(){if(!swapInFlight()){return;}"
            "settled=cycle;pending=true;chip(true);}") in js
    assert "htmx.on('htmx:responseError',swapFailed);" in js
    assert "htmx.on('htmx:sendError',swapFailed);" in js
    # and the promise settles the guard whatever happens, so a response htmx
    # never turns into an event cannot lock the page out of refreshing forever
    assert ".then(settle,settle);" in js


def test_a_swap_reconciles_the_sync_button_state():
    """Finding 4. `syncing` is client-side memory of a sweep that was kicked.
    The swapped-in header is server truth and its button is fresh, so a stale
    flag would keep re-disabling it and leave Sync dead for up to SYNC_MAX_MS
    -- 80 seconds after the sweep it was waiting for already landed."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    hook = js[js.index("htmx.on('htmx:afterSwap',function(){"):]
    hook = hook[:hook.index("});")]
    assert "syncing=false;" in hook


def test_an_uncheck_during_a_refresh_is_never_reverted():
    """Finding 14 (security). `carried` is captured before the request and
    re-applied after the swap. A box the user unticks WHILE that request is in
    the air would be silently re-ticked by the re-apply -- putting a row the
    user explicitly refused back on offer, and back into the next submitted
    set. An explicit deselection must win over a stale capture."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    # Asserted INSIDE the delegated change handler -- at the moment of the
    # click, not reconstructed afterwards from a DOM that has since been
    # swapped -- and as one contiguous block, so a dead `if(false)` around it
    # cannot pass by matching the single-flight guard in applyFragments().
    change = js[js.index("document.addEventListener('change',"):]
    assert ("if(swapInFlight()){"
            "varv=parseInt(b.value,10);"
            "carried=carried.filter(function(x){returnx!==v;});"
            "if(b.checked){carried.push(v);}"
            "}") in change


def test_a_durable_approve_never_reports_itself_as_failed():
    """Finding 8. send()'s catch covers the whole promise chain, so a DOM error
    in the post-200 follow-up would surface as "Request failed" for an approval
    that is already durable on disk -- and the honest reaction to that message
    is to approve it again."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert ("try{clearSelection();applyFragments();}"
            "catch(err){pending=true;chip(true);armPoll();}") in js
    i = js.index("try{clearSelection();applyFragments();}")
    assert i < js.index(".catch(function(e){inflight=false;alert('Requestfailed:'")


def test_htmx_is_configured_shut_before_it_is_used():
    """Finding 12, defense in depth. Our fragments carry no <script> and every
    request this page makes is same-origin; saying so means a malformed or
    tampered response cannot smuggle one past htmx's defaults, which allow both.
    Verified against the vendored source: 2.0.7 ships allowScriptTags:true and
    selfRequestsOnly:true, so one of these is a change and one is a pin."""
    js = _script(_page([_rec(1)])).replace(" ", "").replace("\n", "")
    assert "htmx.config.allowScriptTags=false;" in js
    assert "htmx.config.selfRequestsOnly=true;" in js
    # set before the first request this page could ever make
    assert js.index("htmx.config.allowScriptTags") < js.index("htmx.ajax(")
    assert js.index("htmx.config.allowScriptTags") < js.index("newEventSource(")
    src = open(_static("htmx.min.js")).read()
    assert "allowScriptTags:true" in src        # the default we are overriding
    assert "selfRequestsOnly:true" in src       # the default we are pinning


def test_the_vendored_pin_records_a_second_source():
    """A sha256 you can only check against the CDN you fetched from proves
    nothing about that CDN. Two independent CDNs agreeing does."""
    pin = open(_static("htmx.version")).read()
    assert "cdn.jsdelivr.net" in pin
    assert "unpkg.com" in pin
    assert _HTMX_SHA256 in pin


def test_a_client_reset_is_not_an_error_in_the_log(tmp_path):
    """Keep-alive made a reset connection routine, and noisy.

    Under HTTP/1.0 every response closed the socket, so the read for a NEXT
    request never happened. Under 1.1 it always does, and a client that simply
    went away -- a closed tab, a phone leaving the tailnet -- surfaces as
    ConnectionResetError out of the base class, which socketserver prints as a
    full traceback. That is per-tab-close noise in the .err file the agent's
    real failures are supposed to stand out in, and .err staying meaningful is
    a stated design constraint of this module.
    """
    qpath = os.path.join(str(tmp_path), "queue.json")
    save_queue(qpath, [_rec(1)])
    s = _Server(qpath)
    try:
        errors = []
        s.httpd.handle_error = lambda request, addr: errors.append(addr)
        before = set(threading.enumerate())
        sock = socket.create_connection((s.host, s.port), timeout=5)
        sock.sendall(f"GET /mtime HTTP/1.1\r\nHost: {s.host}:{s.port}\r\n\r\n".encode())
        head = _Reader(sock).headers()
        assert head.splitlines()[0].startswith("HTTP/1.1 200")
        spawned = [t for t in threading.enumerate() if t not in before]
        assert spawned
        # RST, not FIN: the abrupt disappearance, which is what a killed tab
        # or a dropped tailnet link actually looks like on the wire
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0))
        sock.close()
        for t in spawned:
            t.join(timeout=5)
            assert not t.is_alive(), t.name
        assert errors == [], "a client going away was logged as a server error"
    finally:
        s.close()


# --- dismissing a feedback row records what was read (2026-08-28) --------

def _fb_rec(number=1, note_refs=(("d1", "101"),)):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=dataclasses.replace(_rec(number).item,
                                 id="feedback:pb-www!4084", kind="feedback",
                                 executor="address-feedback",
                                 why="1 unaddressed thread",
                                 status="proposed", note_refs=note_refs))


def test_dismissing_a_feedback_row_records_its_notes(serve_queue, tmp_path):
    """FALSIFYING. Without the record the next sweep re-derives the same row
    from the same note, and the dismissal was theatre."""
    seen_path = str(tmp_path / "seen-notes.json")
    s, _ = serve_queue([_fb_rec()], seen_path=seen_path)
    assert s.dismiss(1)[0] == 200
    from worksweep.seennotes import load_seen
    assert load_seen(seen_path) == frozenset({("d1", "101")})


def test_dismissing_a_todo_records_nothing(serve_queue, tmp_path):
    """Only feedback rows carry note evidence; a todo dismissal is unchanged."""
    seen_path = str(tmp_path / "seen-notes.json")
    todo = QueueRecord(number=1, first_seen=NOW, last_seen=NOW,
                       item=dataclasses.replace(_rec(1).item, kind="todo",
                                                executor="triage",
                                                status="proposed"))
    s, _ = serve_queue([todo], seen_path=seen_path)
    assert s.dismiss(1)[0] == 200
    from worksweep.seennotes import load_seen
    assert load_seen(seen_path) == frozenset()


def test_a_row_with_no_note_refs_dismisses_without_recording(serve_queue,
                                                             tmp_path):
    """An older feedback row carries no evidence. It still dismisses -- it
    just needs one more sweep before the dismissal can be durable."""
    seen_path = str(tmp_path / "seen-notes.json")
    s, _ = serve_queue([_fb_rec(note_refs=())], seen_path=seen_path)
    assert s.dismiss(1)[0] == 200
    from worksweep.seennotes import load_seen
    assert load_seen(seen_path) == frozenset()


def test_a_dismissed_feedback_row_still_posts_its_audit(serve_queue, tmp_path):
    posted = []
    s, _ = serve_queue([_fb_rec()], post=lambda h, c: posted.append(c),
                       webhook="https://discord.com/api/webhooks/1/x",
                       seen_path=str(tmp_path / "seen-notes.json"))
    body = json.dumps({"number": 1, "actor": "claude"})
    assert s.dismiss(1, body=body)[0] == 200
    assert [p for p in posted if p.startswith("🗑️")][0].endswith(
        " (dashboard · claude)")


def test_a_failed_seen_write_never_fails_the_dismissal(serve_queue, tmp_path):
    """The row is already `done` on disk by then. Losing the durability half
    means the row comes back next sweep -- annoying, not wrong -- while
    failing the request would report the dismissal as not having happened."""
    s, _ = serve_queue([_fb_rec()],
                       seen_path=str(tmp_path / "nope" / "\0bad" / "s.json"))
    assert s.dismiss(1)[0] == 200


def test_a_feedback_row_renders_both_controls(serve_queue):
    """FALSIFYING. The server accepts a dismiss for these rows, but the page
    rendered only the checkbox -- so the control the whole round exists to
    provide was unreachable. `_checkbox` keyed off `has_checkbox` alone;
    `is_dismissable` was imported and never called."""
    s, _ = serve_queue([_fb_rec()])
    html = s.request("GET", "/")[2].decode("utf-8")
    assert 'data-dismiss="1"' in html
    assert 'type="checkbox"' in html


def test_an_ordinary_runnable_row_renders_only_the_checkbox(serve_queue):
    """Dismissing a magi row still silently drops real work, so it still
    offers no way to."""
    s, _ = serve_queue([_rec(1)])
    html = s.request("GET", "/")[2].decode("utf-8")
    assert 'data-dismiss="1"' not in html
    assert 'type="checkbox"' in html


def test_a_manual_row_still_renders_only_dismiss(serve_queue):
    triage = QueueRecord(number=1, first_seen=NOW, last_seen=NOW,
                         item=dataclasses.replace(_rec(1).item,
                                                  executor="triage",
                                                  status="proposed"))
    s, _ = serve_queue([triage])
    html = s.request("GET", "/")[2].decode("utf-8")
    assert 'data-dismiss="1"' in html
    assert 'type="checkbox"' not in html


# =============================================================================
# Send-to-Fable (2026-09-01): the consult strip on parked rows
# =============================================================================
#
# A needs-input row is a question, and before this the question itself never
# reached the page (#238: the human read Discord scrollback to learn what was
# asked). Now the row shows the question, offers 🔮 Consult, renders the rec,
# and Accept flips it to approved with the rec as the executor's ruling.

def _parked_rec(n=7, consult="", consult_rec="", **kw):
    item = dict(schema_version=1, id=f"feedback:pb-www!{4090 + n}",
                repo="pb-www", kind="feedback", executor="address-feedback",
                risk="low", why="2 unaddressed threads",
                web_url=f"https://gl/x/-/merge_requests/{4090 + n}", sha="s",
                status="needs-input", title=f"parked {n}",
                error_summary="2 threads need your call",
                consult=consult, consult_rec=consult_rec)
    item.update(kw)
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=WorkItem(**item))


def _consult_post(s, path, number):
    h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
    return s.request("POST", path, json.dumps({"number": number}), h)


def test_a_parked_row_shows_its_question_and_the_consult_button(serve_queue):
    """FALSIFYING for the question: error_summary never rendered on parked
    rows, so the dashboard offered a hold with no way to read the ask."""
    s, _ = serve_queue([_parked_rec()])
    html = _markup(s.request("GET", "/")[2].decode("utf-8"))
    assert "2 threads need your call" in html
    assert 'data-consult="7"' in html


def test_a_pending_consult_shows_the_hold_not_the_button(serve_queue):
    s, _ = serve_queue([_parked_rec(consult="requested")])
    html = _markup(s.request("GET", "/")[2].decode("utf-8"))
    assert "consult pending" in html
    assert 'data-consult="7"' not in html


def test_a_finished_consult_shows_the_rec_and_accept(serve_queue):
    s, _ = serve_queue([_parked_rec(consult="done",
                                    consult_rec="Do X.  ·  Why: Y.")])
    html = _markup(s.request("GET", "/")[2].decode("utf-8"))
    assert "Do X." in html
    assert 'data-accept-rec="7"' in html
    assert 'data-consult="7"' not in html


def test_a_failed_consult_reoffers_the_button(serve_queue):
    s, _ = serve_queue([_parked_rec(consult="error")])
    html = _markup(s.request("GET", "/")[2].decode("utf-8"))
    assert "consult failed" in html
    assert 'data-consult="7"' in html


def test_post_consult_flips_the_row_to_requested(serve_queue):
    s, qpath = serve_queue([_parked_rec()])
    status, _, body = _consult_post(s, "/consult", 7)
    assert status == 200
    assert json.loads(body)["consult"] == "requested"
    row = load_queue(qpath)[0]
    assert row.item.consult == "requested"
    assert row.item.status == "needs-input"


def test_post_consult_is_idempotent_but_never_replaces_a_rec(serve_queue):
    s, _ = serve_queue([_parked_rec(consult="requested")])
    assert _consult_post(s, "/consult", 7)[0] == 200
    s2, qpath2 = serve_queue([_parked_rec(consult="done", consult_rec="r")])
    status, _, body = _consult_post(s2, "/consult", 7)
    assert status == 400
    assert load_queue(qpath2)[0].item.consult == "done"


def test_post_consult_refuses_a_row_that_is_not_parked(serve_queue):
    s, qpath = serve_queue([_rec(1)])
    assert _consult_post(s, "/consult", 1)[0] == 400
    assert load_queue(qpath)[0].item.consult == ""


def test_accept_rec_approves_with_the_ruling_and_audits_it(serve_queue):
    """The one approval path WITH content, and the audit line says so."""
    posts = []
    s, qpath = serve_queue([_parked_rec(consult="done",
                                        consult_rec="Do X.  ·  Why: Y.")],
                           post=lambda hook, c: posts.append(c),
                           webhook="https://discord/hook")
    status, _, body = _consult_post(s, "/accept-rec", 7)
    assert status == 200
    row = load_queue(qpath)[0]
    assert row.item.status == "approved"
    assert row.item.ruling == "Do X.  ·  Why: Y."
    assert row.item.consult == "" and row.item.consult_rec == ""
    assert len(posts) == 1
    assert "accepted fable rec" in posts[0] and "#7" in posts[0]


def test_accept_rec_without_a_rec_is_a_400(serve_queue):
    posts = []
    s, qpath = serve_queue([_parked_rec()],
                           post=lambda hook, c: posts.append(c),
                           webhook="https://discord/hook")
    assert _consult_post(s, "/accept-rec", 7)[0] == 400
    assert load_queue(qpath)[0].item.status == "needs-input"
    assert posts == []


def test_consult_routes_demand_the_csrf_header(serve_queue):
    """Same skin as every other POST: no header, no flip."""
    s, qpath = serve_queue([_parked_rec()])
    status, _, _ = s.request("POST", "/consult", json.dumps({"number": 7}),
                             {"Content-Type": "application/json"})
    assert status == 403
    assert load_queue(qpath)[0].item.consult == ""
