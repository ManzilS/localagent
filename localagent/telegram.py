"""Control LocalAgent from your phone through a private Telegram bot.

Setup (the app walks you through it):
  1. In Telegram, message @BotFather, send /newbot, copy the token.
  2. Paste the token into LocalAgent → Phone. It shows a pairing code.
  3. Send "/pair CODE" to your new bot. Only that Telegram account is
     ever obeyed from then on.

Security choices (from 2026's agent incidents):
  • outbound long-polling only — no port is opened on your computer;
  • one owner, paired with a one-time code; everyone else is ignored;
  • group chats are ignored entirely (anyone in a group could give orders);
  • approvals for risky steps arrive as Allow / Don't allow buttons.
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

import httpx

API = "https://api.telegram.org"
LIMIT = 4000


class Telegram:
    def __init__(self, cfg, owner, make_runtime, save_settings):
        self.cfg = cfg
        self.owner = owner                  # automations.Owner
        self.make_runtime = make_runtime    # () -> Runtime
        self.save_settings = save_settings  # (dict patch) -> None
        tg = cfg.data.get("telegram") or {}
        self.token: str = tg.get("token", "")
        self.owner_id: int | None = tg.get("owner_id")
        self.pair_code: str | None = None
        self.client = httpx.Client(timeout=httpx.Timeout(45, connect=10))
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.agent = None
        self.rt = None
        self.busy = threading.Lock()
        self.status = "off"
        self.bot_name = ""

    # ------------------------------------------------------------ api
    def _call(self, method: str, **params):
        r = self.client.post(f"{API}/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram error"))
        return data["result"]

    def send(self, text: str, buttons: list | None = None) -> None:
        if not (self.token and self.owner_id):
            return
        chunks = [text[i:i + LIMIT] for i in range(0, max(len(text), 1), LIMIT)] or [""]
        for i, chunk in enumerate(chunks):
            params = {"chat_id": self.owner_id, "text": chunk or "(empty)"}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": [buttons]}
            self._call("sendMessage", **params)

    def send_file(self, path: str, caption: str = "") -> None:
        if not (self.token and self.owner_id):
            raise RuntimeError("phone isn't paired")
        p = Path(path)
        with open(p, "rb") as f:
            r = self.client.post(f"{API}/bot{self.token}/sendDocument", data={"chat_id": self.owner_id, "caption": caption[:900]},
                                 files={"document": (p.name, f)}, timeout=120)
        if not r.json().get("ok"):
            raise RuntimeError(r.json().get("description", "upload failed"))

    # owner-channel interface (see automations.Owner)
    def notify(self, title: str, text: str) -> None:
        self.send(f"{title}\n\n{text}")

    def ask(self, aid: str, question: str) -> None:
        self.send(f"LocalAgent wants to: {question}", [{"text": "Allow", "callback_data": f"ap:{aid}:1"},
                                                        {"text": "Don't allow", "callback_data": f"ap:{aid}:0"}])

    # --------------------------------------------------------- control
    def connect(self, token: str) -> dict:
        self.token = token.strip()
        me = self._call("getMe")
        self.bot_name = me.get("username", "")
        self.pair_code = f"{secrets.randbelow(10**6):06d}"
        self.owner_id = None
        self.save_settings({"telegram": {"token": self.token, "owner_id": None}})
        self.start()
        return {"bot": self.bot_name, "code": self.pair_code}

    def info(self) -> dict:
        return {"connected": bool(self.token), "paired": bool(self.owner_id), "bot": self.bot_name,
                "code": self.pair_code if self.token and not self.owner_id else None, "status": self.status}

    def start(self) -> None:
        if not self.token or (self.thread and self.thread.is_alive()):
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="la-telegram")
        self.thread.start()
        if self.owner_id and self not in self.owner.channels:
            self.owner.channels.append(self)

    def stop(self) -> None:
        self._stop.set()
        if self in self.owner.channels:
            self.owner.channels.remove(self)

    def _loop(self) -> None:
        offset = None
        if not self.bot_name:
            try:
                self.bot_name = self._call("getMe").get("username", "")
            except Exception as e:
                self.status = f"error: {e}"
                return
        self.status = "running"
        while not self._stop.is_set():
            try:
                params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
                if offset:
                    params["offset"] = offset
                for up in self._call("getUpdates", **params):
                    offset = up["update_id"] + 1
                    try:
                        self.handle(up)
                    except Exception as e:
                        self.status = f"last error: {e}"
            except Exception as e:
                self.status = f"reconnecting ({e})"
                self._stop.wait(5)

    # --------------------------------------------------------- updates
    def handle(self, up: dict) -> None:
        if "callback_query" in up:
            cq = up["callback_query"]
            if cq.get("from", {}).get("id") != self.owner_id:
                return
            data = cq.get("data", "")
            if data.startswith("ap:"):
                _, aid, allow = data.split(":")
                ok = self.owner.answer(aid, allow == "1")
                self._call("answerCallbackQuery", callback_query_id=cq["id"],
                           text=("Allowed" if allow == "1" else "Not allowed") if ok else "That request already expired")
            return
        msg = up.get("message") or {}
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return  # never act on group messages
        sender = msg.get("from", {}).get("id")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if self.owner_id is None:
            if self.pair_code and text == f"/pair {self.pair_code}":
                self.owner_id = sender
                self.pair_code = None
                self.save_settings({"telegram": {"token": self.token, "owner_id": sender}})
                if self not in self.owner.channels:
                    self.owner.channels.append(self)
                self.send("Paired. I'll only listen to you. Send me any task — I work on your computer and "
                          "will ask here before anything risky. /stop cancels, /new starts fresh.")
            return
        if sender != self.owner_id:
            return  # strangers get silence
        if text == "/stop":
            if self.rt:
                self.rt.cancel.set()
            self.send("Stopping.")
            return
        if text == "/new":
            self.agent = None
            self.send("Starting fresh.")
            return
        if text in ("/start", "/help"):
            self.send("Send a task in plain words, a photo with a question, or a voice note. /stop cancels, /new starts fresh.")
            return
        images, extra = [], ""
        if msg.get("photo"):
            images.append(self._download(msg["photo"][-1]["file_id"], ".jpg"))
        if msg.get("document"):
            p = self._download(msg["document"]["file_id"], Path(msg["document"].get("file_name", "file")).suffix)
            extra = f"\n\n(The user sent a file: {p})"
        if msg.get("voice"):
            from .voice import transcribe
            try:
                text = transcribe(self._download(msg["voice"]["file_id"], ".ogg"), self.cfg) or text
                self.send(f"Heard: “{text}”")
            except Exception as e:
                self.send(f"I couldn't understand the voice note ({e}). Please type it.")
                return
        if not (text or images):
            return
        threading.Thread(target=self._run, args=((text or "What's in this photo?") + extra, images), daemon=True).start()

    def _download(self, file_id: str, suffix: str) -> str:
        info = self._call("getFile", file_id=file_id)
        r = self.client.get(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=120)
        up = self.cfg.workspace / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        p = up / f"phone_{int(time.time())}{suffix or ''}"
        p.write_bytes(r.content)
        return str(p)

    def _run(self, text: str, images: list[str]) -> None:
        if not self.busy.acquire(blocking=False):
            self.send("Still working on your last request — send /stop to cancel it.")
            return
        try:
            from .agent import Agent
            if self.agent is None:
                self.rt = self.make_runtime()
                timeout = float((self.cfg.data.get("desktop") or {}).get("approval_timeout", 600))
                self.rt.approver = lambda q: self.owner.ask(q, timeout=timeout)
                self.agent = Agent(self.rt, mode="auto", system_extra=(
                    "The user is talking to you from their phone through Telegram, away from the computer. Keep replies short. "
                    "Use send_phone_message to send them a file when that's what they want."))
            try:
                self._call("sendChatAction", chat_id=self.owner_id, action="typing")
            except Exception:
                pass
            self.send(self.agent.run(text, images=images or None))
        except Exception as e:
            self.send(f"Something went wrong: {e}")
        finally:
            self.busy.release()
