import json
import os
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from localagent.connections import ConnectionManager, registry_tools
from localagent.connections import storage_conn, extras
from localagent.connections.secrets import Secrets
from localagent.trash import Trash


class Cfg:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = tmp / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.data = {"workspace": str(self.workspace), "allowed_roots": [str(tmp)]}


def mgr(tmp):
    return ConnectionManager(Cfg(tmp), secrets=Secrets(tmp / "home", use_keychain=False))


def cloud(tmp):
    d = tmp / "Icedrive"
    (d / "Videos").mkdir(parents=True)
    (d / "Videos" / "trip.mp4").write_bytes(b"v" * 3_000_000)
    (d / "notes.txt").write_text("hello", encoding="utf-8")
    return d


# ------------------------------------------------------------- folder mode
def test_folder_storage_browse_search_bigfiles(tmp_path):
    m = mgr(tmp_path)
    r = m.add("storage", {"mode": "folder", "path": str(cloud(tmp_path)), "service": "Icedrive"})
    assert r["message"].startswith("Connected")
    c = m.pick("storage")
    assert {i["name"] for i in c.list()} == {"Videos", "notes.txt"}
    assert [f["path"] for f in c.search("*.mp4")] == ["Videos/trip.mp4"]
    assert c.big_files(min_mb=2)[0]["path"] == "Videos/trip.mp4" and c.big_files(min_mb=5) == []


def test_folder_storage_never_overwrites_or_deletes(tmp_path):
    m = mgr(tmp_path)
    m.add("storage", {"mode": "folder", "path": str(cloud(tmp_path))})
    c = m.pick("storage")
    local = tmp_path / "notes.txt"
    local.write_text("other", encoding="utf-8")
    assert c.upload(local, "notes.txt") == "notes (2).txt"                 # renamed, original kept
    assert (tmp_path / "Icedrive" / "notes.txt").read_text(encoding="utf-8") == "hello"
    with pytest.raises(FileExistsError):
        c.move("notes (2).txt", "notes.txt")
    where = c.trash("Videos/trip.mp4")
    assert where.startswith("agent_trash/") and (tmp_path / "Icedrive" / where).exists()
    assert not (tmp_path / "Icedrive" / "Videos" / "trip.mp4").exists()
    with pytest.raises(PermissionError):
        c.list("../")


def test_offload_asks_verifies_and_keeps_local_in_trash(tmp_path):
    m = mgr(tmp_path)
    m.add("storage", {"mode": "folder", "path": str(cloud(tmp_path))})
    big = tmp_path / "ws" / "old_backup.zip"
    big.write_bytes(b"z" * 500_000)

    class RT:
        cfg = m.cfg
        connections = m
        trash = Trash(tmp_path / "agent_trash")
        answers = [False, True]
        def request_approval(self, q):
            self.q = q
            return self.answers.pop(0)
    rt = RT()
    tools = {t.name: t for t in registry_tools(rt)}
    assert "Not done" in tools["storage_offload"].func(rt, local_path=str(big))
    assert big.exists()
    out = tools["storage_offload"].func(rt, local_path=str(big))
    assert "copy verified" in out and not big.exists()
    assert (tmp_path / "Icedrive" / "From my PC" / "old_backup.zip").stat().st_size == 500_000
    assert any(e["original"] == str(big) for e in rt.trash.list())            # local copy is in the trash, not gone


def test_overview_lists_cloud_and_local_drives(tmp_path):
    m = mgr(tmp_path)
    m.add("storage", {"mode": "folder", "path": str(cloud(tmp_path)), "service": "Icedrive"})

    class RT:
        connections = m
    out = {t.name: t for t in registry_tools(RT())}["storage_overview"].func(RT())
    assert "Icedrive" in out and "Drives on this computer" in out and "free" in out


# ------------------------------------------------------------- rclone mode
FAKE_RCLONE = r'''
import json, os, shutil, sys
args = sys.argv[1:]
conf = args[args.index("--config") + 1]; args = args[args.index("--config") + 2:]
root = os.environ["FAKE_CLOUD"]
log = open(os.environ["FAKE_LOG"], "a"); log.write(" ".join(args) + "\n"); log.close()
def p(r): return os.path.join(root, r.split(":", 1)[1]) if ":" in r and not os.path.isabs(r) else r
cmd = args[0]
if cmd == "config":
    if args[1] == "create": open(conf, "a").write(f"[{args[2]}]\ntype = {args[3]}\n")
    sys.exit(0)
if cmd == "about":
    print(json.dumps({"total": 2*2**40, "used": 2**40, "free": 2**40, "trashed": 5})); sys.exit(0)
if cmd == "lsjson":
    target = p([a for a in args[1:] if ":" in a][0])
    if "--stat" in args:
        if not os.path.exists(target): print("null"); sys.exit(0)
        import hashlib; h = hashlib.md5(open(target, "rb").read()).hexdigest()
        print(json.dumps({"Size": os.path.getsize(target), "Hashes": {"md5": h}})); sys.exit(0)
    out = []
    for dp, dn, fn in os.walk(target):
        for f in fn:
            fp = os.path.join(dp, f); rel = os.path.relpath(fp, target).replace(os.sep, "/")
            if "--min-size" in args and os.path.getsize(fp) < int(args[args.index("--min-size") + 1][:-1]) * 1024: continue
            out.append({"Path": rel, "Name": f, "Size": os.path.getsize(fp), "IsDir": False, "ModTime": "2025-01-01T00:00:00Z"})
        if "-R" not in args:
            out += [{"Path": d, "Name": d, "Size": -1, "IsDir": True, "ModTime": "2025-01-01T00:00:00Z"} for d in dn]
            break
    print(json.dumps(out)); sys.exit(0)
if cmd in ("copyto", "moveto"):
    s, d = p(args[1]), p(args[2]); os.makedirs(os.path.dirname(d), exist_ok=True)
    (shutil.copy2 if cmd == "copyto" else shutil.move)(s, d); sys.exit(0)
sys.exit(3)
'''


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    bindir = tmp_path / "home" / "bin"
    bindir.mkdir(parents=True)
    script = tmp_path / "fake_rclone.py"
    script.write_text(FAKE_RCLONE, encoding="utf-8")
    if os.name == "nt":
        exe = bindir / "rclone.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = bindir / "rclone"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    remote = tmp_path / "drive"
    (remote / "Photos").mkdir(parents=True)
    (remote / "Photos" / "big.mov").write_bytes(b"m" * 2_000_000)
    monkeypatch.setenv("FAKE_CLOUD", str(remote))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "rclone.log"))
    monkeypatch.setattr(storage_conn, "rclone_exe", lambda home=None: str(exe))
    monkeypatch.setattr(storage_conn.shutil, "which", lambda n: None)
    return remote


def test_rclone_account_connect_and_use(tmp_path, fake_rclone):
    m = mgr(tmp_path)
    r = m.add("storage", {"mode": "rclone", "provider": "drive"})
    assert "1.0 TB used of 2.0 TB" in r["message"]
    log = (tmp_path / "rclone.log").read_text()
    assert "config create storage_" in log and " drive scope=drive" in log      # browser sign-in step
    c = m.pick("storage")
    assert c.big_files(min_mb=1)[0]["path"] == "Photos/big.mov"
    local = tmp_path / "ws" / "report.pdf"
    local.write_bytes(b"%PDF" * 1000)
    dest = c.upload(local, "Docs/report.pdf")
    assert c.verify(local, dest)
    c.trash("Photos/big.mov")
    assert (fake_rclone / "agent_trash").exists() and not (fake_rclone / "Photos" / "big.mov").exists()
    assert not any(w in log for w in (" delete ", " purge ", " rmdir"))


def test_rclone_wrapper_refuses_deleting_commands(tmp_path, fake_rclone):
    rc = storage_conn.Rclone(tmp_path / "home")
    for bad in ("delete", "purge", "rmdir", "sync", "dedupe", "cleanup"):
        with pytest.raises(PermissionError):
            rc.run(bad, "x:")


def test_password_services_dont_keep_the_password(tmp_path, fake_rclone):
    m = mgr(tmp_path)
    r = m.add("storage", {"mode": "rclone", "provider": "mega"}, {"user": "me@x.com", "password": "hunter2"})
    assert m.secrets._load() == {}                                            # handed to rclone once, not kept by LocalAgent
    assert "--obscure" in (tmp_path / "rclone.log").read_text()


# ---------------------------------------------------------------- detection
def test_detect_sync_folders(tmp_path, monkeypatch):
    od = tmp_path / "OneDrive"
    od.mkdir()
    db = tmp_path / "Dropbox (Personal)"
    db.mkdir()
    la = tmp_path / "LocalAppData" / "Dropbox"
    la.mkdir(parents=True)
    (la / "info.json").write_text(json.dumps({"personal": {"path": str(db)}}), encoding="utf-8")
    monkeypatch.setenv("OneDrive", str(od))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    found = {(f["service"], Path(f["path"]).name) for f in storage_conn.detect_mounts()}
    assert ("OneDrive", "OneDrive") in found and ("Dropbox", "Dropbox (Personal)") in found


# ------------------------------------------------------------ feeds/weather
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Daily</title>
<item><title>Rates cut &amp; markets jump</title><link>https://n.example/1</link><pubDate>Fri, 23 Oct 2026 08:00:00 GMT</pubDate>
<description>&lt;p&gt;Central bank moves&lt;/p&gt;</description></item>
<item><title>Old story</title><link>https://n.example/0</link><pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>New model released</title><link href="https://b.example/x"/><updated>2026-10-24T10:00:00Z</updated><summary>Big news</summary></entry></feed>"""


def test_feeds(tmp_path, monkeypatch):
    def fake_get(url, **k):
        return httpx.Response(200, content=RSS if "rss" in url else ATOM, request=httpx.Request("GET", url))
    monkeypatch.setattr(extras.httpx, "get", fake_get)
    m = mgr(tmp_path)
    m.add("feeds", {"feeds": {"Daily": "https://n.example/rss", "Blog": "https://b.example/atom"}})
    items, errs = m.pick("feeds").latest()
    assert [i["title"] for i in items] == ["New model released", "Rates cut & markets jump", "Old story"] and not errs
    assert items[1]["summary"] == "Central bank moves"

    class RT:
        connections = m
    out = {t.name: t for t in registry_tools(RT())}["news_latest"].func(RT(), query="model")
    assert "New model released" in out and "Rates" not in out
    from localagent import guard
    assert guard.is_untrusted(RT(), "news_latest", {}) == "news feeds"


def test_weather(tmp_path, monkeypatch):
    def fake_get(url, params=None, **k):
        if "geocoding" in url:
            data = {"results": [{"name": "Madison", "admin1": "Mississippi", "country": "United States", "latitude": 32, "longitude": -90},
                                {"name": "Madison", "admin1": "Wisconsin", "country": "United States", "latitude": 43.07, "longitude": -89.4}]}
        else:
            assert params["latitude"] == 43.07
            data = {"current": {"temperature_2m": 51, "apparent_temperature": 48, "weather_code": 3, "wind_speed_10m": 9},
                    "daily": {"time": ["2026-10-24"], "weather_code": [61], "temperature_2m_min": [40], "temperature_2m_max": [55],
                              "precipitation_probability_max": [70]}}
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))
    monkeypatch.setattr(extras.httpx, "get", fake_get)
    m = mgr(tmp_path)
    r = m.add("weather", {"place": "Madison, Wisconsin"})
    assert "Wisconsin" in r["message"] and "51°F" in r["message"]
    assert "70% chance" in m.pick("weather").forecast(days=1)


# ----------------------------------------------------------------- registry
def test_registry_search_parses_remotes_and_packages():
    from localagent import mcp_registry

    def handler(req):
        assert req.url.params["search"] == "spotify"
        return httpx.Response(200, json={"servers": [
            {"server": {"name": "io.github.x/spotify-mcp", "description": "Control Spotify", "packages": [
                {"registryType": "npm", "identifier": "spotify-mcp", "transport": {"type": "stdio"},
                 "environmentVariables": [{"name": "SPOTIFY_CLIENT_ID", "isRequired": True}, {"name": "SPOTIFY_SECRET", "isSecret": True}]}]},
             "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}}},
            {"server": {"name": "com.example/music", "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com/mcp"}]}},
            {"server": {"name": "io.github.x/spotify-mcp", "description": "old"},
             "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": False}}},
            {"server": {"name": "no.way/to-run"}}]})
    res = mcp_registry.search("spotify", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [r["name"] for r in res] == ["io.github.x/spotify-mcp", "com.example/music"]
    assert res[0]["packages"][0]["command"] == ["npx", "-y", "spotify-mcp"]
    assert res[0]["packages"][0]["env"][1] == {"name": "SPOTIFY_SECRET", "secret": True, "required": False, "description": ""}
    assert res[1]["remotes"][0]["url"] == "https://mcp.example.com/mcp" and res[1]["title"] == "Music"


# ---------------------------------------------------------------- SSE MCP
class SSEHandler(BaseHTTPRequestHandler):
    queues: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good":
            self.send_response(401); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        q = []
        SSEHandler.queues["s1"] = q
        self.wfile.write(b"event: endpoint\ndata: /messages?session=s1\n\n")
        self.wfile.flush()
        for _ in range(200):
            while q:
                self.wfile.write(b"event: message\ndata: " + json.dumps(q.pop(0)).encode() + b"\n\n")
                self.wfile.flush()
            time.sleep(0.02)

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(202); self.end_headers()
        q = SSEHandler.queues["s1"]
        if msg.get("method") == "initialize":
            q.append({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}}})
        elif msg.get("method") == "tools/list":
            q.append({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "list_tasks", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "delete_task", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}]}})
        elif msg.get("method") == "tools/call":
            q.append({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": f"ran {msg['params']['name']}"}]}})


def test_sse_transport_and_always_ask_for_deletes(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SSEHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from localagent.mcp import MCPManager
        cfg = Cfg(tmp_path)
        cfg.data["mcp_servers"] = {"tasks": {"url": f"http://127.0.0.1:{srv.server_port}/sse", "token_in_keychain": True}}
        mm = MCPManager(cfg)
        mm.store = Secrets(tmp_path / "home", use_keychain=False)
        mm.servers["tasks"].store = mm.store
        mm.store.set("mcp:tasks:token", "good")
        s = mm.servers["tasks"]
        s.start()
        assert [t["name"] for t in s.tools] == ["list_tasks", "delete_task"]
        assert s.call("list_tasks", {}) == "ran list_tasks"

        from localagent import guard

        class RT:
            mcp = mm
            asked = []
            def request_approval(self, q):
                self.asked.append(q)
                return False
        rt = RT()
        guard.check(rt, None, "tasks__list_tasks", {})                      # reading: no question
        with pytest.raises(PermissionError):
            guard.check(rt, None, "tasks__delete_task", {"id": "7"})       # deleting: always asks, even untainted
        assert "can't be undone" in rt.asked[0] and '"7"' in rt.asked[0]
        s.close()
    finally:
        srv.shutdown()


def test_sync_folder_doesnt_claim_the_disks_space(tmp_path):
    m = mgr(tmp_path)
    od = tmp_path / "OneDrive"
    od.mkdir()
    r = m.add("storage", {"mode": "folder", "path": str(od), "service": "OneDrive"})
    assert "synced folder" in r["message"] and "used of" not in r["message"]

    class RT:
        connections = m
    out = {t.name: t for t in registry_tools(RT())}["storage_overview"].func(RT())
    assert "cloud space isn't visible" in out
