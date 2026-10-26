"""Task board: fire off several jobs (from the phone or the PC) and let them
run side by side, each on the brain that suits it. Every task gets its own
agent, shows live status, asks you (app, phone) before risky steps, and
pings you when it's done.

Local-model jobs run one at a time (one GPU); subscription brains run in
parallel up to `max_parallel_tasks`."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

STATUSES = ("queued", "running", "waiting", "done", "failed", "stopped")


class TaskManager:
    def __init__(self, cfg, make_runtime: Callable, owner, on_change: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.make_runtime = make_runtime
        self.owner = owner
        self.on_change = on_change
        self.path: Path = cfg.home / "tasks.json"
        self.lock = threading.RLock()
        self.tasks: dict[str, dict] = {}
        self.live: dict[str, dict] = {}      # id -> {"rt", "agent"}
        self.local_gpu = threading.Semaphore(1)
        self.slots = threading.Semaphore(max(1, int(cfg.data.get("max_parallel_tasks", 3))))
        self._load()

    # ------------------------------------------------------------ store
    def _load(self) -> None:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            items = []
        for t in items:
            if t.get("status") in ("queued", "running", "waiting"):
                t["status"] = "stopped"
                t["result"] = (t.get("result") or "") + "\n(LocalAgent was closed while this was running.)"
            self.tasks[t["id"]] = t

    def _save(self) -> None:
        with self.lock:
            items = sorted(self.tasks.values(), key=lambda t: t["created"], reverse=True)[:150]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.path)

    def _touch(self, t: dict) -> None:
        t["updated"] = time.time()
        self._save()
        if self.on_change:
            try:
                self.on_change(self.public(t))
            except Exception:
                pass

    @staticmethod
    def public(t: dict) -> dict:
        return {k: v for k, v in t.items() if k != "history"}

    def list(self) -> list[dict]:
        with self.lock:
            return [self.public(t) for t in sorted(self.tasks.values(), key=lambda t: t["created"], reverse=True)]

    def get(self, tid: str) -> dict | None:
        t = self.tasks.get(tid)
        return dict(t) if t else None

    # ------------------------------------------------------------- create
    def create(self, message: str, brain: str = "auto", mode: str = "auto", files: list[str] | None = None,
               source: str = "app") -> dict:
        message = message.strip()
        if not message:
            raise ValueError("Say what the task is.")
        t = {"id": uuid.uuid4().hex[:8], "title": message.splitlines()[0][:90], "message": message,
             "brain": brain or "auto", "mode": mode, "files": files or [], "source": source,
             "status": "queued", "created": time.time(), "started": None, "finished": None, "updated": time.time(),
             "result": "", "route": "", "steps": [], "approval": None, "replies": []}
        with self.lock:
            self.tasks[t["id"]] = t
        self._touch(t)
        threading.Thread(target=self._run, args=(t["id"], message), daemon=True, name=f"task-{t['id']}").start()
        return self.public(t)

    def reply(self, tid: str, message: str) -> dict:
        t = self.tasks.get(tid)
        if not t:
            raise KeyError(tid)
        if t["status"] in ("queued", "running", "waiting"):
            raise ValueError("That task is still working. Wait for it, or stop it first.")
        t["replies"].append({"time": time.time(), "text": message})
        t["status"] = "queued"
        t["approval"] = None
        self._touch(t)
        threading.Thread(target=self._run, args=(tid, message), daemon=True, name=f"task-{tid}").start()
        return self.public(t)

    def stop(self, tid: str) -> bool:
        live = self.live.get(tid)
        t = self.tasks.get(tid)
        if live:
            live["rt"].cancel.set()
        if t and t["status"] == "queued":
            t["status"] = "stopped"
            self._touch(t)
        if t and t.get("approval"):
            self.owner.answer(t["approval"]["id"], False)
        return bool(live or t)

    # ---------------------------------------------------------------- run
    def _step(self, t: dict, text: str, ok: bool | None = None) -> None:
        t["steps"] = (t["steps"] + [{"time": time.time(), "text": text, "ok": ok}])[-60:]
        self._touch(t)

    def _run(self, tid: str, message: str) -> None:
        from .agent import Agent
        t = self.tasks[tid]
        live = self.live.get(tid)
        if live is None:
            rt = self.make_runtime()
            agent = Agent(rt, mode=t["mode"], brain=t["brain"], system_extra=(
                "This is a background task on the user's task board. They may be away from the computer, on their phone. "
                "Finish the whole job, then give a short, complete result they can read on a phone."))
            live = {"rt": rt, "agent": agent}
            self.live[tid] = live
        rt, agent = live["rt"], live["agent"]

        def on_event(kind: str, data: dict) -> None:
            if data.get("subagent"):
                return
            if kind == "tool_call":
                self._step(t, _plain(data["name"], data.get("arguments") or {}))
            elif kind == "tool_result" and t["steps"]:
                t["steps"][-1]["ok"] = bool(data.get("ok"))
                self._touch(t)
            elif kind == "route":
                t["route"] = f"{data['to']} ({data['why']})"
                self._touch(t)
            elif kind == "escalate":
                t["route"] = f"{data['to']} (taking over: {data['why'][:80]})"
                self._step(t, f"Handing off to {data['to']}")
            elif kind == "warning":
                self._step(t, "⚠ " + data.get("text", ""), False)

        agent._on_event = on_event

        def approver(question: str) -> bool:
            t["status"] = "waiting"
            self._touch(t)
            # owner.ask shows it in the app inbox, on the phone and as a notification
            allowed = _ask_tracking(self.owner, f"[{t['title'][:50]}] {question}", t, self._touch,
                                    float((self.cfg.data.get("desktop") or {}).get("approval_timeout", 600)))
            t["status"] = "running"
            t["approval"] = None
            self._touch(t)
            return allowed
        rt.approver = approver

        uses_local = (t["brain"] == "local") or (t["brain"] == "auto" and t["mode"] == "quick")
        gate = self.local_gpu if uses_local else self.slots
        with gate:
            if t["status"] == "stopped":
                return
            t["status"] = "running"
            t["started"] = t["started"] or time.time()
            self._touch(t)
            try:
                images = [p for p in t["files"] if Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}] if not t["replies"] else None
                docs = [p for p in t["files"] if p not in (images or [])] if not t["replies"] else []
                text = message + (("\n\nAttached files: " + ", ".join(docs)) if docs else "")
                result = agent.run(text, images=images or None)
                t["result"] = result
                t["status"] = "stopped" if rt.cancel.is_set() else "done"
            except Exception as e:
                t["result"] = f"Couldn't finish: {type(e).__name__}: {e}"
                t["status"] = "failed"
            finally:
                t["finished"] = time.time()
                t["route"] = t["route"] or agent.provider.label
                self._touch(t)
        if t["status"] in ("done", "failed") and self.cfg.data.get("notify_tasks", True):
            self.owner.notify(("Done: " if t["status"] == "done" else "Couldn't finish: ") + t["title"][:60],
                              t["result"][:3500], source=f"task:{tid}", important=True)


def _ask_tracking(owner, question: str, t: dict, touch, timeout: float) -> bool:
    """owner.ask, but record the approval id on the task so the board can answer it."""
    before = set(owner.pending)
    result = {}

    def ask():
        result["v"] = owner.ask(question, timeout=timeout)
    th = threading.Thread(target=ask, daemon=True)
    th.start()
    for _ in range(50):
        new = set(owner.pending) - before
        if new:
            aid = new.pop()
            t["approval"] = {"id": aid, "question": question}
            touch(t)
            break
        time.sleep(0.02)
    th.join()
    return bool(result.get("v"))


PLAIN = {
    "read_file": "Reading {path}", "write_file": "Writing {path}", "edit_file": "Editing {path}",
    "list_dir": "Looking in a folder", "find_files": "Finding files", "search_text": "Searching files",
    "move_path": "Moving a file", "copy_path": "Copying a file", "trash_path": "Moving {path} to the trash",
    "run_shell": "Running a command", "run_python": "Running a small program", "start_process": "Starting a program",
    "web_search": "Searching the web for “{query}”", "web_fetch": "Reading {url}", "download_file": "Downloading a file",
    "browser": "Using the browser", "read_document": "Reading {path}", "look_at": "Looking at an image",
    "remember": "Remembering something", "recall": "Checking memory", "delegate": "Splitting the work up",
    "contemplate": "Thinking it through", "open_app": "Opening {name}", "see_screen": "Looking at the screen",
    "click": "Clicking", "type_text": "Typing", "press_keys": "Pressing {keys}", "use_skill": "Following the {name} skill",
    "schedule_task": "Scheduling something", "send_phone_message": "Messaging your phone",
}


def _plain(name: str, args: dict) -> str:
    tmpl = PLAIN.get(name)
    if not tmpl:
        if "__" in name:
            srv, tool = name.split("__", 1)
            return f"Using {srv}: {tool.replace('_', ' ')}"
        return name.replace("_", " ").capitalize()
    try:
        vals = {k: (Path(str(v)).name if k == "path" else str(v)[:60]) for k, v in args.items()}
        return tmpl.format_map(_Default(vals))
    except Exception:
        return tmpl.split(" {")[0]


class _Default(dict):
    def __missing__(self, key):
        return "…"
