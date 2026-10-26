"""Speed features: Ollama streaming + keep-alive, GPU-fit context, lean Codex flags, warm-up."""
import json

import httpx

from localagent.llm import OllamaProvider
from localagent.bridges import ChatGPTBridge


def provider_with(handler):
    p = OllamaProvider("qwen3:8b", "http://ollama.test")
    p.client = httpx.Client(transport=httpx.MockTransport(handler))
    p._fit_cache = {}  # per-test
    return p


def test_streaming_tokens_and_tool_calls():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen.update(body)
        lines = [{"message": {"content": "Hel"}}, {"message": {"content": "lo"}},
                 {"message": {"content": "", "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]}},
                 {"done": True, "eval_count": 3, "prompt_eval_count": 9, "message": {"content": ""}}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    toks = []
    r = provider_with(handler).chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "list_dir"}}],
                                    on_token=toks.append, num_ctx=8192)
    assert toks == ["Hel", "lo"] and r.content == "Hello" and r.tool_calls[0].name == "list_dir"
    assert seen["stream"] is True and seen["keep_alive"] == "30m" and seen["options"]["num_ctx"] == 8192
    assert "keep_alive" not in seen["options"]


def test_non_streaming_still_works_and_keeps_model_loaded():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "pong"}})

    p = provider_with(handler)
    p.options = {"keep_alive": "2h", "top_k": 20}
    assert p.chat([{"role": "user", "content": "x"}]).content == "pong"
    assert seen["stream"] is False and seen["keep_alive"] == "2h" and seen["options"]["top_k"] == 20


def test_fitted_ctx_shrinks_until_model_fits_and_remembers(tmp_path):
    """Mirrors the RTX 3060 measurements: 14B fits fully only at 8K."""
    sizes = {32768: (15.0, 10.2), 24576: (13.6, 10.2), 16384: (12.3, 10.1), 12288: (11.6, 10.2), 8192: (10.9, 10.9)}
    state = {"ctx": None, "loads": 0}

    def handler(req):
        if req.url.path == "/api/generate":
            state["ctx"] = json.loads(req.content)["options"]["num_ctx"]
            state["loads"] += 1
            return httpx.Response(200, json={})
        size, vram = sizes[state["ctx"]]
        return httpx.Response(200, json={"models": [{"name": "qwen3:14b", "size": size * 1e9, "size_vram": vram * 1e9}]})

    p = provider_with(handler)
    p.model = "qwen3:14b"
    store = tmp_path / "fit.json"
    assert p.fitted_ctx(32768, store) == 8192 and state["loads"] == 5
    assert json.loads(store.read_text())["qwen3:14b@32768"] == 8192
    p2 = provider_with(handler)
    p2.model = "qwen3:14b"
    assert p2.fitted_ctx(32768, store) == 8192 and state["loads"] == 5   # remembered, no re-probe


def test_fitted_ctx_keeps_full_window_when_it_fits(tmp_path):
    def handler(req):
        if req.url.path == "/api/generate":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b", "size": 10.3e9, "size_vram": 10.3e9}]})
    assert provider_with(handler).fitted_ctx(32768, tmp_path / "fit.json") == 32768


def test_fitted_ctx_never_blocks_on_errors(tmp_path):
    def handler(req):
        raise httpx.ConnectError("down")
    assert provider_with(handler).fitted_ctx(32768, tmp_path / "f.json") == 32768


def test_codex_lean_flags_and_user_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n\n[mcp_servers.x]\nmodel = "nope"\n')
    a = ChatGPTBridge().argv("out.txt", [])
    for flag in ("--ignore-user-config", "--ephemeral", "--ignore-rules"):
        assert flag in a
    assert a[a.index("--model") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="medium"' in a and a[-1] == "-"
    b = ChatGPTBridge(model="gpt-5.5", reasoning="low").argv("out.txt", [])
    assert b[b.index("--model") + 1] == "gpt-5.5" and 'model_reasoning_effort="low"' in b


def test_agent_streams_tokens_to_ui(tmp_path):
    from test_localagent import FakeRouter
    from localagent.agent import Agent
    from localagent.config import DEFAULTS, Config, deep_merge
    from localagent.llm import LLMResponse
    from localagent.runtime import Runtime

    class StreamingFake:
        name, model, supports_tools, label = "fake", "m", True, "fake:m"
        def chat(self, messages, tools=None, on_token=None, **kw):
            for t in ("Done ", "quickly."):
                on_token and on_token(t)
            return LLMResponse(content="Done quickly.")

    cfg = Config(deep_merge(DEFAULTS, {"workspace": str(tmp_path / "ws"), "agent": {"auto_recall_memories": 0}}))
    cfg.home = tmp_path / "home"
    cfg.ensure_dirs()
    events = []
    rt = Runtime(cfg, router=FakeRouter(StreamingFake()))
    Agent(rt, on_event=lambda k, d: events.append((k, d))).run("hi")
    assert [d["text"] for k, d in events if k == "token"] == ["Done ", "quickly."]
    assert events[-1][0] == "final"


def test_router_routes_by_job_and_rests_exhausted(monkeypatch):
    from localagent.llm import Router, brain_key

    class B:
        def __init__(self, key):
            self.key, self.name, self.label = key, key, key.title()
        def available(self, refresh=False):
            return True

    r = Router({"main": {"provider": "ollama", "model": "x"}}, {"ollama": {"base_url": "http://x"}}, [])
    brains = [B("chatgpt"), B("gemini"), B("claude")]
    monkeypatch.setattr(r, "all_brains", lambda refresh=False: list(brains))
    assert r.route("fix the bug in my python script")[0] == "chatgpt"
    assert r.route("research the latest news on Wisconsin housing and summarize")[0] == "gemini"
    assert r.route("draft a polite email to my professor")[0] == "claude"
    assert r.route("hi there") == ("", "")
    r.rest(brains[0])
    assert r.route("fix the bug in my python script")[0] != "chatgpt"          # resting: route elsewhere
    assert [brain_key(p) for p in r.brain_chain(prefer="claude")][0] == "claude"
    assert Router.is_quota_error("You've hit your usage limit") and not Router.is_quota_error("file not found")


def test_task_board_runs_jobs_and_notifies(tmp_path):
    import time as _t
    from test_localagent import FakeProvider, FakeRouter
    from localagent.automations import Inbox, Owner
    from localagent.config import DEFAULTS, Config, deep_merge
    from localagent.llm import LLMResponse
    from localagent.runtime import Runtime
    from localagent.tasks import TaskManager

    cfg = Config(deep_merge(DEFAULTS, {"workspace": str(tmp_path / "ws"), "agent": {"auto_recall_memories": 0}}))
    cfg.home = tmp_path / "home"
    cfg.ensure_dirs()
    brain = FakeProvider("m", lambda m, t: LLMResponse(content="Found 3 flights."))
    inbox = Inbox(cfg.home)
    tm = TaskManager(cfg, lambda: Runtime(cfg, router=FakeRouter(brain)), Owner(inbox))
    ids = [tm.create(f"job {i}")["id"] for i in range(3)]
    for _ in range(100):
        if all(tm.get(i)["status"] == "done" for i in ids):
            break
        _t.sleep(0.05)
    assert all(tm.get(i)["status"] == "done" and tm.get(i)["result"] == "Found 3 flights." for i in ids)
    assert len([x for x in inbox.list() if x["title"].startswith("Done:")]) == 3
    tm.reply(ids[0], "now sort them by price")
    for _ in range(100):
        if tm.get(ids[0])["status"] == "done":
            break
        _t.sleep(0.05)
    assert tm.get(ids[0])["replies"][0]["text"] == "now sort them by price"
    tm2 = TaskManager(cfg, lambda: None, Owner(inbox))          # survives a restart
    assert {t["id"] for t in tm2.list()} >= set(ids)
