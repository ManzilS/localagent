import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from localagent.connections import ConnectionManager, passwords_conn
from localagent.connections.passwords_conn import same_site, host_of, mask
from localagent.connections.secrets import Secrets

SECRET = "Tr0ub4dor&3-very-secret"

FAKE_OP = r'''
import json, sys, os
a = sys.argv[1:]
port = os.environ["FAKE_PORT"]
items = {
  "it1": {"id": "it1", "title": "Local test site", "additional_information": "nick@example.com",
          "urls": [{"href": f"http://127.0.0.1:{port}/login", "primary": True}],
          "fields": [{"id": "username", "purpose": "USERNAME", "value": "nick@example.com"},
                     {"id": "password", "purpose": "PASSWORD", "type": "CONCEALED", "value": "SECRET"},
                     {"id": "otp", "type": "OTP", "value": "otpauth://x", "totp": "123456"}]},
  "it2": {"id": "it2", "title": "GitHub", "additional_information": "nick", "urls": [{"href": "https://github.com"}],
          "fields": [{"id": "password", "purpose": "PASSWORD", "value": "other"}]},
}
if a[:2] == ["vault", "list"]: print(json.dumps([{"id": "v1", "name": "Personal"}]))
elif a[:2] == ["item", "list"]: print(json.dumps([{k: v for k, v in i.items() if k != "fields"} for i in items.values()]))
elif a[:2] == ["item", "get"]:
    assert "--reveal" in a
    print(json.dumps(items[a[2]]).replace("SECRET", os.environ["FAKE_SECRET"]))
else: sys.exit(2)
'''

PAGES = {
    "/login": """<title>Sign in</title><form><input type=email name=email placeholder="Email">
                 <input type=password name=pw placeholder="Password"><button type=button>Sign in</button></form>""",
    "/step1": """<title>Sign in</title><input type=email name=email aria-label="Email"><button>Next</button>""",
    "/otp": """<title>Verify</title><input autocomplete="one-time-code" name=code aria-label="Code"><button>Verify</button>""",
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
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGES.get(self.path.split("?")[0], "").encode())


def test_site_matching_is_strict():
    assert same_site("accounts.google.com", "google.com") and same_site("google.com", "mail.google.com")
    assert not same_site("google.com.evil.io", "google.com") and not same_site("evilgithub.com", "github.com")
    assert host_of("https://www.github.com/login") == "github.com"
    assert mask("nick@example.com") == "ni•••@example.com" and SECRET[:3] not in mask("x")


class Cfg:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = tmp / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.data = {"browser": {"headless": True, **({} if os.name == "nt" else {"channel": "bundled"})}, "desktop": {"confirm_patterns": []}}
        self.agent = {"max_tool_output_chars": 20000}


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv = _server(H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    script = tmp_path / "fake_op.py"
    script.write_text(FAKE_OP, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "op.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "op"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setattr(passwords_conn, "find_cli", lambda n: str(exe))
    monkeypatch.setenv("FAKE_PORT", str(srv.server_port))
    monkeypatch.setenv("FAKE_SECRET", SECRET)
    cfg = Cfg(tmp_path)
    m = ConnectionManager(cfg, secrets=Secrets(tmp_path, use_keychain=False))
    r = m.add("passwords", {"provider": "1password"})
    assert "1 1Password vault" in r["message"]

    class RT:
        connections = m
        answers = []
        asked = []
        def __init__(self):
            self.cfg = cfg
        def request_approval(self, q):
            self.asked.append(q)
            return self.answers.pop(0) if self.answers else False
    rt = RT()
    from localagent.tools import browser
    try:
        browser.run(rt, "goto", "about:blank")
    except Exception as e:
        pytest.skip(f"no browser here: {e}")
    yield browser, rt, f"http://127.0.0.1:{srv.server_port}"
    browser.run(rt, "close")
    srv.shutdown()


def field(browser, name):
    return browser._executor.submit(lambda: browser._state["page"].locator(f"[name={name}]").input_value()).result()


def test_login_fills_after_approval_and_never_reveals(env):
    browser, rt, site = env
    out = browser.run(rt, "goto", site + "/login")
    assert "action 'login'" in out                                   # agent is pointed at the password manager
    with pytest.raises(PermissionError):
        browser.run(rt, "login")                                     # user said no
    assert "127.0.0.1" in rt.asked[-1] and "ni•••@example.com" in rt.asked[-1] and SECRET not in rt.asked[-1]
    assert field(browser, "pw") == ""
    rt.answers = [True]
    out = browser.run(rt, "login")
    assert "Filled the username and password" in out and SECRET not in out
    assert field(browser, "pw") == SECRET and field(browser, "email") == "nick@example.com"
    for action in ("snapshot", "read", "links"):
        assert SECRET not in browser.run(rt, action)                 # nothing the model sees contains it


def test_login_not_offered_to_other_sites(env, monkeypatch):
    browser, rt, site = env
    browser.run(rt, "goto", site.replace("127.0.0.1", "localhost") + "/login")   # same server, different site name
    out = browser.run(rt, "login")
    assert "No saved login for localhost" in out and not rt.asked


def test_two_step_and_one_time_code(env):
    browser, rt, site = env
    browser.run(rt, "goto", site + "/step1")
    rt.answers = [True]
    out = browser.run(rt, "login")
    assert "Only the username box" in out and field(browser, "email") == "nick@example.com"
    browser.run(rt, "goto", site + "/otp")
    rt.answers = [True]
    out = browser.run(rt, "login", text="code")
    assert "one-time code" in out and "123456" not in out and field(browser, "code") == "123456"


FAKE_BW = r'''
import json, sys, os
a = sys.argv[1:]
state = os.path.join(os.environ["FAKE_DIR"], "bw_logged_in")
if a[0] == "status": print(json.dumps({"status": "locked" if os.path.exists(state) else "unauthenticated"}))
elif a[:2] == ["login", "--apikey"]:
    assert os.environ.get("BW_CLIENTID") == "user.abc" and os.environ.get("BW_CLIENTSECRET") == "sek"; open(state, "w").write("1")
elif a[0] == "unlock":
    if os.environ.get("LA_BW_PW") != "correct horse": sys.stderr.write("Invalid master password."); sys.exit(1)
    print("SESSIONKEY")
elif a[:2] == ["list", "items"]:
    assert os.environ.get("BW_SESSION") == "SESSIONKEY"
    print(json.dumps([{"id": "b1", "name": "Netflix", "login": {"username": "nick@example.com", "password": "pw", "totp": "x",
                       "uris": [{"uri": "https://www.netflix.com/login"}]}},
                      {"id": "b2", "name": "Fake", "login": {"username": "n", "uris": [{"uri": "https://netflix.com.evil.io"}]}}]))
else: sys.exit(2)
'''


def test_bitwarden_unlock_is_memory_only(tmp_path, monkeypatch):
    script = tmp_path / "fake_bw.py"
    script.write_text(FAKE_BW, encoding="utf-8")
    exe = tmp_path / ("bw.cmd" if os.name == "nt" else "bw")
    exe.write_text(f'@"{sys.executable}" "{script}" %*\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                   encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(passwords_conn, "find_cli", lambda n: str(exe))
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    m = ConnectionManager(Cfg(tmp_path), secrets=Secrets(tmp_path, use_keychain=False))
    r = m.add("passwords", {"provider": "bitwarden"}, {"client_id": "user.abc", "client_secret": "sek"})
    c = m.get(r["id"])
    assert "Unlock" in r["message"] and not c.unlocked
    with pytest.raises(PermissionError, match="locked"):
        c.logins_for("https://netflix.com")
    with pytest.raises(RuntimeError, match="Invalid master password"):
        c.bw_unlock("wrong")
    assert "Unlocked" in c.bw_unlock("correct horse") and c.unlocked
    stored = json.dumps(m.secrets._load()) + (tmp_path / "home").joinpath("x").as_posix()
    assert "correct horse" not in stored and "SESSIONKEY" not in json.dumps(m.secrets._load())   # never saved
    assert [i["title"] for i in c.logins_for("https://www.netflix.com/browse")] == ["Netflix"]   # the look-alike is not offered
    assert m.status()[0]["locked"] is False
    c.lock()
    assert m.status()[0]["locked"] is True
