"""MCP connectors: plug any Model Context Protocol server into LocalAgent.

Config (~/.localagent/config.yaml):

    mcp_servers:
      github:
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        env: {GITHUB_PERSONAL_ACCESS_TOKEN: "..."}
      notes:
        url: "http://127.0.0.1:8808/mcp"        # Streamable HTTP server
        headers: {Authorization: "Bearer ..."}
        readonly: true                           # never needs approval after outside content
        enabled: false                           # off until you turn it on

Small local models have small context windows, so when the connected
servers expose more than `mcp_inline_limit` tools in total, they are offered
through two lightweight tools instead (connector_tools / use_connector) and
the full schemas load only when needed.

Connectors run programs or reach services you configure, so nothing starts
until it is enabled, and their output is treated as outside content.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Any

import httpx

from . import __version__

PROTOCOL = "2025-06-18"


class MCPError(Exception):
    pass


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:30]


class StdioConn:
    def __init__(self, spec: dict, store=None):
        c = spec["command"]
        cmd = (list(c) if isinstance(c, list) else [c]) + [str(a) for a in spec.get("args") or []]
        env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
        for var, key in (spec.get("env_secrets") or {}).items():   # secrets come from the keychain, not config.yaml
            if store is not None and store.get(key):
                env[var] = store.get(key)
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=env, cwd=spec.get("cwd") or None, **kw)
        except OSError as e:
            raise MCPError(f"couldn't start {cmd[0]}: {e}")
        self.pending: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _read(self):
        assert self.proc.stdout
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                slot = self.pending.get(msg["id"])
                if slot:
                    slot["msg"] = msg
                    slot["ev"].set()
        for slot in list(self.pending.values()):  # process died
            slot["ev"].set()

    def _err(self):
        assert self.proc.stderr
        for raw in iter(self.proc.stderr.readline, b""):
            self.stderr_tail = (self.stderr_tail + [raw.decode("utf-8", "replace").rstrip()])[-20:]

    def send(self, msg: dict, timeout: float = 60) -> dict | None:
        if self.proc.poll() is not None:
            raise MCPError("server exited: " + " | ".join(self.stderr_tail[-3:]))
        slot = None
        if "id" in msg:
            slot = {"ev": threading.Event(), "msg": None}
            self.pending[msg["id"]] = slot
        with self.lock:
            assert self.proc.stdin
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        if slot is None:
            return None
        if not slot["ev"].wait(timeout):
            self.pending.pop(msg["id"], None)
            raise MCPError(f"timed out after {timeout:.0f}s")
        self.pending.pop(msg["id"], None)
        if slot["msg"] is None:
            raise MCPError("server exited: " + " | ".join(self.stderr_tail[-3:]))
        return slot["msg"]

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class HttpConn:
    def __init__(self, spec: dict, auth=None):
        self.auth = auth                     # () -> bearer token, or None; auth(True) forces a refresh
        self.url = spec["url"]
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        **(spec.get("headers") or {})}
        self.client = httpx.Client(timeout=httpx.Timeout(120, connect=10))
        self.session: str | None = None

    def send(self, msg: dict, timeout: float = 60) -> dict | None:
        h = dict(self.headers)
        if self.session:
            h["Mcp-Session-Id"] = self.session
        h["MCP-Protocol-Version"] = PROTOCOL
        if self.auth is not None:
            h["Authorization"] = "Bearer " + self.auth(False)
        r = self.client.post(self.url, json=msg, headers=h, timeout=timeout)
        if r.status_code == 401 and self.auth is not None:
            h["Authorization"] = "Bearer " + self.auth(True)   # expired: refresh once and retry
            r = self.client.post(self.url, json=msg, headers=h, timeout=timeout)
        if r.status_code == 401:
            raise MCPError("not signed in (the service said 401). Sign in again on the Connections page.")
        if "mcp-session-id" in r.headers:
            self.session = r.headers["mcp-session-id"]
        if "id" not in msg:
            return None
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {r.text[:300]}")
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(m, dict) and m.get("id") == msg["id"]:
                        return m
            raise MCPError("no response in event stream")
        return r.json()

    def close(self):
        self.client.close()


class SseConn:
    """The older MCP transport (2024-11): a GET event stream for replies plus
    POSTs to the endpoint the server announces. Some hosted connectors
    (Asana, Atlassian, Webflow…) still use it."""
    def __init__(self, spec: dict, auth=None):
        self.url, self.auth = spec["url"], auth
        self.headers = dict(spec.get("headers") or {})
        self.client = httpx.Client(timeout=httpx.Timeout(None, connect=10))
        self.pending: dict[int, dict] = {}
        self.endpoint: str | None = None
        self.ready = threading.Event()
        self.error = ""
        self.closed = False
        threading.Thread(target=self._listen, daemon=True).start()
        if not self.ready.wait(30):
            raise MCPError(self.error or "the connector didn't open its event stream")
        if self.error:
            raise MCPError(self.error)

    def _h(self, force=False) -> dict:
        h = dict(self.headers)
        if self.auth is not None:
            h["Authorization"] = "Bearer " + self.auth(force)
        return h

    def _listen(self):
        from urllib.parse import urljoin
        try:
            with self.client.stream("GET", self.url, headers={**self._h(), "Accept": "text/event-stream"}) as r:
                if r.status_code == 401:
                    self.error = "not signed in (the service said 401). Sign in again on the Connections page."
                    self.ready.set()
                    return
                if r.status_code >= 400:
                    self.error = f"HTTP {r.status_code}"
                    self.ready.set()
                    return
                event, data = "message", []
                for line in r.iter_lines():
                    if self.closed:
                        return
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif line == "":
                        payload = "\n".join(data)
                        data = []
                        if event == "endpoint":
                            self.endpoint = urljoin(self.url, payload)
                            self.ready.set()
                        elif payload:
                            try:
                                m = json.loads(payload)
                            except json.JSONDecodeError:
                                m = None
                            if isinstance(m, dict) and m.get("id") in self.pending:
                                slot = self.pending[m["id"]]
                                slot["resp"] = m
                                slot["ev"].set()
                        event = "message"
        except Exception as e:
            self.error = self.error or str(e)
        finally:
            self.ready.set()
            for slot in self.pending.values():
                slot["ev"].set()

    def send(self, msg: dict, timeout: float = 60) -> dict | None:
        if not self.endpoint:
            raise MCPError(self.error or "connector stream closed")
        slot = {"ev": threading.Event(), "resp": None}
        if "id" in msg:
            self.pending[msg["id"]] = slot
        r = self.client.post(self.endpoint, json=msg, headers=self._h(), timeout=timeout)
        if r.status_code >= 400:
            self.pending.pop(msg.get("id"), None)
            raise MCPError(f"HTTP {r.status_code}: {r.text[:300]}")
        if "id" not in msg:
            return None
        if not slot["ev"].wait(timeout):
            self.pending.pop(msg["id"], None)
            raise MCPError("the connector didn't answer in time")
        self.pending.pop(msg["id"], None)
        if slot["resp"] is None:
            raise MCPError(self.error or "connector stream closed")
        return slot["resp"]

    def close(self):
        self.closed = True
        self.client.close()


class Server:
    def __init__(self, name: str, spec: dict, store=None):
        self.name, self.spec, self.store = name, spec, store
        self.conn: StdioConn | HttpConn | None = None
        self.tools: list[dict] = []
        self.error: str = ""
        self._id = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.spec.get("enabled", True) is not False

    def _rpc(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        assert self.conn
        with self._lock:
            self._id += 1
            mid = self._id
        resp = self.conn.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, timeout)
        if resp is None:
            return None
        if "error" in resp:
            e = resp["error"]
            raise MCPError(f"{e.get('message', e)}")
        return resp.get("result")

    def _auth(self):
        """Bearer token source for hosted connectors: OAuth sign-in or a saved token."""
        if self.store is None:
            return None
        if self.spec.get("oauth"):
            from .mcp_oauth import TokenSource
            src = TokenSource(self.store, self.name)
            return lambda force=False: src.token(force)
        if self.spec.get("token_in_keychain"):
            return lambda force=False: self.store.get(f"mcp:{self.name}:token")
        return None

    def start(self) -> None:
        if self.conn is not None:
            return
        try:
            url = self.spec.get("url") or ""
            if url and (self.spec.get("transport") == "sse" or url.rstrip("/").endswith("/sse")):
                self.conn = SseConn(self.spec, self._auth())
            elif url:
                self.conn = HttpConn(self.spec, self._auth())
            else:
                self.conn = StdioConn(self.spec, self.store)
            self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "localagent", "version": __version__}},
                      timeout=float(self.spec.get("startup_timeout", 60)))
            self.conn.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools, cursor = [], None
            for _ in range(20):
                res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}) or {}
                tools += res.get("tools", [])
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            self.tools = tools
            self.error = ""
        except Exception as e:
            self.error = str(e)
            if self.conn:
                self.conn.close()
            self.conn = None
            raise MCPError(f"{self.name}: {e}")

    def call(self, tool: str, args: dict) -> str:
        self.start()
        res = self._rpc("tools/call", {"name": tool, "arguments": args or {}},
                        timeout=float(self.spec.get("call_timeout", 120))) or {}
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                r = c.get("resource", {})
                parts.append(r.get("text") or f"[resource {r.get('uri', '')}]")
            elif c.get("type") == "image":
                parts.append(f"[image: {c.get('mimeType', 'image')}]")
            else:
                parts.append(json.dumps(c)[:2000])
        if res.get("structuredContent") and not parts:
            parts.append(json.dumps(res["structuredContent"]))
        text = "\n".join(parts) or "(no output)"
        return ("ERROR from connector: " + text) if res.get("isError") else text

    def is_readonly(self, tool: str) -> bool:
        ro = self.spec.get("readonly")
        if ro is True or (isinstance(ro, list) and tool in ro):
            return True
        t = next((t for t in self.tools if t["name"] == tool), None)
        return bool(t and (t.get("annotations") or {}).get("readOnlyHint"))

    def close(self):
        if self.conn:
            self.conn.close()
        self.conn = None


class MCPManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.servers: dict[str, Server] = {}
        from .connections.secrets import Secrets
        self.store = Secrets(cfg.home)
        for name, spec in (cfg.data.get("mcp_servers") or {}).items():
            if isinstance(spec, dict) and (spec.get("command") or spec.get("url")):
                self.servers[_safe(name)] = Server(_safe(name), spec, self.store)
        self._started = False

    def enabled(self) -> list[Server]:
        return [s for s in self.servers.values() if s.enabled]

    def start_all(self) -> None:
        if self._started:
            return
        self._started = True
        threads = [threading.Thread(target=self._try_start, args=(s,), daemon=True) for s in self.enabled()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

    def _try_start(self, s: Server) -> None:
        try:
            s.start()
        except MCPError:
            pass

    def total_tools(self) -> int:
        return sum(len(s.tools) for s in self.enabled())

    def inline(self) -> bool:
        return self.total_tools() <= int(self.cfg.data.get("mcp_inline_limit", 12))

    # name resolution -------------------------------------------------
    def resolve(self, name: str, args: dict | None = None) -> tuple[Server, str] | None:
        if name == "use_connector" and args:
            s = self.servers.get(_safe(str(args.get("connector", ""))))
            return (s, str(args.get("tool", ""))) if s else None
        if "__" in name:
            srv, tool = name.split("__", 1)
            s = self.servers.get(srv)
            if s and any(_tool_id(s.name, t["name"]) == name for t in s.tools):
                real = next(t["name"] for t in s.tools if _tool_id(s.name, t["name"]) == name)
                return s, real
        return None

    def owns(self, name: str, args: dict | None = None) -> bool:
        return name in ("use_connector", "connector_tools") or self.resolve(name, args) is not None

    def server_of(self, name: str, args: dict | None = None) -> str:
        r = self.resolve(name, args)
        return r[0].name if r else "a connector"

    def short_name(self, name: str, args: dict | None = None) -> str:
        r = self.resolve(name, args)
        return r[1] if r else name

    def is_readonly(self, name: str, args: dict | None = None) -> bool:
        r = self.resolve(name, args)
        return bool(r and r[0].is_readonly(r[1]))

    def status(self) -> list[dict]:
        return [{"name": s.name, "enabled": s.enabled, "running": s.conn is not None, "tools": len(s.tools),
                 "error": s.error, "kind": "url" if s.spec.get("url") else "command",
                 "target": s.spec.get("url") or " ".join(s.spec["command"] if isinstance(s.spec["command"], list) else [s.spec["command"]])}
                for s in self.servers.values()]

    def close(self) -> None:
        for s in self.servers.values():
            s.close()
        self._started = False


def _tool_id(server: str, tool: str) -> str:
    return f"{server}__{_safe(tool)}"[:64]


def _find_tool():
    from .tools import Tool

    def find_connector(rt_, app: str) -> str:
        from .connections.catalog import CATALOG
        from .mcp_registry import search
        q = app.lower()
        built = [e for e in CATALOG if q in e["name"].lower() or q in e.get("blurb", "").lower()]
        lines = [f"In LocalAgent's catalog: {e['name']} ({e['group']}) — the user can connect it on the Connections page."
                 for e in built[:5]]
        try:
            for r in search(app, limit=6):
                how = "hosted, sign in on the web" if r["remotes"] else f"runs on this PC ({r['packages'][0]['type']} package)"
                lines.append(f"In the MCP Registry: {r['title']} — {r['description'][:160]} [{how}] ({r['name']})")
        except Exception as e:
            lines.append(f"(couldn't search the MCP Registry: {e})")
        return ("\n".join(lines) or f"No connector found for {app}.") + (
            "\n\nYou can't add connectors yourself: tell the user which one to add on the Connections page (search box at the bottom). "
            "If there's no connector, or until they add one, use the website in the browser instead (open_website / browser goto).")
    return Tool("find_connector", "Look for a way to connect an app or service LocalAgent can't reach yet (searches the built-in "
                "catalog and the official MCP Registry). Suggest it to the user; they add it.",
                {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}, find_connector, group="connectors")


def registry_tools(rt) -> list:
    """Tool objects for the registry: one per connector tool when there are
    few, otherwise the two lazy meta-tools."""
    from .tools import Tool
    mgr: MCPManager | None = getattr(rt, "mcp", None)
    if mgr is None or not mgr.enabled():
        return [_find_tool()]
    mgr.start_all()
    live = [s for s in mgr.enabled() if s.tools]
    if not live:
        return [_find_tool()]
    if mgr.inline():
        out = [_find_tool()]
        for s in live:
            for t in s.tools:
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                fn = (lambda srv, tool: lambda rt_, **kw: srv.call(tool, kw))(s, t["name"])
                out.append(Tool(_tool_id(s.name, t["name"]), f"[{s.name} connector] {(t.get('description') or '')[:300]}",
                                schema, fn, group="connectors"))
        return out

    def connector_tools(rt_, connector: str) -> str:
        s = mgr.servers.get(_safe(connector))
        if not s:
            return f"no connector '{connector}'. Connected: {', '.join(x.name for x in live)}"
        return "\n\n".join(f"{t['name']}: {(t.get('description') or '')[:240]}\nargs: {json.dumps((t.get('inputSchema') or {}).get('properties', {}))[:600]}"
                           for t in s.tools)

    def use_connector(rt_, connector: str, tool: str, arguments: dict | None = None) -> str:
        s = mgr.servers.get(_safe(connector))
        if not s:
            return f"no connector '{connector}'"
        if not any(t["name"] == tool for t in s.tools):
            return f"{connector} has no tool '{tool}'. Call connector_tools('{connector}') to see them."
        return s.call(tool, arguments or {})

    summary = "; ".join(f"{s.name} ({len(s.tools)} tools)" for s in live)
    return [_find_tool(),
        Tool("connector_tools", f"List what a connected app can do. Connected: {summary}.",
             {"type": "object", "properties": {"connector": {"type": "string"}}, "required": ["connector"]},
             connector_tools, group="connectors"),
        Tool("use_connector", "Run a tool on a connected app (see connector_tools for names and arguments).",
             {"type": "object", "properties": {"connector": {"type": "string"}, "tool": {"type": "string"},
                                               "arguments": {"type": "object"}}, "required": ["connector", "tool"]},
             use_connector, group="connectors"),
    ]


def import_from_other_apps() -> dict[str, dict]:
    """Find MCP servers already configured for Claude Desktop, Cursor, or
    Windsurf so the user can bring them over (they start disabled)."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json"),
        os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json"),
        os.path.join(home, ".config", "Claude", "claude_desktop_config.json"),
        os.path.join(home, ".cursor", "mcp.json"),
        os.path.join(home, ".codeium", "windsurf", "mcp_config.json"),
    ]
    found: dict[str, dict] = {}
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for name, spec in (data.get("mcpServers") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("command"):
                entry = {"command": [spec["command"]] + list(spec.get("args") or []), "enabled": False}
                if spec.get("env"):
                    entry["env"] = spec["env"]
            elif spec.get("url"):
                entry = {"url": spec["url"], "enabled": False}
                if spec.get("headers"):
                    entry["headers"] = spec["headers"]
            else:
                continue
            entry["imported_from"] = path
            found.setdefault(_safe(name), entry)
    return found
