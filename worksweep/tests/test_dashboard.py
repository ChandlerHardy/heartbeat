import ast, http.client, json, os, re, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest  # noqa: E402
from worksweep import dashboard  # noqa: E402
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import load_queue, save_queue  # noqa: E402

T0 = "2026-06-23T08:00:00Z"
NOW = "2026-06-30T08:00:00Z"


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

class _Server:
    def __init__(self, qpath, post=None, webhook="", now=NOW, sweep=None,
                 mark_todo_done=None):
        self.httpd = dashboard.make_server(("127.0.0.1", 0), qpath, post=post,
                                           webhook=webhook, now=lambda: now,
                                           sweep=sweep,
                                           mark_todo_done=mark_todo_done)
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

    def approve(self, numbers, headers=None):
        h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", "/approve", json.dumps({"numbers": numbers}), h)

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

    def approve_all(self, numbers, headers=None):
        """F2: the page sends the proposed+runnable numbers it rendered."""
        h = {"X-Worksweep": "approve", "Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", "/approve-all",
                            json.dumps({"numbers": numbers}), h)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def serve_queue(tmp_path):
    made = []

    def _make(records, post=None, webhook="", now=NOW, sweep=None,
              mark_todo_done=None):
        qpath = os.path.join(str(tmp_path), "queue.json")
        save_queue(qpath, list(records))
        s = _Server(qpath, post=post, webhook=webhook, now=now, sweep=sweep,
                    mark_todo_done=mark_todo_done)
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
        assert name not in page
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
    assert "send('/approve-all',{numbers:n})" in page.replace(" ", "")


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
    assert "<link" not in page
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
    # selection. A JS timer reloads instead, and skips while either is true.
    assert "http-equiv=\"refresh\"" not in page
    assert "setTimeout(tick," in page.replace(" ", "")
    # AC #35: `branches` persists exactly like the other two -- the restore
    # script must accept all three stored values, not just the original pair
    restore_src = page[restore - 400:page.index("</script>", restore)]
    for view in ("checklist", "panels", "branches"):
        assert f"'{view}'" in restore_src, view


def test_layout_state_never_rides_in_the_url():
    """AC #32."""
    page = _page([_rec(1)])
    assert "pushState" not in page
    assert "location.search" not in page
    assert "location.href" not in page
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
        "datetime", "html", "ipaddress", "json", "os", "re", "subprocess",
        "sys", "threading", "time",
        "urllib.parse",          # pure string parsing, not network
        "dataclasses", "http.server", "typing", "__future__",
        ".approvals", ".formatter", ".models", ".queue",
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
    js = page.replace(" ", "").replace("\n", "")
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


def test_timed_reload_skips_while_a_selection_or_post_is_live():
    """F7 (falsifying): a reload mid-POST tears an approval, and a reload with
    boxes ticked silently discards the selection under the user's thumb."""
    js = _page([_rec(1)]).replace(" ", "").replace("\n", "")
    # One shared guard now covers every auto-reload path: an in-flight POST, an
    # open confirm dialog, or any ticked checkbox.
    assert "functionbusy(){returninflight||confirming||selected().length>0;}" in js
    assert "if(busy()){setTimeout(tick,FALLBACK_MS);return;}" in js
    assert "location.reload();" in js


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
    assert body.decode() == dashboard.mtime_token(os.path.getmtime(qpath))


def test_mtime_changes_when_the_queue_is_rewritten(serve_queue):
    """This is the whole signal the Sync flow waits on."""
    s, qpath = serve_queue([_rec(1)])
    before = s.request("GET", "/mtime")[2]
    os.utime(qpath, (1_800_000_000, 1_800_000_000))
    after = s.request("GET", "/mtime")[2]
    assert after != before
    assert after.decode() == dashboard.mtime_token(1_800_000_000)


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
    js = _page([_rec(1)]).replace(" ", "").replace("\n", "")
    assert "fetch('/sweep',{method:'POST',headers:{'X-Worksweep':'approve'}})" in js
    assert "sync.disabled=true;sync.textContent='syncing…';" in js
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


def _todo(n, todo_id=None, status="proposed"):
    """A todo record. `todo_id` synthesises the `todo:<digits>:` shape that the
    GitLab edge needs; the REAL queue never carries one (see todo_id_of)."""
    ident = (f"todo:{todo_id}:https://gl/x/-/merge_requests/9" if todo_id
             else "todo:assigned:https://gl/x/-/work_items/1719")
    return _rec(n, kind="todo", executor="triage", status=status, id=ident,
                web_url="https://gl/x/-/work_items/1719")


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


def test_a_real_todo_record_dismisses_locally_and_says_why_not_in_gitlab(
        serve_queue, capsys):
    """The live queue's todo ids are `todo:<action>:<url>` -- no numeric id
    anywhere -- so the GitLab edge cannot fire. That must be LOUD, not silent."""
    marker = _Marker()
    s, qpath = serve_queue([_todo(1)], mark_todo_done=marker)
    assert s.dismiss(1)[0] == 200
    assert marker.ids == []                        # nothing to call it with
    assert load_queue(qpath)[0].item.status == "done"
    err = capsys.readouterr().err
    assert "was NOT marked done" in err
    assert "no todo id" in err


def test_todo_id_of_reads_a_numeric_id_and_zero_otherwise():
    assert dashboard.todo_id_of(_todo(1, todo_id=4242).item) == 4242
    # the shape the live queue actually carries
    assert dashboard.todo_id_of(_todo(1).item) == 0
    assert dashboard.todo_id_of(_rec(1, id="issue:pb-www#869").item) == 0
    assert dashboard.todo_id_of(_rec(1, id="").item) == 0


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
    js = _page([_rec(1)]).replace(" ", "").replace("\n", "")
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
    js = _page([_rec(1, executor="triage")]).replace(" ", "").replace("\n", "")
    assert "vard=e.target.closest('[data-dismiss]');" in js
    assert "send('/dismiss',{number:parseInt(d.getAttribute('data-dismiss'),10)})" in js


# --- always-on live polling --------------------------------------------------

def test_live_poll_is_always_armed_not_gated_on_a_sync_tap():
    """Addendum 3 (falsifying): if the poll only started after a Sync tap, a
    runner completion would not appear until the next timed reload."""
    js = _page([_rec(1)]).replace(" ", "").replace("\n", "")
    assert "POLL_MS=10000" in js
    assert "FALLBACK_MS=300000" in js
    # Armed at TOP LEVEL, not inside a handler. Matched at exactly two spaces
    # of indent so the rescheduling calls inside poll() (deeper indent) cannot
    # satisfy this on their own.
    body = _page([_rec(1)])
    script = body[body.rindex("<script>"):]
    assert re.search(r"^  setTimeout\(poll,POLL_MS\);$", script, re.M)
    assert re.search(r"^  setTimeout\(tick,FALLBACK_MS\);$", script, re.M)
    assert "fetch('/mtime',{cache:'no-store'})" in js


def test_live_poll_reloads_only_when_the_mtime_changed_and_nothing_is_busy():
    js = _page([_rec(1)]).replace(" ", "").replace("\n", "")
    assert "if(t&&baseMtime&&t!==baseMtime&&!busy()){location.reload();return;}" in js
    # and it keeps polling when busy, so it resumes rather than giving up
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
