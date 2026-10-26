"""Tool registry. Tools are plain functions decorated with @tool; the first
parameter is always the Runtime. JSON schemas are written by hand so small
local models get short, unambiguous descriptions."""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable

_ALL: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    group: str = "core"

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None, group: str = "core"):
    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        _ALL[name] = Tool(name, description, {
            "type": "object", "properties": properties, "required": required or []}, fn, group)
        return fn
    return deco


def _load_modules() -> None:
    from . import files, shell, web, documents, memory_tools, subagents, browser, desktop, skill_tools, automation_tools, phone_tools  # noqa: F401


class Registry:
    def __init__(self, runtime, include: set[str] | None = None, exclude: set[str] | None = None):
        _load_modules()
        self.runtime = runtime
        pool = dict(_ALL)
        if not (exclude and "connectors" in exclude):
            from ..mcp import registry_tools
            try:
                pool.update({t.name: t for t in registry_tools(runtime)})
            except Exception:
                pass  # a broken connector must never break the agent
        if not (exclude and "connections" in exclude):
            from ..connections import registry_tools as connection_tools
            try:
                pool.update({t.name: t for t in connection_tools(runtime)})
            except Exception:
                pass
        self.tools = {n: t for n, t in pool.items()
                      if (include is None or n in include or t.group in include)
                      and not (exclude and (n in exclude or t.group in exclude))}

    @property
    def names(self) -> set[str]:
        return set(self.tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (output, ok)."""
        t = self.tools.get(name)
        if t is None:
            close = [n for n in self.tools if name.lower() in n or n in name.lower()]
            return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(self.tools))}" + (
                f". Did you mean {close[0]}?" if close else ""), False
        if not isinstance(args, dict):
            return f"ERROR: arguments for {name} must be a JSON object", False
        allowed = set(t.parameters.get("properties", {}))
        clean = {k: v for k, v in args.items() if k in allowed}
        missing = [r for r in t.parameters.get("required", []) if r not in clean]
        if missing:
            return (f"ERROR: {name} is missing required argument(s) {missing}. "
                    f"Schema: {json.dumps(t.parameters)}"), False
        from .. import guard
        rt = self.runtime
        agent = getattr(rt.local, "agent", None)
        try:
            guard.check(rt, agent, name, clean)
            out = t.func(rt, **clean)
            out = out if isinstance(out, str) else json.dumps(out, default=str)
            src = guard.is_untrusted(rt, name, clean)
            if src and not out.startswith("ERROR"):
                out, hits = guard.fence(out, src)
                if agent is not None:
                    agent.taint.add(src)
                if hits:
                    rt.emit("warning", {"text": f"Content from {src} contains text that looks like instructions to an AI "
                                                f"({'; '.join(sorted(set(hits)))}). LocalAgent was told to ignore it."})
            return out, True
        except Exception as e:
            if type(e).__name__ == "Cancelled":
                raise
            if isinstance(e, PermissionError):
                return f"ERROR (permission): {e}", False
            if isinstance(e, FileNotFoundError):
                return f"ERROR (not found): {e}", False
            tb = traceback.format_exc(limit=3)
            return f"ERROR: {type(e).__name__}: {e}\n{tb[-800:]}", False


def truncate(text: str, limit: int) -> str:
    """Keep the head and tail of long output — errors usually live at the end."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n\n... [{len(text) - limit} chars omitted] ...\n\n{text[-tail:]}"
