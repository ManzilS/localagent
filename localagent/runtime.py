"""Shared state for one running agent and all its subagents."""
from __future__ import annotations

import threading
from typing import Callable

from .config import Config
from .llm import Provider, Router
from .memory_store import MemoryStore
from .safety import ensure_hooks
from .trash import Trash

EventFn = Callable[[str, dict], None]


class Cancelled(Exception):
    """Raised inside tools when the user presses Stop."""


class Runtime:
    def __init__(self, cfg: Config, router: Router | None = None, on_event: EventFn | None = None, mcp=None):
        self.cfg = cfg
        from .mcp import MCPManager
        self.mcp = mcp if mcp is not None else MCPManager(cfg)  # connectors start lazily
        from .connections import ConnectionManager
        self.connections = ConnectionManager(cfg)  # email, calendars, notes (the app passes a shared one)
        ensure_hooks(cfg)
        self.router = router or Router(cfg.models, cfg.providers, cfg.data.get("brains"),
                                       routes=cfg.data.get("routing"), cooldown_minutes=float(cfg.data.get("brain_rest_minutes", 60)))
        self.trash = Trash(cfg.trash_dir)
        embedder = None
        try:
            emb = self.router.get("embed")
            embedder = emb if emb is not None and hasattr(emb, "embed") else None
        except Exception:
            embedder = None
        self.memory = MemoryStore(cfg.home / "memory.db", embedder=embedder)
        self.processes: dict = {}
        self.local = threading.local()  # .agent = the agent running on this thread
        self.on_event = on_event
        self._emit_lock = threading.Lock()
        self.cancel = threading.Event()      # set by a Stop button / Ctrl-C
        # approver(question) -> bool. None means nobody can answer: deny.
        self.approver: Callable[[str], bool] | None = None

    def emit(self, kind: str, data: dict) -> None:
        if self.on_event:
            with self._emit_lock:
                try:
                    self.on_event(kind, data)
                except Exception:
                    pass

    def request_approval(self, question: str) -> bool:
        """Ask the person before a risky action. Denies if nobody can answer."""
        self.emit("status", {"text": f"waiting for your approval: {question}"})
        if self.approver is None:
            return False
        try:
            return bool(self.approver(question))
        except Exception:
            return False

    def ctx_for(self, provider) -> int:
        """One context size per model. Ollama reloads a model (~14 s) whenever a
        request asks for a different size, so every call must agree."""
        want = int(self.cfg.agent["context_tokens"])
        fit = getattr(provider, "fitted_ctx", None)
        if fit is None or not self.cfg.agent.get("fit_context_to_gpu", True):
            return want
        return fit(want, self.cfg.home / "fit.json")

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("stopped by user")

    def vision_provider(self) -> Provider | None:
        agent = getattr(self.local, "agent", None)
        p = getattr(agent, "provider", None)
        if p is not None and p is not self.router.main and getattr(p, "accepts_images", True) \
                and getattr(p, "supports_tools", True):
            return p  # API cloud models see images natively
        return self.router.get("vision") or self.router.get("main")

    def shutdown(self) -> None:
        from .tools.shell import _kill
        for bp in self.processes.values():
            if bp.proc.poll() is None:
                _kill(bp.proc)

    def close(self) -> None:
        """Full shutdown, including connector processes."""
        self.shutdown()
        self.mcp.close()
