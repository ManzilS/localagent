"""Real browser tests (skipped when Playwright's Chromium isn't available)."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pw = pytest.importorskip("playwright.sync_api")

PAGES = {
    "/login": "<title>Sign in</title><form><input name=u placeholder='Email'><button>Next</button></form>",
    "/inbox": """<title>Inbox</title><h1>Inbox</h1><p>Rent reminder from landlord</p>
      <button onclick="document.title='deleted'">Delete</button>
      <textarea aria-label="Message"></textarea><button id=s onclick="document.title='sent'">Send</button>
      <a href="/setcookie">Remember me</a>""",
    "/setcookie": "<title>ok</title>",
}


UNSAFE_PORTS = {1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080}


def _server(handler):
    """A local test server on a port browsers are willing to open."""
    while True:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if srv.server_port not in UNSAFE_PORTS:
            return srv
        srv.server_close()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if path == "/setcookie":
            self.send_header("Set-Cookie", "session=abc; Max-Age=86400; Path=/")
        self.end_headers()
        body = PAGES.get(path, "")
        if path == "/whoami":
            body = f"<title>who</title><p>cookie:{self.headers.get('Cookie', '')}</p>"
        self.wfile.write(body.encode())


class Cfg:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = tmp / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.data = {"browser": {"headless": True, **({} if os.name == "nt" else {"channel": "bundled"})},
                     "desktop": {"confirm_patterns": [r"\b(delete|remove)\b"]}}
        self.agent = {"max_tool_output_chars": 20000}


class RT:
    def __init__(self, cfg, answer=False):
        self.cfg, self.answer, self.asked = cfg, answer, []

    def request_approval(self, q):
        self.asked.append(q)
        return self.answer


@pytest.fixture
def site():
    srv = _server(H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def br(tmp_path):
    from localagent.tools import browser
    try:
        browser.run(RT(Cfg(tmp_path)), "close")
        rt = RT(Cfg(tmp_path))
        browser.run(rt, "goto", "about:blank")
    except Exception as e:
        pytest.skip(f"no browser here: {e}")
    yield browser, rt
    browser.run(rt, "close")


def test_sign_in_wall_is_handed_to_the_user(br, site):
    browser, rt = br
    out = browser.run(rt, "goto", site + "/login")
    assert "wants the user to sign in" in out and "show" in out


def test_snapshot_refs_and_risky_clicks_ask(br, site):
    browser, rt = br
    browser.run(rt, "goto", site + "/inbox")
    snap = browser.run(rt, "snapshot")
    ref = next(l.split()[0] for l in snap.splitlines() if '"Delete"' in l)
    with pytest.raises(PermissionError):
        browser.run(rt, "click", target=ref)                     # user said no
    assert "Delete" in rt.asked[-1] and "127.0.0.1" in rt.asked[-1]
    assert "deleted" not in browser.run(rt, "read")
    browser.run(rt, "fill", target="Message", text="hi")
    with pytest.raises(PermissionError):
        browser.run(rt, "click", target="#s")                    # Send also asks, even with no setting
    rt.answer = True
    browser.run(rt, "click", target="#s")
    assert "Title: sent" in browser.run(rt, "read")


def test_sign_ins_survive_restart(tmp_path, site):
    from localagent.tools import browser
    rt = RT(Cfg(tmp_path))
    try:
        browser.run(rt, "goto", site + "/setcookie")
    except Exception as e:
        pytest.skip(f"no browser here: {e}")
    browser.run(rt, "close")
    browser.run(rt, "goto", site + "/whoami")
    assert "cookie:session=abc" in browser.run(rt, "read")
    browser.run(rt, "close")


def test_open_website_tool(tmp_path, site):
    from localagent.connections import ConnectionManager, registry_tools
    from localagent.connections.secrets import Secrets
    from localagent.tools import browser
    cfg = Cfg(tmp_path)
    m = ConnectionManager(cfg, secrets=Secrets(tmp_path, use_keychain=False))
    m.add("web", {"url": site + "/inbox", "name": "My mail", "tips": "Messages are listed on the page."}, test=False)

    class R(RT):
        connections = m
    rt = R(cfg)
    tools = {t.name: t for t in registry_tools(rt)}
    try:
        out = tools["open_website"].func(rt, site="mail")
    except Exception as e:
        pytest.skip(f"no browser here: {e}")
    assert "Rent reminder" in out and "Messages are listed" in out
    from localagent import guard
    assert guard.is_untrusted(rt, "open_website", {}) == "a website"
    browser.run(rt, "close")
