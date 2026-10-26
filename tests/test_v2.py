"""Subscription bridges, brain failover, desktop approvals, GUI stop/approve."""
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from localagent.agent import Agent
from localagent.bridges import ChatGPTBridge, GeminiBridge, flatten
from localagent.llm import LLMResponse, Router
from localagent.runtime import Runtime
from localagent.tools import desktop
from test_localagent import FakeProvider, FakeRouter, cfg, runtime, tc  # noqa: F401

FAKE_CODEX = r'''#!PY
import sys, json
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
open(__import__("os").environ["FAKE_LOG"], "a").write(json.dumps({"exe": "codex", "args": args}) + "\n")
if __import__("os").environ.get("CODEX_FAIL"):
    print("error: You've hit your usage limit", file=sys.stderr); sys.exit(1)
if __import__("os").environ.get("CODEX_REFUSE_MODEL") and "--model" in args:
    print('ERROR: {"status":400,"error":{"message":"The model is not supported when using Codex with a ChatGPT account.","code":"model_not_found"}}', file=sys.stderr); sys.exit(1)
out = args[args.index("--output-last-message") + 1]
assert "list_dir(" in prompt, "tool list must be in the prompt"
reply = ('<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>' if "[TOOL RESULT: list_dir]" not in prompt
         else "Finished with ChatGPT.")
open(out, "w").write(reply)
'''

FAKE_GEMINI = r'''#!PY
import sys, json, os
prompt = sys.stdin.read()
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"exe": "gemini", "args": sys.argv[1:]}) + "\n")
if os.environ.get("GEMINI_RETIRED"):
    print("An unexpected critical error occurred:IneligibleTierError: This client is no longer supported for "
          "Gemini Code Assist for individuals.", file=sys.stderr); sys.exit(1)
print("Loaded cached credentials.")
print(json.dumps({"response": "Gemini handled it.", "stats": {}}))
'''


FAKE_AGY = r'''#!PY
import sys, json, os
args = sys.argv[1:]
stdin = sys.stdin.read()
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"exe": "agy", "args": args, "stdin": stdin}) + "\n")
if os.environ.get("AGY_FAIL"):
    print(json.dumps({"status": "ERROR", "error": "authentication required"})); sys.exit(1)
if "stream-json" in args:
    prompt = json.loads(stdin.splitlines()[0])["message"]["content"]
    # event shapes captured from agy 1.4.7
    print(json.dumps({"event": "init", "conversation_id": "c1", "init": {"tools": ["write_to_file"], "permission_mode": "request-review"}}))
    print(json.dumps({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}}))
    print(json.dumps({"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "Antigravity "}}))
    print(json.dumps({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "streamed it.\n"}}))
    if not os.environ.get("AGY_NO_RESULT"):
        print(json.dumps({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "Antigravity streamed it.\n"}}))
else:
    prompt = args[args.index("-p") + 1]
    print(json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "Antigravity handled it."}))
assert "YOUR TURN" in prompt
'''


def install_fake(b: Path, name: str, src: str) -> None:
    """A fake CLI on PATH. Windows only runs extensionless scripts through a .cmd shim."""
    if os.name == "nt":
        (b / f"{name}.py").write_text(src.replace("#!PY", ""))
        (b / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n')
        return
    f = b / name
    f.write_text(src.replace("#!PY", "#!" + sys.executable))
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    for name, src in (("codex", FAKE_CODEX), ("gemini", FAKE_GEMINI)):
        install_fake(b, name, src)
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "home_dir"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home_dir"))
    monkeypatch.setenv("LOCALAGENT_HOME", str(tmp_path / "la_home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(GeminiBridge, "find_agy", staticmethod(lambda: None))  # tests opt in to agy below
    (tmp_path / "home_dir" / ".gemini").mkdir(parents=True)
    (tmp_path / "home_dir" / ".gemini" / "oauth_creds.json").write_text("{}")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def test_flatten_renders_transcript():
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "list_dir", "arguments": {"path": "."}}]},
            {"role": "tool", "tool_call_id": "1", "name": "list_dir", "content": "a.txt"}]
    out = flatten(msgs)
    assert "SYS" in out and "[USER]\ndo it" in out and '"name": "list_dir"' in out and "[TOOL RESULT: list_dir]\na.txt" in out
    assert "do NOT run commands" in out


def test_bridge_status(fakebin):
    st = ChatGPTBridge().status()
    assert st["installed"] and st["signed_in"]
    assert GeminiBridge().status()["signed_in"]


def test_chatgpt_bridge_drives_agent_tools(cfg, fakebin):
    """Local model is stuck -> ChatGPT subscription takes over and its text
    tool calls are executed by LocalAgent's own tools."""
    stuck = FakeProvider("small", lambda m, t: tc("no_such_tool"))
    rt = runtime(cfg, stuck, ChatGPTBridge())
    events = []
    out = Agent(rt, mode="auto", on_event=lambda k, d: events.append((k, d))).run("organize my files")
    assert out == "Finished with ChatGPT."
    assert ("escalate" in [k for k, _ in events])
    assert any(k == "tool_call" and d["name"] == "list_dir" for k, d in events)
    calls = [json.loads(l) for l in fakebin.read_text().splitlines()]
    args = calls[0]["args"]
    assert args[0] == "exec" and "--sandbox" in args and args[args.index("--sandbox") + 1] == "read-only"


def test_brain_failover_chatgpt_limit_to_gemini(cfg, fakebin, monkeypatch):
    monkeypatch.setenv("CODEX_FAIL", "1")
    router = Router({"main": {"provider": "ollama", "model": "x"}}, {"ollama": {"base_url": "http://127.0.0.1:9"}},
                    [{"provider": "chatgpt"}, {"provider": "gemini"}])
    assert [p.key for p in router.brain_chain()] == ["chatgpt", "gemini"]
    rt = Runtime(cfg, router=router)
    events = []
    out = Agent(rt, mode="deep", on_event=lambda k, d: events.append((k, d))).run("hard question")
    assert out == "Gemini handled it."
    assert any(k == "escalate" and "Gemini" in d["to"] for k, d in events)


def test_disabled_brain_is_skipped(fakebin):
    router = Router({}, {}, [{"provider": "chatgpt", "enabled": False}, {"provider": "gemini"}])
    assert [p.key for p in router.brain_chain()] == ["gemini"]


class FakeGui:
    def __init__(self):
        self.clicks, self.keys = [], []
    def size(self):
        return (1920, 1080)
    def moveTo(self, *a, **k):
        pass
    def click(self, x, y, clicks=1, button="left"):
        self.clicks.append((x, y, clicks, button))
    def hotkey(self, *k):
        self.keys.append("+".join(k))
    def press(self, k):
        self.keys.append(k)
    def write(self, t, interval=0):
        self.keys.append("type:" + t)


@pytest.fixture
def gui(monkeypatch):
    g = FakeGui()
    monkeypatch.setattr(desktop, "_gui", lambda: g)
    return g


def _with_elements(rt):
    desktop._state(rt)["elements"] = {
        "e1": {"type": "Button", "name": "Save", "x": 100, "y": 100, "w": 40, "h": 20},
        "e2": {"type": "Button", "name": "Delete conversation", "x": 300, "y": 200, "w": 80, "h": 30},
    }


def test_desktop_click_and_guard(cfg, gui):
    rt = runtime(cfg, FakeProvider("m", lambda m, t: None))
    _with_elements(rt)
    reg = Agent(rt).registry
    out, ok = reg.call("click", {"target": "e1"})
    assert ok and gui.clicks == [(120, 110, 1, "left")]
    out, ok = reg.call("click", {"target": "Save"})  # by name
    assert ok and len(gui.clicks) == 2
    asked = []
    rt.approver = lambda q: asked.append(q) or False
    out, ok = reg.call("click", {"target": "e2"})
    assert not ok and "did not approve" in out and len(gui.clicks) == 2 and "Delete conversation" in asked[0]
    rt.approver = lambda q: True
    out, ok = reg.call("click", {"target": "e2"})
    assert ok and gui.clicks[-1][:2] == (340, 215)
    rt.approver = None  # nobody around -> denied
    out, ok = reg.call("press_keys", {"keys": "shift+delete"})
    assert not ok and "shift+delete" not in gui.keys
    out, ok = reg.call("press_keys", {"keys": "ctrl+s"})
    assert ok and gui.keys[-1] == "ctrl+s"


def test_grid_math():
    assert desktop.grid_cell_center(1, 8, 6, 800, 600) == (50, 50)
    assert desktop.grid_cell_center(48, 8, 6, 800, 600) == (750, 550)
    assert desktop.grid_cell_center(9, 8, 6, 800, 600) == (50, 150)


def test_desktop_disabled_removes_tools(cfg):
    cfg.data["desktop"] = {"enabled": False}
    names = Agent(runtime(cfg, FakeProvider("m", lambda m, t: None))).registry.names
    assert "click" not in names and "read_file" in names


class LiveServer:
    """Real uvicorn server on a free port, so SSE streams for real."""
    def __init__(self, cfg, monkeypatch, provider, session_init=None):
        import socket, uvicorn, httpx
        import localagent.webui as webui
        monkeypatch.setattr(webui, "load_config", lambda p=None: cfg)
        monkeypatch.setattr(webui, "Router", lambda *a, **k: FakeRouter(provider))
        if session_init:
            real = webui.Session.__init__
            def init(self, state, mode):
                real(self, state, mode)
                session_init(self)
            monkeypatch.setattr(webui.Session, "__init__", init)
        self.app = webui.create_app(token="tok")
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0)); self.port = so.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30, headers={"X-LocalAgent-Token": "tok"})

    def events(self, body):
        import httpx
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30, headers={"X-LocalAgent-Token": "tok"}) as c, c.stream("POST", "/api/chat", json=body) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def close(self):
        self.server.should_exit = True


def test_gui_approval_roundtrip(cfg, gui, monkeypatch):
    def brain(msgs, tools):
        if msgs[-1]["role"] == "user":
            return tc("click", target="Empty trash")
        return LLMResponse(content="done")
    seed = lambda s: desktop._state(s.rt).__setitem__("elements", {"e1": {"type": "Button", "name": "Empty trash", "x": 10, "y": 10, "w": 10, "h": 10}})
    srv = LiveServer(cfg, monkeypatch, FakeProvider("m", brain), seed)
    try:
        events = []
        for ev in srv.events({"session": "a", "message": "clean up", "mode": "quick"}):
            events.append(ev)
            if ev["type"] == "approval":
                assert "Empty trash" in ev["text"] and not gui.clicks  # nothing clicked yet
                assert srv.http.post("/api/approve", json={"session": "a", "id": ev["id"], "allow": True}).status_code == 200
        assert gui.clicks == [(15, 15, 1, "left")]
        assert any(e["type"] == "approval_done" and e["allow"] for e in events)
        assert srv.http.post("/api/approve", json={"session": "a", "id": "gone", "allow": True}).status_code == 404
    finally:
        srv.close()


def test_gui_denied_approval(cfg, gui, monkeypatch):
    def brain(msgs, tools):
        if msgs[-1]["role"] == "user":
            return tc("click", target="Empty trash")
        return LLMResponse(content="ok: " + msgs[-1]["content"])
    seed = lambda s: desktop._state(s.rt).__setitem__("elements", {"e1": {"type": "Button", "name": "Empty trash", "x": 10, "y": 10, "w": 10, "h": 10}})
    srv = LiveServer(cfg, monkeypatch, FakeProvider("m", brain), seed)
    try:
        for ev in srv.events({"session": "d", "message": "clean up", "mode": "quick"}):
            if ev["type"] == "approval":
                srv.http.post("/api/approve", json={"session": "d", "id": ev["id"], "allow": False})
            if ev["type"] == "final":
                assert "did not approve" in ev["text"]
        assert gui.clicks == []
    finally:
        srv.close()


def test_gui_stop(cfg, monkeypatch):
    cfg.agent["max_steps"] = 500
    slow = FakeProvider("m", lambda m, t: (time.sleep(0.05), tc("list_dir"))[1])
    srv = LiveServer(cfg, monkeypatch, slow)
    try:
        n, final = 0, None
        for ev in srv.events({"session": "b", "message": "loop", "mode": "quick"}):
            if ev["type"] == "tool_result":
                n += 1
                if n == 2:
                    srv.http.post("/api/stop", json={"session": "b"})
            if ev["type"] == "final":
                final = ev["text"]
        assert final.startswith("Stopped. Tell me") and n < 10
    finally:
        srv.close()


def test_settings_roundtrip(cfg, monkeypatch, tmp_path):
    import localagent.setup_service as ss
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setattr(ss, "CONFIG_PATH", cfgfile)
    monkeypatch.setattr("localagent.config.CONFIG_PATH", cfgfile)
    ss.save_settings({"desktop_enabled": False, "confirm": ["delete"], "brains": [{"provider": "gemini", "enabled": True}],
                      "api": {"provider": "anthropic", "key": "sk-test", "model": ""}, "setup_done": True})
    data = ss.read_user_config()
    assert data["desktop"]["enabled"] is False and len(data["desktop"]["confirm_patterns"]) == 1
    assert data["brains"] == [{"provider": "gemini", "enabled": True}]
    assert data["models"]["cloud"]["api_key"] == "sk-test" and data["setup_done"]
    # Windows chmod does not implement POSIX owner/group permission bits.
    if os.name != "nt":
        assert stat.S_IMODE(cfgfile.stat().st_mode) == 0o600
    ss.save_settings({"api": {"provider": "anthropic", "key": "", "model": "claude-opus-5-5"}})
    assert ss.read_user_config()["models"]["cloud"]["api_key"] == "sk-test"  # key kept when not re-entered


def test_server_rejects_cross_site_and_unauthenticated(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    import localagent.webui as webui
    monkeypatch.setattr(webui, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(webui, "Router", lambda *a, **k: FakeRouter(FakeProvider("m", lambda m, t: None)))
    app = webui.create_app(token="secret-token")
    anon = TestClient(app)
    assert anon.post("/api/chat", json={"session": "x", "message": "rm -rf ~"}).status_code == 401
    assert anon.get("/").status_code == 403                       # no token: locked page
    assert anon.get("/?t=wrong").status_code == 403
    # an evil web page posting from the user's browser (no cookie, foreign Origin)
    r = anon.post("/api/stop_all", headers={"Origin": "https://evil.example", "X-LocalAgent-Token": "secret-token"})
    assert r.status_code == 403
    # DNS rebinding: evil.example resolved to 127.0.0.1, Host header gives it away
    assert anon.get("/api/status", headers={"Host": "evil.example", "X-LocalAgent-Token": "secret-token"}).status_code == 403
    # the real app: open with ?t=, then the SameSite cookie authorizes API calls
    ok = TestClient(app)
    assert ok.get("/?t=secret-token").status_code == 200
    assert ok.get("/api/trash").status_code == 200


# ------------------------------------------------------- brain recovery

def test_chatgpt_drops_refused_model_and_retries(fakebin, monkeypatch, tmp_path):
    """The Codex config names a model this account/CLI can't use -> retry with Codex's own default."""
    monkeypatch.setenv("CODEX_REFUSE_MODEL", "1")
    b = ChatGPTBridge(model="gpt-unavailable")
    out = b.chat([{"role": "user", "content": "list_dir( please"}])
    assert out.content.startswith("<tool_call>")
    calls = [json.loads(l) for l in fakebin.read_text().splitlines()]
    assert "--model" in calls[0]["args"] and "--model" not in calls[1]["args"]
    assert len(calls) == 2


def test_gemini_cli_retired_is_explained_and_remembered(fakebin, monkeypatch):
    monkeypatch.setenv("GEMINI_RETIRED", "1")
    b = GeminiBridge()
    assert b.available(refresh=True)
    with pytest.raises(Exception) as e:
        b.chat([{"role": "user", "content": "hi"}])
    assert "retired" in str(e.value) and "Antigravity" in str(e.value) and "not signed in" not in str(e.value)
    assert GeminiBridge.retired_marker().exists()
    st = GeminiBridge().status()
    assert not st["installed"] and "antigravity.google/cli/install" in st["install_command"]
    assert not GeminiBridge().available(refresh=True)
    # an API key still works with the old CLI
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert GeminiBridge().engine()[0] == "gemini"


def _with_agy(tmp_path, monkeypatch):
    b = tmp_path / "agybin"
    b.mkdir()
    install_fake(b, "agy", FAKE_AGY)
    exe = str(b / ("agy.cmd" if os.name == "nt" else "agy"))
    monkeypatch.setattr(GeminiBridge, "find_agy", staticmethod(lambda: exe))
    (Path(os.environ["USERPROFILE"]) / ".gemini" / "antigravity-cli").mkdir(parents=True)
    return exe


def test_gemini_prefers_antigravity(fakebin, tmp_path, monkeypatch):
    _with_agy(tmp_path, monkeypatch)
    b = GeminiBridge()
    assert b.engine()[0] == "agy" and b.status()["signed_in"]
    out = b.chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "x" * 30_000}])
    assert out.content == "Antigravity streamed it."
    call = [json.loads(l) for l in fakebin.read_text().splitlines()][-1]
    assert call["exe"] == "agy" and "--sandbox" in call["args"] and "--dangerously-skip-permissions" not in call["args"]
    assert json.loads(call["stdin"])["event"] == "user"


def test_agy_short_prompt_goes_in_argv(fakebin, tmp_path, monkeypatch):
    monkeypatch.setattr(GeminiBridge, "find_agy", staticmethod(lambda: "/x/agy"))
    b = GeminiBridge()
    a = b.argv("o", [], "short prompt")
    assert a[-2:] == ["-p", "short prompt"] and "json" in a and b.stdin_for("short prompt") == ""
    assert b.parse('{"status": "SUCCESS", "response": "ok"}', "o") == "ok"
    assert b.parse('{"status": "ERROR", "error": "authentication required"}', "o") == ""
    assert b.last_error == "authentication required"


def test_agy_failure_reports_sign_in(fakebin, tmp_path, monkeypatch):
    _with_agy(tmp_path, monkeypatch)
    monkeypatch.setenv("AGY_FAIL", "1")
    with pytest.raises(Exception) as e:
        GeminiBridge().chat([{"role": "user", "content": "hi"}])
    assert "not signed in" in str(e.value)
