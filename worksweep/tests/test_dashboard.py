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
    def __init__(self, qpath, post=None, webhook="", now=NOW):
        self.httpd = dashboard.make_server(("127.0.0.1", 0), qpath, post=post,
                                           webhook=webhook, now=lambda: now)
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

    def approve_all(self, headers=None):
        h = {"X-Worksweep": "approve"}
        h.update(headers or {})
        return self.request("POST", "/approve-all", "", h)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def serve_queue(tmp_path):
    made = []

    def _make(records, post=None, webhook="", now=NOW):
        qpath = os.path.join(str(tmp_path), "queue.json")
        save_queue(qpath, list(records))
        s = _Server(qpath, post=post, webhook=webhook, now=now)
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
def test_resolve_bind_falls_back_to_loopback(fake):
    """AC #15: missing binary, non-zero exit, or no output -> 127.0.0.1."""
    assert dashboard.resolve_bind("auto", run_subprocess=fake) == "127.0.0.1"


def test_resolve_bind_passes_an_explicit_address_through():
    def boom(cmd, **kw):
        raise AssertionError("must not shell out for an explicit bind")
    assert dashboard.resolve_bind("10.0.0.9", run_subprocess=boom) == "10.0.0.9"


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
                    post=post, webhook=webhook)
        return 0
    monkeypatch.setattr(dashboard, "serve", fake_serve)

    assert wsmain.main(["dashboard"]) == 0
    assert seen["port"] == 8787
    assert seen["bind"] == "auto"
    assert seen["queue_path"] == qpath
    # the Discord poster arrives by INJECTION -- dashboard.py imports no __main__
    assert seen["post"] is wsmain._post_discord
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
    status, _, body = s.approve_all()
    assert status == 200
    assert json.loads(body)["approved"] == [1, 3]
    out = {r.number: r.item.status for r in load_queue(qpath)}
    assert out == {1: "approved", 2: "needs-input", 3: "approved",
                   4: "running", 5: "approved", 6: "done", 7: "error"}


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


def test_approve_all_needs_no_body(serve_queue):
    """The blanket route takes an empty or absent body (Component Spec)."""
    s, qpath = serve_queue([_rec(1)])
    status, _, _ = s.request("POST", "/approve-all", None, {"X-Worksweep": "approve"})
    assert status == 200
    assert load_queue(qpath)[0].item.status == "approved"


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
    assert s.approve_all()[0] == 200
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
    assert s.approve_all()[0] == 200
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


def test_approve_all_button_confirms_with_the_proposed_count():
    """No un-approve path exists, so the bulk action must not be a stray tap."""
    page = _page([_rec(1), _rec(2), _rec(3, status="needs-input"),
                  _rec(4, status="running")])
    btn = re.search(r'<button[^>]*id="approve-all"[^>]*>', page).group(0)
    assert 'data-proposed-count="2"' in btn      # proposed only, not needs-input
    assert re.search(r"confirm\(\s*['\"]Approve all", page)
    assert "proposed items?" in page


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
    assert re.search(r'<meta http-equiv="refresh" content="60"', page)
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


def test_card_title_falls_back_to_the_mr_ref_when_there_is_no_branch():
    """AC #38."""
    recs = [_br(1, web_url="https://gl/g/pb-www/-/merge_requests/4821")]
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
        "datetime", "html", "json", "os", "re", "subprocess", "sys",
        "urllib.parse",          # pure string parsing, not network
        "dataclasses", "http.server", "typing", "__future__",
        ".approvals", ".models", ".queue",
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
