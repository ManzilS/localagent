"""End-to-end tests with a scripted fake model. Real tools, real subprocesses,
real trash — only the LLM is mocked."""
import json
import os
import threading
from pathlib import Path

import pytest

from localagent.agent import Agent
from localagent.config import DEFAULTS, Config, deep_merge
from localagent.llm import LLMResponse, ToolCall, parse_text_tool_calls
from localagent.runtime import Runtime
from localagent.safety import check_shell_command
from localagent.trash import Trash, TrashError


# ------------------------------------------------------------------ fixtures

class FakeProvider:
    """Replies via a function of the transcript, so it works across threads."""
    name = "fake"
    supports_tools = True

    def __init__(self, label, fn):
        self.model, self._fn, self.calls = label, fn, []
        self.base_url = "http://fake"
        self._lock = threading.Lock()

    @property
    def label(self):
        return f"fake:{self.model}"

    def chat(self, messages, tools=None, **kw):
        with self._lock:
            self.calls.append(messages)
        return self._fn(messages, tools)


class FakeRouter:
    def __init__(self, main, cloud=None, vision=None):
        self._p = {"main": main, "cloud": cloud, "vision": vision or main, "embed": None}

    def get(self, role):
        return self._p.get(role)

    @property
    def main(self):
        return self._p["main"]

    @property
    def cloud(self):
        return self._p["cloud"]

    def brain_chain(self, refresh=False, prefer=""):
        return [self._p["cloud"]] if self._p["cloud"] else []

    def deep(self, prefer=""):
        return self._p["cloud"] or self._p["main"]

    def route(self, task):
        return "", ""

    def by_key(self, key):
        return self._p["main"] if key in ("local", "main") else self._p["cloud"]

    def rest(self, p, minutes=None):
        pass

    def status(self):
        return [{"key": "fake", "label": p.label, "resting_until": None, "good_at": ""} for p in self.brain_chain()]

    @staticmethod
    def is_quota_error(err):
        from localagent.llm import Router
        return Router.is_quota_error(err)


def tc(name, **args):
    return LLMResponse(tool_calls=[ToolCall(id=f"c_{name}_{len(json.dumps(args))}", name=name, arguments=args)])


def script(*responses):
    it = iter(responses)
    return lambda msgs, tools: next(it)


@pytest.fixture
def cfg(tmp_path):
    data = deep_merge(DEFAULTS, {"workspace": str(tmp_path / "ws"), "allowed_roots": [str(tmp_path)],
                                 "agent": {"auto_recall_memories": 0}})
    c = Config(data)
    c.home = tmp_path / "home"
    c.ensure_dirs()
    return c


def runtime(cfg, main, cloud=None):
    return Runtime(cfg, router=FakeRouter(main, cloud))


# --------------------------------------------------------------------- trash

def test_trash_move_restore_and_backup(tmp_path):
    t = Trash(tmp_path / "agent_trash")
    f = tmp_path / "notes.txt"
    f.write_text("v1")
    dest = t.move(f, reason="test")
    assert not f.exists() and dest.read_text() == "v1"
    f.write_text("v2")  # something new now sits at the original path
    tid = t.list()[0]["id"]
    t.restore(tid)
    assert f.read_text() == "v1"  # restored...
    assert any(e["original"] == str(f) and Path(e["stored"]).read_text() == "v2" for e in t.list())  # ...v2 kept
    assert t.backup(f) and f.exists()


def test_trash_refuses_itself(tmp_path):
    t = Trash(tmp_path / "ws" / "agent_trash")
    with pytest.raises(TrashError):
        t.move(tmp_path / "ws")  # would swallow the trash folder
    with pytest.raises(TrashError):
        t.move(tmp_path / "missing.txt")


# -------------------------------------------------------------------- guards

@pytest.mark.parametrize("cmd", [
    "/bin/rm -rf x", "\\rm x", "find . -name '*.log' -delete", "git clean -fdx", "git rm file.py",
    "git reset --hard HEAD~1", "rsync -a --delete a/ b/", "sudo rm -rf /var/x", "dd if=/dev/zero of=disk.img",
    "node -e \"require('fs').unlinkSync('a')\"", "mkfs.ext4 /dev/sdb1", "git stash drop",
])
def test_shell_guard_blocks(cmd):
    assert check_shell_command(cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf build", "ls -la", "git status", "git rm --cached secrets.env", "python3 -m http.server",
    "find . -name '*.py' -exec wc -l {} +", "pip install requests", "mv a.txt b.txt",
])
def test_shell_guard_allows(cmd):
    assert check_shell_command(cmd) is None


def test_rm_shim_moves_to_trash(cfg):
    rt = runtime(cfg, FakeProvider("m", script()))
    d = cfg.workspace / "build"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.o").write_text("obj")
    (cfg.workspace / "x.txt").write_text("x")
    reg = Agent(rt).registry
    command = (
        "Remove-Item -Recurse -Force build; Remove-Item x.txt; Remove-Item nope.txt; Write-Output done"
        if os.name == "nt" else "rm -rf build && rm x.txt && rm nope.txt; echo done"
    )
    out, ok = reg.call("run_shell", {"command": command})
    missing_message = "nothing matched" if os.name == "nt" else "No such file"
    assert ok and "done" in out and missing_message in out
    assert not d.exists() and not (cfg.workspace / "x.txt").exists()
    originals = {e["original"] for e in rt.trash.list()}
    assert str(d) in originals and str(cfg.workspace / "x.txt") in originals


def test_mv_and_cp_shims_preserve_overwritten(cfg):
    rt = runtime(cfg, FakeProvider("m", script()))
    ws = cfg.workspace
    (ws / "a.txt").write_text("A"); (ws / "b.txt").write_text("B"); (ws / "c.txt").write_text("C")
    reg = Agent(rt).registry
    command = (
        "Move-Item -Force a.txt b.txt; Copy-Item -Force c.txt b.txt"
        if os.name == "nt" else "mv a.txt b.txt && cp c.txt b.txt"
    )
    reg.call("run_shell", {"command": command})
    assert not (ws / "a.txt").exists()
    assert (ws / "b.txt").read_text() == "C"
    kept = sorted(Path(e["stored"]).read_text() for e in rt.trash.list())
    assert kept == ["A", "B"]  # B (overwritten by mv) and A (overwritten by cp) both survive


def test_python_hook_traps_deletes(cfg):
    rt = runtime(cfg, FakeProvider("m", script()))
    ws = cfg.workspace
    (ws / "f1.txt").write_text("1"); (ws / "f2.txt").write_text("2"); (ws / "d").mkdir(); (ws / "d" / "k").write_text("k")
    code = """
import os, shutil, pathlib, tempfile
os.remove('f1.txt'); pathlib.Path('f2.txt').unlink(); shutil.rmtree('d')
with tempfile.TemporaryDirectory() as t:
    open(os.path.join(t, 'tmp'), 'w').write('scratch')
print('ok', os.path.exists('f1.txt'), os.path.exists('d'))
"""
    out, ok = Agent(rt).registry.call("run_python", {"code": code})
    assert "ok False False" in out, out
    originals = {Path(e["original"]).name for e in rt.trash.list()}
    assert {"f1.txt", "f2.txt", "d"} <= originals
    assert not any("tmp" == Path(e["original"]).name for e in rt.trash.list())  # temp space really deleted


def test_file_tools_back_up_and_protect_trash(cfg):
    rt = runtime(cfg, FakeProvider("m", script()))
    reg = Agent(rt).registry
    reg.call("write_file", {"path": "doc.md", "content": "hello world"})
    reg.call("edit_file", {"path": "doc.md", "old_text": "world", "new_text": "there"})
    assert (cfg.workspace / "doc.md").read_text() == "hello there"
    assert [Path(e["stored"]).read_text() for e in rt.trash.list()] == ["hello world"]
    out, ok = reg.call("write_file", {"path": "agent_trash/evil.txt", "content": "x"})
    assert not ok and "read-only" in out
    out, ok = reg.call("write_file", {"path": "/etc/localagent_test", "content": "x"})
    assert not ok and "outside the allowed" in out
    reg.call("write_file", {"path": "new.md", "content": "new"})
    reg.call("move_path", {"source": "new.md", "destination": "doc.md"})
    assert (cfg.workspace / "doc.md").read_text() == "new"
    assert any(Path(e["stored"]).read_text() == "hello there" for e in rt.trash.list())


# ------------------------------------------------------------------- parsing

def test_parse_text_tool_calls():
    names = {"read_file", "web_search"}
    calls, rest = parse_text_tool_calls('Sure.\n<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>', names)
    assert calls[0].name == "read_file" and calls[0].arguments == {"path": "a"} and rest == "Sure."
    calls, _ = parse_text_tool_calls('```json\n{"tool": "web_search", "args": {"query": "x"}}\n```', names)
    assert calls[0].arguments == {"query": "x"}
    calls, _ = parse_text_tool_calls('I will call {"name":"web_search","parameters":{"query":"y {z}"}} now', names)
    assert calls[0].arguments == {"query": "y {z}"}
    calls, rest = parse_text_tool_calls('{"name": "unknown_tool", "arguments": {}}', names)
    assert calls == []


# ---------------------------------------------------------------- the loop

def test_agent_completes_task_with_tools(cfg):
    main = FakeProvider("main", script(
        tc("write_file", path="hello.py", content="print('hi from agent')"),
        tc("run_python", code=open(__file__).read()[:0] + "import runpy; runpy.run_path('hello.py')"),
        LLMResponse(content="Created hello.py and ran it."),
    ))
    rt = runtime(cfg, main)
    events = []
    agent = Agent(rt, on_event=lambda k, d: events.append((k, d)))
    out = agent.run("make a hello script and run it")
    assert out == "Created hello.py and ran it."
    tool_results = [d["output"] for k, d in events if k == "tool_result"]
    assert "hi from agent" in tool_results[1]
    assert agent.messages[-1]["role"] == "assistant"


def test_text_tool_calls_in_loop(cfg):
    main = FakeProvider("main", script(
        LLMResponse(content='<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>'),
        LLMResponse(content="Your workspace is empty except the trash."),
    ))
    out = Agent(runtime(cfg, main)).run("what's in my workspace?")
    assert "workspace" in out
    assert any(m["role"] == "tool" and "agent_trash" in m["content"] for m in main.calls[-1])


def test_auto_mode_escalates_to_cloud(cfg):
    bad = FakeProvider("small", lambda m, t: tc("not_a_real_tool", x=1))
    cloud = FakeProvider("cloud", script(LLMResponse(content="Solved by the big model.")))
    events = []
    agent = Agent(runtime(cfg, bad, cloud), mode="auto", on_event=lambda k, d: events.append(k))
    assert agent.run("hard task") == "Solved by the big model."
    assert "escalate" in events and agent.escalated
    # the cloud model got the whole transcript including the failed tool results
    assert any(m["role"] == "tool" for m in cloud.calls[0])


def test_quick_mode_never_escalates(cfg):
    cfg.agent["max_steps"] = 5
    bad = FakeProvider("small", lambda m, t: tc("not_a_real_tool", x=1) if t else LLMResponse(content="gave up"))
    cloud = FakeProvider("cloud", script())
    agent = Agent(runtime(cfg, bad, cloud), mode="quick")
    assert agent.run("task") == "gave up"
    assert not cloud.calls


def test_deep_mode_uses_cloud(cfg):
    main = FakeProvider("small", script())
    cloud = FakeProvider("cloud", script(LLMResponse(content="deep answer")))
    assert Agent(runtime(cfg, main, cloud), mode="deep").run("q") == "deep answer"
    assert not main.calls


def test_repeated_call_nudge(cfg):
    cfg.agent["max_steps"] = 6
    main = FakeProvider("m", lambda m, t: tc("list_dir") if t else LLMResponse(content="summary"))
    agent = Agent(runtime(cfg, main), mode="quick")
    agent.run("loop forever")
    assert any("same tool call 3 times" in (m.get("content") or "") for m in agent.messages)


def test_delegate_runs_subagents_in_parallel(cfg):
    cfg.agent["max_parallel_subagents"] = 3
    seen_threads = set()

    def brain(msgs, tools):
        seen_threads.add(threading.get_ident())
        system, last = msgs[0]["content"], msgs[-1]
        if "You are a subagent" in system:
            task = next(m["content"] for m in msgs if m["role"] == "user")
            return LLMResponse(content=f"result for <{task}>")
        if last["role"] == "user":
            return tc("delegate", tasks=["alpha", "beta", "gamma"])
        return LLMResponse(content="combined: " + last["content"].count("result for") * "x")

    out = Agent(runtime(cfg, FakeProvider("m", brain))).run("research three things")
    assert out == "combined: xxx"
    assert len(seen_threads) >= 2


def test_contemplate_synthesizes(cfg):
    def brain(msgs, tools):
        sys_ = msgs[0]["content"]
        if "You are a subagent" in sys_:
            return LLMResponse(content="draft answer 42")
        if "merge independent expert drafts" in sys_:
            return LLMResponse(content="final: 42 (drafts agreed)" if msgs[1]["content"].count("draft answer 42") == 3 else "bad")
        return tc("contemplate", question="what is 6*7?", n=3) if msgs[-1]["role"] == "user" else LLMResponse(content=msgs[-1]["content"])

    out = Agent(runtime(cfg, FakeProvider("m", brain))).run("think hard: 6*7")
    assert out == "final: 42 (drafts agreed)"


def test_context_compaction_keeps_transcript_valid(cfg):
    cfg.agent["context_tokens"] = 9000
    cfg.agent["max_tool_output_chars"] = 6000
    (cfg.workspace / "big.txt").write_text("lorem ipsum dolor " * 2000)
    n = {"i": 0}

    def brain(msgs, tools):
        if tools is None and "Summarize an AI agent" in msgs[0]["content"]:
            return LLMResponse(content="SUMMARY: read big.txt several times")
        n["i"] += 1
        return tc("read_file", path="big.txt", offset=n["i"]) if n["i"] < 8 else LLMResponse(content="done")

    agent = Agent(runtime(cfg, FakeProvider("m", brain)), mode="quick")
    assert agent.run("read big.txt a lot") == "done"
    assert any("SUMMARY" in (m.get("content") or "") for m in agent.messages)
    # every tool message must still follow its assistant tool_call
    ids = set()
    for m in agent.messages:
        if m["role"] == "assistant":
            ids |= {c["id"] for c in m.get("tool_calls") or []}
        if m["role"] == "tool":
            assert m["tool_call_id"] in ids


def test_memory_roundtrip(cfg):
    rt = runtime(cfg, FakeProvider("m", script()))
    reg = Agent(rt).registry
    reg.call("remember", {"text": "Nick prefers Python and lives in Madison, WI"})
    reg.call("remember", {"text": "The AuraBook project is deployed on Vercel"})
    out, _ = reg.call("recall", {"query": "where is AuraBook deployed?"})
    assert "Vercel" in out.splitlines()[0]


def test_interrupt_repair(cfg):
    agent = Agent(runtime(cfg, FakeProvider("m", script())))
    agent.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                      {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "list_dir", "arguments": {}},
                                                                         {"id": "b", "name": "list_dir", "arguments": {}}]},
                      {"role": "tool", "tool_call_id": "a", "name": "list_dir", "content": "x"}]
    agent.repair()
    assert [m.get("tool_call_id") for m in agent.messages if m["role"] == "tool"] == ["a", "b"]
    assert agent.messages[-1]["role"] == "assistant"


def test_web_ui_streams_events(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    import localagent.webui as webui
    main = FakeProvider("m", script(tc("list_dir"), LLMResponse(content="**All good**")))
    monkeypatch.setattr(webui, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(webui, "Router", lambda *a, **k: FakeRouter(main))
    client = TestClient(webui.create_app(token="tok"), headers={"X-LocalAgent-Token": "tok"})
    assert client.get("/api/status").json()["main_label"] == "fake:m"
    with client.stream("POST", "/api/chat", json={"session": "s1", "message": "hi", "mode": "quick"}) as r:
        events = [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done" and "tool_call" in kinds and "final" in kinds
    assert client.get("/?t=tok").status_code == 200
