"""The LocalAgent app server. Serves the GUI (web/index.html) and its API.
Used by the desktop app (`localagent app`) and the browser UI (`localagent web`).

Binds to localhost only by default. The agent is autonomous on this machine,
so never expose this port to a network you don't control."""
from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import shutil
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import setup_service
from .agent import Agent
from .bridges import BRIDGES
from .config import load_config
from .llm import Router
from .runtime import Runtime
from .tools.desktop import SHOT_DIR


class ChatIn(BaseModel):
    session: str
    message: str
    mode: str = "auto"
    files: list[str] = []
    brain: str = "auto"


class UploadIn(BaseModel):
    name: str
    data: str  # base64 / data URL


class IdIn(BaseModel):
    id: str


class ApproveIn(BaseModel):
    session: str
    id: str
    allow: bool


class SessionIn(BaseModel):
    session: str


class Session:
    def __init__(self, state: "AppState", mode: str):
        self.state = state
        self.rt = state.make_runtime()
        self.agent = Agent(self.rt, mode=mode)
        self.lock = threading.Lock()
        self.queue: queue.Queue | None = None
        self.pending: dict[str, dict] = {}
        self.rt.approver = self._approve

    def _approve(self, question: str) -> bool:
        q = self.queue
        if q is None:
            return False
        aid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "allow": False}
        self.pending[aid] = slot
        q.put({"type": "approval", "id": aid, "text": question})
        self.state.fire("approval", {"text": question})
        timeout = float((self.rt.cfg.data.get("desktop") or {}).get("approval_timeout", 300))
        answered = slot["event"].wait(timeout)
        self.pending.pop(aid, None)
        if not self.rt.cancel.is_set():
            q.put({"type": "approval_done", "id": aid, "allow": answered and slot["allow"]})
        return answered and slot["allow"]


class AppState:
    def __init__(self, config: str | None = None, background: bool = True):
        from .automations import DesktopChannel, Inbox, Owner
        self.config_path = config
        self.sessions: dict[str, Session] = {}
        self.hooks: list[Callable[[str, dict], None]] = []  # desktop app listens here
        self.scheduler = None
        self.telegram = None
        self.mcp = None
        self.background = background
        self.reload()
        from .connections import ConnectionManager
        self.connections = ConnectionManager(self.cfg, save_patch=self._save_patch)
        self.inbox = Inbox(self.cfg.home)
        self.owner = Owner(self.inbox)
        self.owner.channels.append(DesktopChannel())
        self.inbox.listeners.append(lambda item: self.fire("inbox", item))
        from .automations import Scheduler
        from .telegram import Telegram
        self.scheduler = Scheduler(self.cfg, self.make_runtime, self.owner)
        self.telegram = Telegram(self.cfg, self.owner, self.make_runtime, self._save_patch)
        from .tasks import TaskManager
        self.tasks = TaskManager(self.cfg, self.make_runtime, self.owner,
                                 on_change=lambda t: self.fire("task", t))
        from .proactive import Proactive
        self.proactive = Proactive(self.cfg, self.router, self.tasks, self.scheduler, self.owner,
                                   memory_recent=self._memory_recent, connections=self.connections)
        if background:
            self.proactive.start()
            self.scheduler.start()
            self.telegram.start()
            threading.Thread(target=self.warm_up, daemon=True, name="la-warmup").start()

    def warm_up(self) -> None:
        """At launch, get slow things out of the way before the first message:
        status checks (hardware, sign-ins), fitting the local model's context to
        the GPU, and loading the model into memory (~14 s cold on a 3060)."""
        self.warm_state = "warming"
        try:
            setup_service.status(self.cfg)
        except Exception:
            pass
        try:
            main = self.router.main
            if hasattr(main, "warm"):
                rt = self.make_runtime()
                ctx = rt.ctx_for(main)          # measures once, then remembered in fit.json
                main.warm(ctx)
        except Exception:
            pass
        self.warm_state = "ready"

    def _memory_recent(self) -> list[str]:
        try:
            from .memory_store import MemoryStore
            return [m["text"] for m in MemoryStore(self.cfg.home / "memory.db").recent(15)]
        except Exception:
            return []

    def _save_patch(self, patch: dict) -> None:
        data = setup_service.read_user_config()
        data.update(patch)
        setup_service.write_user_config(data)
        self.cfg = load_config(Path(self.config_path) if self.config_path else None)
        if self.telegram:
            self.telegram.cfg = self.cfg
        if getattr(self, "proactive", None) is not None:
            self.proactive.cfg = self.cfg
        if getattr(self, "connections", None) is not None:
            self.connections.cfg = self.cfg

    def make_runtime(self) -> Runtime:
        rt = Runtime(self.cfg, router=self.router, mcp=self.mcp)
        rt.scheduler = self.scheduler
        rt.telegram = self.telegram
        rt.owner = getattr(self, "owner", None)
        if getattr(self, "connections", None) is not None:
            rt.connections = self.connections
        return rt

    def reload(self) -> None:
        self.cfg = load_config(Path(self.config_path) if self.config_path else None)
        self.router = Router(self.cfg.models, self.cfg.providers, self.cfg.data.get("brains"),
                             routes=self.cfg.data.get("routing"),
                             cooldown_minutes=float(self.cfg.data.get("brain_rest_minutes", 60)))
        from .mcp import MCPManager
        if self.mcp is not None:
            self.mcp.close()
        self.mcp = MCPManager(self.cfg)
        for s in self.sessions.values():
            s.rt.shutdown()
        self.sessions.clear()
        if self.scheduler is not None:
            self.scheduler.cfg = self.cfg
        if self.telegram is not None:
            self.telegram.cfg = self.cfg
        if getattr(self, "proactive", None) is not None:
            self.proactive.cfg, self.proactive.router = self.cfg, self.router
        if getattr(self, "connections", None) is not None:
            self.connections.cfg = self.cfg

    def session(self, sid: str, mode: str) -> Session:
        if sid not in self.sessions:
            self.sessions[sid] = Session(self, mode)
        return self.sessions[sid]

    def fire(self, kind: str, data: dict) -> None:
        for h in list(self.hooks):
            try:
                h(kind, data)
            except Exception:
                pass


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#17203A"/>
<circle cx="256" cy="230" r="96" fill="none" stroke="#F5D046" stroke-width="36"/><rect x="160" y="356" width="192" height="36" rx="18" fill="#2D4BD6"/></svg>"""

OVERLAY = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:15px/1.3 "Atkinson Hyperlegible",system-ui,sans-serif;background:#17203A;color:#fff;display:flex;
align-items:center;gap:12px;padding:0 14px;height:100vh;box-sizing:border-box;-webkit-user-select:none;user-select:none}
i{width:10px;height:10px;border-radius:50%;background:#F5D046;flex:none}
span{flex:1}button{font:inherit;font-weight:700;background:#C2412D;color:#fff;border:0;border-radius:6px;padding:8px 16px;cursor:pointer}
</style></head><body><i></i><span>LocalAgent is using your computer</span>
<button onclick="fetch('/api/stop_all',{method:'POST'});this.textContent='Stopping…'">Stop</button></body></html>"""


def _sse(gen):
    def stream():
        for ev in gen:
            yield f"data: {json.dumps(ev, default=str)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>LocalAgent</title>
<body style="font:16px system-ui;max-width:34em;margin:15vh auto;color:#17203A">
<h1>Open LocalAgent from the app</h1><p>This page needs the private link LocalAgent printed when it started
(it ends in <code>?t=…</code>). That keeps other websites from sending commands to your agent.</p></body>"""

COOKIE = "la_token"


def create_app(config: str | None = None, token: str | None = None, extra_hosts: tuple[str, ...] = (),
               background: bool = True) -> FastAPI:
    """token: per-launch secret. Every API call must carry it (SameSite=Strict
    cookie set when the page is opened with ?t=TOKEN, or an X-LocalAgent-Token
    header). Requests whose Host isn't localhost, or whose Origin is another
    site, are refused — blocks cross-site requests and DNS rebinding."""
    state = AppState(config, background=background)
    app = FastAPI(title="LocalAgent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.la = state
    app.state.token = token = token or secrets.token_urlsafe(32)
    page = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]", "testserver", *extra_hosts}
    app.state.allowed_hosts = allowed_hosts
    PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/oauth/callback", "/oauth/google"}  # the callback is checked by its one-time state  # harmless static files the phone fetches without cookies

    def _remote() -> dict:
        return state.cfg.data.get("remote") or {}

    def _phone_token() -> str:
        r = _remote()
        return r.get("token", "") if r.get("enabled") else ""

    def _ok(value: str | None) -> bool:
        if not value:
            return False
        if hmac.compare_digest(value.encode(), token.encode()):
            return True
        pt = _phone_token()
        return bool(pt) and hmac.compare_digest(value.encode(), pt.encode())

    if _remote().get("enabled") and _remote().get("host"):
        allowed_hosts.add(_remote()["host"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0] if not (request.headers.get("host") or "").startswith("[") \
            else (request.headers.get("host") or "").split("]")[0] + "]"
        if host not in allowed_hosts:
            return JSONResponse({"detail": "wrong host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != "null":
            o = origin.split("://", 1)[-1].rsplit(":", 1)[0]
            if o not in allowed_hosts:
                return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
        path = request.url.path
        if path in PUBLIC:
            return await call_next(request)
        if path in ("/", "/overlay"):
            given = request.query_params.get("t")
            if _ok(given):
                resp = await call_next(request)
                if given == token:
                    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict")
                else:  # phone pairing link: stay signed in on that phone
                    resp.set_cookie(COOKIE, given, httponly=True, samesite="strict", secure=True, max_age=400 * 86400)
                return resp
            if not _ok(request.cookies.get(COOKIE)):
                return HTMLResponse(LOCKED_PAGE, status_code=403)
        elif not (_ok(request.cookies.get(COOKIE)) or _ok(request.headers.get("x-localagent-token"))):
            return JSONResponse({"detail": "not authorized"}, status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({"name": "LocalAgent", "short_name": "LocalAgent", "start_url": "/?view=foryou",
                             "display": "standalone", "background_color": "#F3F5F9", "theme_color": "#17203A",
                             "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]},
                            media_type="application/manifest+json")

    @app.get("/icon.svg")
    def icon():
        from fastapi.responses import Response
        return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------ status
    @app.get("/api/status")
    def get_status():
        st = setup_service.status(state.cfg)
        st["main_label"] = state.router.main.label
        chain = state.router.brain_chain()
        st["brain_chain"] = [p.label for p in chain]
        from .voice import available as voice_ok
        st["voice"] = voice_ok()
        st["unread"] = state.inbox.unread()
        st["approvals_waiting"] = len(state.owner.pending)
        st["phone"] = state.telegram.info()
        st["autostart"] = setup_service.autostart_enabled()
        st["brains_live"] = state.router.status()
        st["for_you"] = len([c for c in state.proactive.cards() if c["status"] == "new"])
        st["tasks_active"] = sum(1 for t in state.tasks.list() if t["status"] in ("queued", "running", "waiting"))
        return st

    # ------------------------------------------------------------- chat
    @app.post("/api/chat")
    def chat(body: ChatIn):
        s = state.session(body.session, body.mode)
        if not s.lock.acquire(blocking=False):
            raise HTTPException(409, "Still working on the last request. Press Stop to cancel it.")
        if s.agent.mode != body.mode:
            s.agent.set_mode(body.mode)
        if getattr(s.agent, "brain", "auto") != body.brain:
            s.agent.set_brain(body.brain)
        q: queue.Queue = queue.Queue()
        s.queue = q

        def on_event(kind, data):
            q.put({"type": kind, **data})
            state.fire(kind, data)
        s.rt.on_event = on_event

        def work():
            try:
                text = body.message
                files = [p for p in body.files if Path(p).exists()]
                images = [p for p in files if Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}]
                docs = [p for p in files if p not in images]
                if docs:
                    text += "\n\nAttached files: " + ", ".join(docs)
                s.agent.run(text, images=images or None)
            except Exception as e:
                q.put({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                s.queue = None
                q.put({"type": "done"})
                state.fire("done", {})
                s.lock.release()

        threading.Thread(target=work, daemon=True).start()

        def gen():
            while True:
                ev = q.get()
                yield ev
                if ev["type"] == "done":
                    return
        return _sse(gen())

    @app.post("/api/stop")
    def stop(body: SessionIn):
        s = state.sessions.get(body.session)
        if s:
            s.rt.cancel.set()
            for slot in list(s.pending.values()):
                slot["allow"] = False
                slot["event"].set()
        return {"ok": True}

    @app.post("/api/stop_all")
    def stop_all():
        for s in state.sessions.values():
            s.rt.cancel.set()
            for slot in list(s.pending.values()):
                slot["allow"] = False
                slot["event"].set()
        return {"ok": True}

    @app.get("/overlay", response_class=HTMLResponse)
    def overlay():
        return OVERLAY

    @app.post("/api/approve")
    def approve(body: ApproveIn):
        s = state.sessions.get(body.session)
        slot = s.pending.get(body.id) if s else None
        if not slot:
            raise HTTPException(404, "That request already timed out.")
        slot["allow"] = body.allow
        slot["event"].set()
        return {"ok": True}

    @app.post("/api/new")
    def new(body: SessionIn):
        s = state.sessions.pop(body.session, None)
        if s:
            s.rt.cancel.set()
            s.rt.shutdown()
        return {"ok": True}

    @app.post("/api/upload")
    def upload(body: UploadIn):
        up = state.cfg.workspace / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\-]", "_", Path(body.name).name) or "upload.bin"
        dest, i = up / name, 1
        while dest.exists():
            dest = up / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        dest.write_bytes(base64.b64decode(body.data.split(",", 1)[-1]))
        return {"path": str(dest), "name": dest.name}

    @app.get("/api/screen")
    def screen(path: str):
        p = Path(path).resolve()
        if SHOT_DIR.resolve() not in p.parents or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ setup
    @app.post("/api/ollama/install")
    def ollama_install():
        return {"message": setup_service.install_ollama()}

    @app.post("/api/ollama/start")
    def ollama_start():
        try:
            return {"message": setup_service.start_ollama()}
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/models/download")
    def download(body: dict):
        profile = body.get("profile") or state.cfg["profile"]

        def gen():
            try:
                yield from setup_service.download_profile(state.cfg, profile)
            except Exception as e:
                yield {"error": f"Couldn't download models: {e}. Is Ollama running?"}
            state.reload()
        return _sse(gen())

    @app.post("/api/settings")
    def settings(body: dict):
        setup_service.save_settings(body)
        state.reload()
        return {"ok": True}

    # ----------------------------------------------------------- brains
    @app.post("/api/brains/{key}/{action}")
    def brain_action(key: str, action: str):
        cls = BRIDGES.get(key)
        if not cls or action not in ("install", "login", "check"):
            raise HTTPException(404)
        b = cls()
        try:
            if action == "install":
                msg = b.install()
            elif action == "login":
                msg = b.login()
            else:
                msg = "checked"
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        state.router.brain_chain(refresh=True)
        setup_service.refresh_bridges()
        st = b.status()
        st["message"] = msg
        return st

    # ------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    def tasks_list():
        return {"tasks": state.tasks.list()[:80], "brains": state.router.status()}

    @app.get("/api/tasks/{tid}")
    def task_get(tid: str):
        t = state.tasks.get(tid)
        if not t:
            raise HTTPException(404, "No such task")
        return t

    @app.post("/api/tasks")
    def task_create(body: dict):
        try:
            return state.tasks.create(str(body.get("message", "")), brain=str(body.get("brain", "auto")),
                                      mode=str(body.get("mode", "auto")), files=list(body.get("files") or []),
                                      source=str(body.get("source", "app")))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/tasks/{tid}/reply")
    def task_reply(tid: str, body: dict):
        try:
            return state.tasks.reply(tid, str(body.get("message", "")))
        except KeyError:
            raise HTTPException(404, "No such task")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/tasks/{tid}/stop")
    def task_stop(tid: str):
        if not state.tasks.stop(tid):
            raise HTTPException(404, "No such task")
        return {"ok": True}

    # ------------------------------------------------------- connections
    @app.get("/api/connections")
    def connections_list():
        from .connections.catalog import CATALOG, GROUP_ORDER
        from .connections.email_conn import PRESETS
        from .connections.calendar_conn import HELP
        from .mcp_oauth import load_tokens
        mgr = state.connections
        hosted = []
        status = {s["name"]: s for s in state.mcp.status()} if state.mcp else {}
        for name, spec in (state.cfg.data.get("mcp_servers") or {}).items():
            if isinstance(spec, dict) and (spec.get("catalog") or spec.get("registry")):
                st = status.get(name, {})
                hosted.append({"name": name, "catalog": spec.get("catalog", ""), "title": spec.get("title", name),
                               "enabled": spec.get("enabled", True),
                               "signed_in": bool(load_tokens(state.mcp.store, name)) if spec.get("oauth") else True,
                               "tools": st.get("tools", 0), "error": st.get("error", "")})
        try:
            detected = mgr.detected()
        except Exception:
            detected = []
        from .connections.storage_conn import rclone_exe
        from .connections import google_conn
        google = {"ready": bool(google_conn.client_config(state.cfg)),
                  "services": {k: {"name": v["name"], "why": v["why"]} for k, v in google_conn.SERVICES.items()},
                  "accounts": [{"id": a.id, "email": a.address, "services": a.services} for a in mgr.google_accounts()]}
        return {"google": google, "catalog": CATALOG, "groups": GROUP_ORDER, "items": mgr.status(), "detected": detected, "hosted": hosted,
                "rclone": bool(rclone_exe(state.cfg.home)), "tools_present": {"uvx": bool(shutil.which("uvx")), "npx": bool(shutil.which("npx"))},
                "presets": {k: {"name": v["name"], "steps": v["steps"], "help": v["password_help"]} for k, v in PRESETS.items()},
                "calendar_help": HELP, "port": int(getattr(app.state, "port", 0) or 0)}

    @app.post("/api/connections/add")
    def connections_add(body: dict):
        t = str(body.get("type", ""))
        mgr = state.connections
        try:
            if t == "email":
                from .connections.email_conn import guess_preset
                addr = str(body.get("address", "")).strip()
                preset = body.get("preset") or guess_preset(addr)
                if preset == "microsoft":
                    raise HTTPException(400, "Outlook.com and Hotmail need Microsoft's own sign-in now. "
                                             "Connect Zapier (under Everything else) and add Outlook there.")
                spec = {"address": addr, "preset": preset, "imap": body.get("imap"), "smtp": body.get("smtp"),
                        "smtp_port": body.get("smtp_port"), "display_name": body.get("display_name")}
                secret = {"password": str(body.get("password", "")).replace(" ", "") if preset == "gmail" else body.get("password")}
            elif t == "calendar":
                spec = {"provider": body.get("provider", "google"), "name": body.get("name") or ""}
                secret = {"url": str(body.get("url", "")).strip()}
                if not secret["url"].lower().startswith(("http", "webcal")):
                    raise HTTPException(400, "Paste the calendar's iCal link (it starts with https:// or webcal://).")
            elif t == "notes":
                spec = {"app": body.get("app", "folder"), "path": str(body.get("path", "")).strip(), "name": body.get("name")}
                secret = {}
            elif t == "storage":
                if body.get("provider") in (None, "", "folder"):
                    path = str(body.get("path", "")).strip()
                    if not path or not Path(path).is_dir():
                        raise HTTPException(400, "Pick a folder or drive that exists on this computer, like I:\\ or C:\\Users\\you\\Dropbox.")
                    spec = {"mode": "folder", "path": path, "service": body.get("service") or Path(path).name or path,
                            "name": body.get("name")}
                    secret = {}
                else:
                    spec = {"mode": "rclone", "provider": body["provider"], "name": body.get("name")}
                    secret = {"user": body.get("user"), "password": body.get("password"), "code": body.get("code")}
            elif t == "feeds":
                feeds = body.get("feeds") or {}
                if isinstance(feeds, str):
                    feeds = {u.strip(): u.strip() for u in feeds.splitlines() if u.strip()}
                if not feeds:
                    from .connections.extras import STARTER_FEEDS
                    feeds = dict(STARTER_FEEDS)
                existing = state.connections.of_kind("feeds")
                if existing:   # one feeds connection; adding more feeds extends it
                    cid = existing[0].id
                    specs = state.connections.specs()
                    specs[cid]["feeds"] = {**specs[cid].get("feeds", {}), **feeds}
                    state.connections._write(specs)
                    return {"id": cid, "name": specs[cid].get("name"), "message": state.connections.test(cid)}
                spec, secret = {"feeds": feeds, "name": "News & feeds"}, {}
            elif t == "passwords":
                prov = str(body.get("provider", ""))
                if prov not in ("1password", "bitwarden"):
                    raise HTTPException(400, "unknown password manager")
                spec = {"provider": prov}
                if prov == "bitwarden":
                    spec.update({"server": str(body.get("server") or "").strip(), "bw_dir": str(state.cfg.home / "bitwarden")})
                secret = {"client_id": body.get("client_id"), "client_secret": body.get("client_secret")}
            elif t == "weather":
                spec = {"place": str(body.get("place", "")).strip(), "units": body.get("units") or "fahrenheit"}
                secret = {}
            else:
                raise HTTPException(400, "unknown type")
            r = mgr.add(t, spec, secret)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e))
        threading.Thread(target=state.proactive.tick, daemon=True).start()   # look for things to do with it
        return r

    @app.post("/api/connections/{cid}/{action}")
    def connections_action(cid: str, action: str):
        mgr = state.connections
        try:
            if action == "test":
                return {"message": mgr.test(cid)}
            if action == "remove":
                mgr.remove(cid)
                return {"ok": True}
            if action in ("on", "off"):
                mgr.set_enabled(cid, action == "on")
                return {"ok": True}
        except KeyError:
            raise HTTPException(404, "No such connection")
        except Exception as e:
            raise HTTPException(400, str(e))
        raise HTTPException(404)

    @app.post("/api/signin/{eid}")
    def signin_start(eid: str, request: Request):
        from .connections.catalog import entry
        from . import mcp_oauth
        e = entry(eid)
        custom = (state.cfg.data.get("mcp_servers") or {}).get(eid)
        if (not e or e["how"] != "signin") and isinstance(custom, dict) and custom.get("url"):
            e = {"id": eid, "name": custom.get("title") or eid, "url": custom["url"]}   # a connector added by address
        elif not e or e["how"] != "signin":
            raise HTTPException(404, "Unknown service")
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(400, f"Signing in to {e['name']} has to be done on the computer running LocalAgent "
                                     "(the service sends you back to it). Open LocalAgent there and press Sign in.")
        port = int(getattr(app.state, "port", 0) or request.url.port or 7860)
        try:
            url = mcp_oauth.start(eid, e["url"], f"http://127.0.0.1:{port}/oauth/callback", state.mcp.store)
        except Exception as ex:
            raise HTTPException(400, f"Couldn't start signing in to {e['name']}: {ex}")
        import webbrowser   # the person's normal browser, where they're already signed in to the service
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return {"url": url}

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def signin_finish(request: Request):
        from . import mcp_oauth
        from .connections.catalog import entry
        st, code, error = (request.query_params.get(k, "") for k in ("state", "code", "error"))
        page = lambda title, msg: f"""<!doctype html><meta charset="utf-8"><title>LocalAgent</title>
<body style="font:17px system-ui;max-width:32em;margin:18vh auto;color:#17203A;text-align:center">
<h1 style="font-size:28px">{title}</h1><p>{msg}</p></body>"""
        if error:
            mcp_oauth.PENDING.pop(st, None)
            return HTMLResponse(page("Not connected", "The sign-in was cancelled. You can try again from LocalAgent."), status_code=400)
        try:
            name = mcp_oauth.finish(st, code)
        except Exception as ex:
            return HTMLResponse(page("Not connected", str(ex)), status_code=400)
        e = entry(name) or {"url": "", "name": name}
        servers = dict(state.cfg.data.get("mcp_servers") or {})
        if isinstance(servers.get(name), dict) and servers[name].get("url"):
            servers[name] = {**servers[name], "oauth": True, "enabled": True}
            e = {"name": servers[name].get("title") or name}
        else:
            servers[name] = {"url": e["url"], "oauth": True, "enabled": True, "catalog": name}
        state._save_patch({"mcp_servers": servers})
        state.reload()
        threading.Thread(target=state.proactive.tick, daemon=True).start()
        return HTMLResponse(page(f"{e['name']} is connected", "You can close this tab and go back to LocalAgent."))

    def _add_server(name: str, spec: dict) -> dict:
        servers = dict(state.cfg.data.get("mcp_servers") or {})
        servers[name] = spec
        state._save_patch({"mcp_servers": servers})
        state.reload()
        s = state.mcp.servers.get(name)
        try:
            s.start()
            return {"ok": True, "tools": len(s.tools)}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    @app.post("/api/connect/{eid}")
    def connect_simple(eid: str, body: dict):
        """Catalog entries that need no browser sign-in: open servers and token servers."""
        from .connections.catalog import entry
        e = entry(eid)
        if not e or e["how"] not in ("open", "token"):
            raise HTTPException(404, "Unknown service")
        url = e["url"]
        spec = {"url": "", "enabled": True, "catalog": eid}
        if e["how"] == "token":
            tok = str(body.get("token", "")).strip()
            if not tok:
                raise HTTPException(400, "Paste the access token.")
            for f in e.get("fields", []):
                v = str(body.get(f["key"], "")).strip().rstrip("/")
                if not v:
                    raise HTTPException(400, f"Fill in {f['label']}.")
                url = url.replace("{" + f["key"] + "}", v)
            state.mcp.store.set(f"mcp:{eid}:token", tok)
            spec["token_in_keychain"] = True
        spec["url"] = url
        r = _add_server(eid, spec)
        if not r["ok"]:
            servers = dict(state.cfg.data.get("mcp_servers") or {})
            servers.pop(eid, None)
            state.mcp.store.forget(f"mcp:{eid}:token")
            state._save_patch({"mcp_servers": servers})
            state.reload()
            raise HTTPException(400, f"Couldn't connect to {e['name']}: {r['error']}")
        return {"message": f"{e['name']} is connected ({r['tools']} actions)."}

    # ---- websites: sign in once in LocalAgent's browser, then the agent uses the site like a person
    def _browser(action: str, url: str = "") -> str:
        from .tools.browser import run
        out = run(state.make_runtime(), action, url, timeout=90)
        if out.startswith("ERROR"):
            raise HTTPException(400, out[7:].strip())
        return out

    @app.post("/api/web/connect")
    def web_connect(body: dict):
        from .connections.catalog import entry
        e = entry(str(body.get("catalog", ""))) or {}
        url = str(body.get("url") or e.get("url") or "").strip()
        if not url:
            raise HTTPException(400, "Type the website's address.")
        if "://" not in url:
            url = "https://" + url
        from urllib.parse import urlparse
        name = str(body.get("name") or e.get("name") or urlparse(url).hostname or "Website").strip()
        r = state.connections.add("web", {"url": url, "name": name, "tips": e.get("tips", ""), "catalog": e.get("id", ""),
                                          "signed_in": False}, test=False)
        try:
            _browser("show", url)
        except HTTPException:
            state.connections.remove(r["id"])
            raise
        return {"id": r["id"], "message": f"{name} is open in LocalAgent's browser. Sign in there, then press “I'm signed in”."}

    @app.post("/api/passwords/{cid}/{action}")
    def passwords_action(cid: str, action: str, body: dict):
        try:
            c = state.connections.get(cid)
        except KeyError:
            raise HTTPException(404, "No such password manager")
        if getattr(c, "kind", "") != "passwords":
            raise HTTPException(404)
        try:
            if action == "unlock":
                msg = c.bw_unlock(str(body.get("master_password", "")))
                body.clear()
                return {"message": msg}
            if action == "lock":
                c.lock()
                return {"message": "Locked."}
        except Exception as e:
            raise HTTPException(400, str(e))
        raise HTTPException(404)

    @app.post("/api/browser/open")
    def browser_open(body: dict):
        url = str(body.get("url", ""))
        if not url.startswith("https://"):
            raise HTTPException(400, "bad address")
        return {"message": _browser("show", url)}

    @app.post("/api/web/{cid}/{action}")
    def web_action(cid: str, action: str):
        from .connections.web_conn import looks_like_login
        try:
            spec = state.connections.specs()[cid]
        except KeyError:
            raise HTTPException(404, "No such website")
        if action == "show":
            return {"message": _browser("show", spec["url"])}
        if action == "check":
            out = _browser("goto", spec["url"])
            first = out.splitlines()[0]
            url = first.removeprefix("opened ").split(" — title: ")[0]
            title = first.split(" — title: ", 1)[-1]
            if looks_like_login(url, title):
                raise HTTPException(400, f"{spec.get('name')} still shows a sign-in page. Finish signing in in LocalAgent's browser window, then try again.")
            specs = state.connections.specs()
            specs[cid]["signed_in"] = True
            state.connections._write(specs)
            return {"message": f"{spec.get('name')} is connected. LocalAgent will use the website when you ask for anything there."}
        raise HTTPException(404)

    # ---- Sign in with Google (LocalAgent's own Google app; users never touch Google Cloud)
    def _page(title: str, msg: str, ok: bool = True) -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><title>LocalAgent</title>
<body style="font:17px system-ui;max-width:34em;margin:18vh auto;color:#17203A;text-align:center">
<h1 style="font-size:28px">{title}</h1><p>{msg}</p></body>""", status_code=200 if ok else 400)

    @app.post("/api/google/connect")
    def google_connect(body: dict, request: Request):
        from .connections import google_conn
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            raise HTTPException(400, "Signing in with Google has to be done on the computer running LocalAgent "
                                     "(Google sends you back to it). Open LocalAgent there and press Sign in with Google.")
        port = int(getattr(app.state, "port", 0) or request.url.port or 7860)
        try:
            url = google_conn.start_signin(state.cfg, f"http://127.0.0.1:{port}/oauth/google", list(body.get("services") or []),
                                           state.connections.secrets, login_hint=str(body.get("email", "")), cid=str(body.get("id", "")))
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return {"url": url}

    @app.get("/oauth/google", response_class=HTMLResponse)
    def google_callback(request: Request):
        from .connections import google_conn
        st, code, err = (request.query_params.get(k, "") for k in ("state", "code", "error"))
        if err:
            google_conn.PENDING.pop(st, None)
            return _page("Not connected", "You cancelled the Google sign-in. You can try again from LocalAgent any time.", ok=False)
        try:
            info = google_conn.finish_signin(st, code)
        except Exception as e:
            return _page("Not connected", str(e), ok=False)
        r = state.connections.save_google(info)
        threading.Thread(target=state.proactive.tick, daemon=True).start()
        names = ", ".join(google_conn.SERVICES[s]["name"] for s in r["services"]) or "nothing yet"
        skipped = ", ".join(google_conn.SERVICES[s]["name"] for s in info["declined"])
        return _page(f"Connected {info['email']}", f"LocalAgent can now use: {names}." +
                     (f" You didn't allow {skipped}; you can add it later." if skipped else "") + " You can close this tab.")

    @app.post("/api/google/client")
    def google_client(body: dict):
        """For whoever publishes LocalAgent (or runs their own copy): the Google app it signs in as."""
        cid, sec = str(body.get("client_id", "")).strip(), str(body.get("client_secret", "")).strip()
        if not cid.endswith(".apps.googleusercontent.com"):
            raise HTTPException(400, "That doesn't look like a Google OAuth client ID (it ends in .apps.googleusercontent.com).")
        state._save_patch({"google_oauth": {"client_id": cid, "client_secret": sec}})
        return {"message": "Saved. Sign in with Google is ready."}

    # ---- the full Google account via the Google Workspace connector (runs on this PC with uvx)
    GOOGLE_SERVICES = ["gmail", "drive", "calendar", "docs", "sheets", "forms", "tasks", "contacts"]

    @app.post("/api/google/setup")
    def google_setup(body: dict):
        cid, secret = str(body.get("client_id", "")).strip(), str(body.get("client_secret", "")).strip()
        email = str(body.get("email", "")).strip()
        if not cid.endswith(".apps.googleusercontent.com") or not secret:
            raise HTTPException(400, "Paste the Client ID (ends in .apps.googleusercontent.com) and the Client secret from Google Cloud.")
        if not shutil.which("uvx"):
            raise HTTPException(400, "This needs “uv”, a small Python tool installer. Install it from docs.astral.sh/uv, then try again.")
        services = [s for s in body.get("services") or GOOGLE_SERVICES if s in GOOGLE_SERVICES]
        state.mcp.store.set("mcp:google:client_secret", secret)
        cmd = ["uvx", "workspace-mcp", "--single-user", "--tools", *services]
        if body.get("read_only"):
            cmd.append("--read-only")
        spec = {"command": cmd, "enabled": True, "catalog": "google", "startup_timeout": 300, "call_timeout": 300,
                "env": {"GOOGLE_OAUTH_CLIENT_ID": cid, "USER_GOOGLE_EMAIL": email, "OAUTHLIB_INSECURE_TRANSPORT": "1",
                        "WORKSPACE_MCP_PORT": "8765", "GOOGLE_MCP_CREDENTIALS_DIR": str(state.cfg.home / "google")},
                "env_secrets": {"GOOGLE_OAUTH_CLIENT_SECRET": "mcp:google:client_secret"}}
        r = _add_server("google", spec)
        if not r["ok"]:
            raise HTTPException(400, f"The Google connector didn't start: {r['error']}")
        return {"message": f"Google connector ready ({r['tools']} actions). Now sign in.", "tools": r["tools"]}

    @app.post("/api/google/signin")
    def google_signin():
        s = state.mcp.servers.get("google")
        if not s:
            raise HTTPException(400, "Set up the Google connector first.")
        try:
            s.start()
        except Exception as ex:
            raise HTTPException(400, f"The Google connector didn't start: {ex}")
        email = (s.spec.get("env") or {}).get("USER_GOOGLE_EMAIL", "")
        names = [t["name"] for t in s.tools]
        tool = "start_google_auth" if "start_google_auth" in names else next((n for n in names if "calendar" in n and "list" in n), names[0] if names else None)
        if not tool:
            raise HTTPException(400, "The Google connector has no tools.")
        out = s.call(tool, {"service_name": "Google Workspace", "user_google_email": email} if tool == "start_google_auth"
                     else {"user_google_email": email})
        m = re.search(r"https://accounts\.google\.com/[^\s)\]>\"']+", out)
        if not m:
            return {"message": "Already signed in." if "error" not in out.lower() else out[:400]}
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(m.group(0),), daemon=True).start()
        return {"message": "Finish signing in to Google in your browser.", "url": m.group(0)}

    # ---- MCP Registry: find connectors for anything else
    @app.get("/api/registry")
    def registry_search(q: str = ""):
        from .mcp_registry import search
        if len(q.strip()) < 2:
            return {"results": []}
        try:
            return {"results": search(q.strip(), limit=12)}
        except Exception as ex:
            raise HTTPException(502, f"Couldn't reach the MCP Registry: {ex}")

    @app.post("/api/registry/add")
    def registry_add(body: dict):
        from .mcp import _safe
        name = _safe(str(body.get("name", "")).split("/")[-1])[:40] or "connector"
        title = str(body.get("title") or name)[:80]
        if body.get("url"):
            spec = {"url": body["url"], "enabled": True, "registry": body.get("registry", ""), "title": title}
            if body.get("type") == "sse":
                spec["transport"] = "sse"
            r = _add_server(name, spec)
            if r["ok"]:
                return {"name": name, "message": f"{title} is connected ({r['tools']} actions)."}
            if "401" in r["error"] or "signed in" in r["error"]:
                return {"name": name, "needs_signin": True, "message": f"{title} needs you to sign in."}
            raise HTTPException(400, f"Couldn't connect: {r['error']}")
        cmd = body.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise HTTPException(400, "Nothing to add.")
        if not shutil.which(cmd[0]):
            raise HTTPException(400, f"This connector needs “{cmd[0]}”, which isn't installed on this computer.")
        env, secrets_map = {}, {}
        for e in body.get("env") or []:
            v = str(e.get("value", "")).strip()
            if not v:
                continue
            if e.get("secret"):
                state.mcp.store.set(f"mcp:{name}:env:{e['name']}", v)
                secrets_map[e["name"]] = f"mcp:{name}:env:{e['name']}"
            else:
                env[e["name"]] = v
        spec = {"command": cmd, "env": env, "env_secrets": secrets_map, "enabled": True, "registry": body.get("registry", ""),
                "title": title, "startup_timeout": 300}
        r = _add_server(name, spec)
        if not r["ok"]:
            raise HTTPException(400, f"It was added but didn't start: {r['error']}")
        return {"name": name, "message": f"{title} is connected ({r['tools']} actions)."}

    @app.post("/api/hosted/{name}/remove")
    def hosted_remove(name: str):
        servers = dict(state.cfg.data.get("mcp_servers") or {})
        if name not in servers:
            raise HTTPException(404, "No such connector")
        servers.pop(name)
        for k in (f"mcp:{name}:oauth", f"mcp:{name}:token", f"mcp:{name}:client_secret"):
            state.mcp.store.forget(k)
        state._save_patch({"mcp_servers": servers})
        state.reload()
        return {"ok": True}

    # ----------------------------------------------------------- for you
    @app.get("/api/foryou")
    def foryou():
        return state.proactive.status()

    @app.post("/api/foryou/profile")
    def foryou_profile(body: dict):
        state.proactive.set_profile(str(body.get("about", "")), list(body.get("areas") or []))
        threading.Thread(target=lambda: (state.proactive.notice(), state.proactive.think(force=True)), daemon=True).start()
        return state.proactive.status()

    @app.post("/api/foryou/{cid}/{action}")
    def foryou_card(cid: str, action: str):
        p = state.proactive
        try:
            if action == "yes":
                return p.accept(cid)
            if action in ("no", "never"):
                return p.dismiss(cid, never=action == "never")
        except KeyError:
            raise HTTPException(404, "That suggestion is gone.")
        raise HTTPException(404)

    @app.post("/api/foryou/refresh")
    def foryou_refresh():
        p = state.proactive
        added = p.notice()
        if p.data["profile"].get("done"):
            added += p.think(force=True)
        return {"added": len(added), **p.status()}

    @app.post("/api/foryou/settings")
    def foryou_settings(body: dict):
        p = state.proactive
        for kind, on in (body.get("auto") or {}).items():
            p.set_auto(kind, bool(on))
        for kind in body.get("unblock") or []:
            p.unblock(kind)
        allowed = {"level", "brain", "brief_hour", "quiet_start", "quiet_end", "watch_folders", "max_alerts_per_day"}
        patch = {k: v for k, v in body.items() if k in allowed}
        if patch:
            cur = dict(state.cfg.data.get("proactive") or {})
            cur.update(patch)
            state._save_patch({"proactive": cur})
        return p.status()

    # ------------------------------------------------------------ phone
    @app.get("/api/remote")
    def remote_status():
        from . import remote
        r = _remote()
        ts = remote.info()
        port = int(getattr(app.state, "port", 0) or 0)
        url = f"https://{ts['dns']}/?t={r.get('token')}" if r.get("enabled") and ts["dns"] and r.get("token") else ""
        return {"tailscale": ts, "enabled": bool(r.get("enabled")), "port": port,
                "serving": bool(port and ts["running"] and remote.serving(port)),
                "url": url, "qr": remote.qr_svg(url) if url else ""}

    @app.post("/api/remote")
    def remote_set(body: dict):
        from . import remote
        port = int(getattr(app.state, "port", 0) or 0)
        if body.get("enabled"):
            ts = remote.info()
            if not ts["installed"]:
                raise HTTPException(400, "Install Tailscale on this computer and your phone first: tailscale.com/download")
            if not ts["running"] or not ts["dns"]:
                raise HTTPException(400, "Tailscale isn't connected on this computer. Open Tailscale and sign in.")
            if not port:
                raise HTTPException(400, "Restart LocalAgent from the app to use phone access.")
            try:
                remote.start_serving(port)
            except RuntimeError as e:
                raise HTTPException(400, str(e))
            r = dict(_remote())
            r.update({"enabled": True, "host": ts["dns"], "token": r.get("token") if r.get("token") and not body.get("new_link") else remote.new_token()})
            allowed_hosts.add(ts["dns"])
        else:
            try:
                remote.stop_serving()
            except Exception:
                pass
            r = dict(_remote())
            r["enabled"] = False
        state._save_patch({"remote": r})
        return remote_status()

    # ----------------------------------------------------- inbox/owner
    @app.get("/api/inbox")
    def inbox():
        return {"items": state.inbox.list()[:100],
                "approvals": [{"id": k, "question": v["question"], "time": v["time"]} for k, v in state.owner.pending.items()]}

    @app.post("/api/inbox/read")
    def inbox_read(body: dict):
        state.inbox.mark_read(body.get("ids"))
        return {"ok": True}

    @app.post("/api/owner/answer")
    def owner_answer(body: dict):
        if not state.owner.answer(str(body.get("id")), bool(body.get("allow"))):
            raise HTTPException(404, "That request already timed out.")
        return {"ok": True}

    # ------------------------------------------------------ automations
    @app.get("/api/automations")
    def automations():
        hb = state.scheduler.heartbeat_settings()
        hb["checklist"] = state.scheduler.heartbeat_file.read_text(encoding="utf-8") if state.scheduler.heartbeat_file.exists() else ""
        return {"items": state.scheduler.list(), "heartbeat": hb, "running": state.scheduler.running_id}

    @app.post("/api/automations")
    def automation_add(body: dict):
        from .automations import ScheduleError
        try:
            return state.scheduler.add(str(body.get("task", "")).strip(), str(body.get("when", "")), name=str(body.get("name", "")),
                                       notify="always" if body.get("notify", "always") == "always" else "important")
        except ScheduleError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/automations/{aid}")
    def automation_update(aid: str, body: dict):
        from .automations import ScheduleError
        try:
            return state.scheduler.update(aid, **{k: v for k, v in body.items() if k in ("enabled", "name", "task", "when", "archived")})
        except (KeyError, ScheduleError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/automations/{aid}/run")
    def automation_run(aid: str):
        threading.Thread(target=lambda: state.scheduler.run(aid), daemon=True).start()
        return {"ok": True}

    @app.post("/api/heartbeat")
    def heartbeat_save(body: dict):
        if "checklist" in body:
            state.scheduler.heartbeat_file.write_text(str(body["checklist"]), encoding="utf-8")
        hb = {k: body[k] for k in ("enabled", "minutes", "start_hour", "end_hour") if k in body}
        if hb:
            setup_service.update_user_config({"heartbeat": hb})
            state.reload()
        return {"ok": True}

    # ------------------------------------------------------- connectors
    @app.get("/api/connectors")
    def connectors():
        from .mcp import import_from_other_apps
        have = set(state.mcp.servers)
        return {"items": state.mcp.status(),
                "importable": {k: v for k, v in import_from_other_apps().items() if k not in have}}

    @app.post("/api/connectors")
    def connector_save(body: dict):
        import shlex
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(body.get("name", "")).strip())[:30]
        if not name:
            raise HTTPException(400, "Give the connector a short name.")
        servers = dict(setup_service.read_user_config().get("mcp_servers") or {})
        spec = dict(servers.get(name) or {})
        if body.get("command"):
            spec = {"command": shlex.split(body["command"], posix=os.name != "nt"), **({"env": spec["env"]} if spec.get("env") else {})}
        elif body.get("url"):
            spec = {"url": body["url"].strip()}
        if "enabled" in body:
            spec["enabled"] = bool(body["enabled"])
        if "readonly" in body:
            spec["readonly"] = bool(body["readonly"])
        if body.get("env"):
            spec["env"] = {k: str(v) for k, v in body["env"].items()}
        if not (spec.get("command") or spec.get("url")):
            raise HTTPException(400, "Enter the command that starts it, or its web address.")
        servers[name] = spec
        data = setup_service.read_user_config()
        data["mcp_servers"] = servers
        setup_service.write_user_config(data)
        state.reload()
        return {"ok": True}

    @app.post("/api/connectors/import")
    def connector_import(body: dict):
        from .mcp import import_from_other_apps
        found = import_from_other_apps()
        data = setup_service.read_user_config()
        servers = dict(data.get("mcp_servers") or {})
        for n in body.get("names") or []:
            if n in found:
                servers[n] = found[n]
        data["mcp_servers"] = servers
        setup_service.write_user_config(data)
        state.reload()
        return {"ok": True}

    @app.post("/api/connectors/{name}/test")
    def connector_test(name: str):
        srv = state.mcp.servers.get(name)
        if not srv:
            raise HTTPException(404)
        try:
            srv.start()
            return {"ok": True, "tools": [t["name"] for t in srv.tools]}
        except Exception as e:
            raise HTTPException(400, f"Couldn't connect: {e}")

    # ----------------------------------------------------------- skills
    def _skills():
        from .skills import SkillStore
        return SkillStore(state.cfg)

    @app.get("/api/skills")
    def skills():
        disabled = set(state.cfg.data.get("disabled_skills") or [])
        store = _skills()
        all_ = {}
        for root, src in store.dirs():
            if root.is_dir():
                from .skills import Skill
                for md in sorted(root.glob("*/SKILL.md")):
                    try:
                        sk = Skill(md, "installed" if src == "mine" and "installed_from" in md.read_text(encoding="utf-8")[:500] else src)
                    except OSError:
                        continue
                    all_[sk.name] = {"name": sk.name, "description": sk.description, "source": sk.source,
                                     "origin": sk.origin, "enabled": sk.name not in disabled, "body": sk.body[:6000]}
        return list(all_.values())

    @app.post("/api/skills/preview")
    def skill_preview(body: dict):
        from .skills import parse_skill, scan
        try:
            text, files = _skills().fetch(str(body.get("source", "")))
        except Exception as e:
            raise HTTPException(400, f"Couldn't download that skill: {e}")
        meta, sk_body = parse_skill(text)
        risks = scan(text + "\n".join(b.decode("utf-8", "ignore")[:20000] for b in files.values()))
        return {"name": meta.get("name", ""), "description": meta.get("description", ""), "risks": risks,
                "files": list(files)[:30], "body": sk_body[:6000]}

    @app.post("/api/skills/install")
    def skill_install(body: dict):
        rt = state.make_runtime()
        rt.approver = lambda q: True  # the user just reviewed the preview and clicked Install
        try:
            return {"message": _skills().install(rt, str(body.get("source", "")))}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/skills/toggle")
    def skill_toggle(body: dict):
        data = setup_service.read_user_config()
        off = set(data.get("disabled_skills") or [])
        (off.discard if body.get("enabled") else off.add)(str(body.get("name")))
        data["disabled_skills"] = sorted(off)
        setup_service.write_user_config(data)
        state.reload()
        return {"ok": True}

    # ----------------------------------------------------------- memory
    @app.get("/api/memory")
    def memory():
        rt = state.make_runtime()
        return rt.memory.recent(300)

    @app.post("/api/memory/add")
    def memory_add(body: dict):
        rt = state.make_runtime()
        return {"id": rt.memory.add(str(body.get("text", "")).strip())}

    @app.post("/api/memory/archive")
    def memory_archive(body: dict):
        rt = state.make_runtime()
        return {"ok": rt.memory.archive(int(body.get("id", 0)))}

    # ------------------------------------------------------------ phone
    @app.post("/api/phone/connect")
    def phone_connect(body: dict):
        try:
            return state.telegram.connect(str(body.get("token", "")))
        except Exception as e:
            raise HTTPException(400, f"Telegram didn't accept that token: {e}")

    @app.post("/api/phone/disconnect")
    def phone_disconnect():
        state.telegram.stop()
        state.telegram.token, state.telegram.owner_id = "", None
        state._save_patch({"telegram": {}})
        return {"ok": True}

    @app.post("/api/phone/test")
    def phone_test():
        try:
            state.telegram.send("Hello from LocalAgent on your computer 👋")
            return {"ok": True}
        except Exception as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ voice
    @app.post("/api/transcribe")
    def transcribe(body: UploadIn):
        from .voice import transcribe as tr
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=Path(body.name).suffix or ".webm", delete=False) as f:
            f.write(base64.b64decode(body.data.split(",", 1)[-1]))
            path = f.name
        try:
            return {"text": tr(path, state.cfg)}
        except RuntimeError as e:
            raise HTTPException(501, str(e))
        except Exception as e:
            raise HTTPException(400, f"Couldn't understand the recording: {e}")

    @app.post("/api/autostart")
    def autostart(body: dict):
        try:
            setup_service.set_autostart(bool(body.get("enabled")))
            return {"enabled": setup_service.autostart_enabled()}
        except Exception as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ trash
    @app.get("/api/trash")
    def trash():
        from .trash import Trash
        return Trash(state.cfg.trash_dir).list(limit=100)

    @app.post("/api/restore")
    def restore(body: IdIn):
        from .trash import Trash
        try:
            return {"restored": str(Trash(state.cfg.trash_dir).restore(body.id))}
        except Exception as e:
            raise HTTPException(400, str(e))

    return app


def serve(host: str = "127.0.0.1", port: int = 7860, config: str | None = None) -> None:
    import uvicorn
    extra = ()
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: the agent can run any command on this machine. Only expose it on a network you trust.")
        extra = (host,)
    app = create_app(config, extra_hosts=extra)
    app.state.port = port
    print(f"LocalAgent → http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/?t={app.state.token}")
    print("Keep this link private — it's the key to your agent.")
    uvicorn.run(app, host=host, port=port, log_level="warning")
