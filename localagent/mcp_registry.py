"""Find MCP connectors in the official MCP Registry
(registry.modelcontextprotocol.io) so people can connect apps that aren't in
LocalAgent's own catalog.

Searching is safe. Adding is not automatic: a hosted connector is just a web
address (sign-in happens on the service's page), but a package connector runs
a program on this computer, so the person adds it from the Connections page.
"""
from __future__ import annotations

import re

import httpx

BASE = "https://registry.modelcontextprotocol.io/v0/servers"


def search(query: str, limit: int = 12, client: httpx.Client | None = None) -> list[dict]:
    c = client or httpx.Client(timeout=20)
    r = c.get(BASE, params={"search": query, "limit": min(100, max(limit * 3, 20))}, headers={"Accept": "application/json"})
    r.raise_for_status()
    out, seen = [], set()
    for item in r.json().get("servers", []):
        s = item.get("server", item)
        meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
        if meta.get("isLatest") is False or meta.get("status") in ("deleted", "deprecated"):
            continue
        name = s.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        remotes = [{"type": x.get("type", ""), "url": x.get("url", "")} for x in (s.get("remotes") or []) if x.get("url")]
        pkgs = []
        for p in s.get("packages") or []:
            t = (p.get("transport") or {}).get("type", "stdio")
            if t in ("streamable-http", "sse") and (p.get("transport") or {}).get("url"):
                remotes.append({"type": t, "url": p["transport"]["url"]})
                continue
            env = [{"name": e.get("name"), "secret": bool(e.get("isSecret")), "required": bool(e.get("isRequired")),
                    "description": e.get("description", "")} for e in p.get("environmentVariables") or [] if e.get("name")]
            cmd = command_for(p)
            if cmd:
                pkgs.append({"type": p.get("registryType"), "id": p.get("identifier"), "command": cmd, "env": env})
        if not remotes and not pkgs:
            continue
        out.append({"name": name, "title": s.get("title") or _pretty(name), "description": (s.get("description") or "")[:300],
                    "repository": (s.get("repository") or {}).get("url", ""), "remotes": remotes, "packages": pkgs})
        if len(out) >= limit:
            break
    return out


def command_for(p: dict) -> list[str] | None:
    ident, kind = p.get("identifier"), p.get("registryType")
    if not ident:
        return None
    args = [str(a.get("value") or a.get("default") or "") for a in p.get("packageArguments") or [] if a.get("value") or a.get("default")]
    if kind == "npm":
        return ["npx", "-y", ident] + args
    if kind == "pypi":
        return ["uvx", ident] + args
    if kind == "oci":
        return ["docker", "run", "-i", "--rm", ident] + args
    return None


def _pretty(name: str) -> str:
    tail = name.split("/")[-1]
    return re.sub(r"[-_]+", " ", re.sub(r"(^mcp[-_]|[-_]?mcp([-_]server)?$|[-_]server$)", "", tail)).strip().title() or tail
