"""Automations: tasks that run on a schedule, a quiet heartbeat, an inbox for
results, and an "owner" channel for asking permission while you're away.

Schedules are written the way people say them:
    "every 30 minutes"   "every 2 hours"   "daily 08:00"   "weekdays 8:30"
    "every monday 9am"   "once 2026-11-02 15:00"   "in 20 minutes"

Heartbeat: every N minutes during waking hours the agent reads
~/.localagent/HEARTBEAT.md (your checklist: "anything due today?", "is the
backup drive full?") and stays silent unless something needs you.

Unattended runs can't approve their own risky actions: approval requests
go to you (app inbox, phone if paired) and are denied if you don't answer.
Automations can't create other automations.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
QUIET = "NOTHING_TO_REPORT"

HEARTBEAT_TEMPLATE = """# Heartbeat checklist
LocalAgent reads this every so often while your computer is on. It only
tells you something when an item needs your attention.

- Is anything in my workspace's todo.md due today or overdue?
- Is my main disk more than 90% full?
"""


class ScheduleError(ValueError):
    pass


def _time(s: str) -> tuple[int, int]:
    s = s.strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        raise ScheduleError(f"can't read the time '{s}' (try 08:30 or 8:30am)")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ScheduleError(f"'{s}' isn't a real time")
    return h, mi


def parse_when(spec: str, now: datetime | None = None) -> dict:
    """Turn a human schedule into a rule dict."""
    now = now or datetime.now()
    s = " ".join(spec.lower().replace(",", " ").split())
    s = re.sub(r"\b(at|on)\b ", "", s)
    if m := re.fullmatch(r"every (\d+)?\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days)", s):
        n = int(m.group(1) or 1)
        unit = m.group(2)
        mins = n * (60 if unit.startswith(("hour", "hr")) else 1440 if unit.startswith("day") else 1)
        if mins < 5:
            raise ScheduleError("the shortest interval is every 5 minutes")
        return {"kind": "interval", "minutes": mins}
    if m := re.fullmatch(r"in (\d+)\s*(minute|minutes|min|mins|hour|hours)", s):
        n = int(m.group(1)) * (60 if m.group(2).startswith("hour") else 1)
        return {"kind": "once", "at": (now + timedelta(minutes=n)).isoformat(timespec="minutes")}
    if m := re.fullmatch(r"(?:every day|daily|each day|everyday) (.+)", s):
        h, mi = _time(m.group(1))
        return {"kind": "daily", "h": h, "m": mi, "days": list(range(7))}
    if m := re.fullmatch(r"(?:every )?weekdays? (.+)", s):
        h, mi = _time(m.group(1))
        return {"kind": "daily", "h": h, "m": mi, "days": [0, 1, 2, 3, 4]}
    if m := re.fullmatch(r"(?:every )?weekends? (.+)", s):
        h, mi = _time(m.group(1))
        return {"kind": "daily", "h": h, "m": mi, "days": [5, 6]}
    if m := re.fullmatch(r"(?:every )?(" + "|".join(DAYS) + r")s? (.+)", s):
        h, mi = _time(m.group(2))
        return {"kind": "daily", "h": h, "m": mi, "days": [DAYS.index(m.group(1))]}
    if m := re.fullmatch(r"(?:once )?(\d{4}-\d{2}-\d{2})[ t](.+)", s):
        h, mi = _time(m.group(2))
        d = datetime.fromisoformat(m.group(1)).replace(hour=h, minute=mi)
        return {"kind": "once", "at": d.isoformat(timespec="minutes")}
    if m := re.fullmatch(r"(?:once )?(?:today )?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", s):
        h, mi = _time(m.group(1))
        d = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if d <= now:
            d += timedelta(days=1)
        return {"kind": "once", "at": d.isoformat(timespec="minutes")}
    raise ScheduleError(f"I don't understand the schedule '{spec}'. Try: every 2 hours, daily 08:00, "
                        "weekdays 9am, every monday 9:30, in 20 minutes, once 2026-11-02 15:00")


def next_run(rule: dict, after: datetime) -> datetime | None:
    if rule["kind"] == "interval":
        return after + timedelta(minutes=rule["minutes"])
    if rule["kind"] == "once":
        t = datetime.fromisoformat(rule["at"])
        return t if t > after else None
    base = after.replace(second=0, microsecond=0)
    for add in range(0, 8):
        d = (base + timedelta(days=add)).replace(hour=rule["h"], minute=rule["m"])
        if d > after and d.weekday() in rule["days"]:
            return d
    return None


def describe(rule: dict) -> str:
    if rule["kind"] == "interval":
        m = rule["minutes"]
        return f"every {m // 60} hour{'s' if m // 60 > 1 else ''}" if m % 60 == 0 else f"every {m} minutes"
    if rule["kind"] == "once":
        return "once, " + datetime.fromisoformat(rule["at"]).strftime("%a %b %d %H:%M")
    days = rule["days"]
    when = "every day" if len(days) == 7 else "weekdays" if days == [0, 1, 2, 3, 4] else "weekends" if days == [5, 6] \
        else ", ".join(DAYS[d].title() + "s" for d in days)
    return f"{when} at {rule['h']:02d}:{rule['m']:02d}"


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, items: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class Inbox:
    """Results and messages from automations, newest first."""
    def __init__(self, home: Path):
        self.store = JsonStore(home / "inbox.json")
        self.listeners: list[Callable[[dict], None]] = []

    def add(self, title: str, text: str, source: str = "", important: bool = False) -> dict:
        item = {"id": uuid.uuid4().hex[:10], "time": time.time(), "title": title, "text": text,
                "source": source, "read": False, "important": important}
        with self.store.lock:
            items = [item] + self.store.load()
            self.store.save(items[:300])
        for fn in list(self.listeners):
            try:
                fn(item)
            except Exception:
                pass
        return item

    def list(self) -> list[dict]:
        return self.store.load()

    def mark_read(self, ids: list[str] | None = None) -> None:
        with self.store.lock:
            items = self.store.load()
            for it in items:
                if ids is None or it["id"] in ids:
                    it["read"] = True
            self.store.save(items)

    def unread(self) -> int:
        return sum(1 for i in self.list() if not i["read"])


class Owner:
    """Ways to reach the person: app inbox, phone, desktop notifications.
    ask() waits for the first answer from any channel; silence = no."""
    def __init__(self, inbox: Inbox):
        self.inbox = inbox
        self.pending: dict[str, dict] = {}
        self.channels: list = []  # objects with notify(title, text) and ask(aid, question)

    def notify(self, title: str, text: str, source: str = "", important: bool = True) -> None:
        self.inbox.add(title, text, source, important)
        for ch in list(self.channels):
            try:
                ch.notify(title, text)
            except Exception:
                pass

    def ask(self, question: str, timeout: float = 600) -> bool:
        aid = uuid.uuid4().hex[:8]
        slot = {"ev": threading.Event(), "allow": False, "question": question, "time": time.time()}
        self.pending[aid] = slot
        for ch in list(self.channels):
            try:
                ch.ask(aid, question)
            except Exception:
                pass
        answered = slot["ev"].wait(timeout)
        self.pending.pop(aid, None)
        return answered and slot["allow"]

    def answer(self, aid: str, allow: bool) -> bool:
        slot = self.pending.get(aid)
        if not slot:
            return False
        slot["allow"] = allow
        slot["ev"].set()
        return True


def desktop_notify(title: str, text: str) -> None:
    """Best-effort OS notification without extra dependencies."""
    import platform
    import subprocess
    t, b = title[:80], text[:200]
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(b)} with title {json.dumps(t)}'])
        elif platform.system() == "Windows":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                  "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
                  f"$x.GetElementsByTagName('text').Item(0).AppendChild($x.CreateTextNode({json.dumps(t + ': ' + b)})) > $null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LocalAgent').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=0x08000000)
        else:
            subprocess.Popen(["notify-send", t, b])
    except Exception:
        pass


class DesktopChannel:
    def notify(self, title, text):
        desktop_notify(title, text)

    def ask(self, aid, question):
        desktop_notify("LocalAgent needs your OK", question)


class Scheduler:
    """Runs due automations in the background. One run at a time."""

    def __init__(self, cfg, make_runtime: Callable, owner: Owner):
        self.cfg = cfg
        self.make_runtime = make_runtime      # () -> Runtime
        self.owner = owner
        self.store = JsonStore(cfg.home / "automations.json")
        self.heartbeat_file = cfg.home / "HEARTBEAT.md"
        if not self.heartbeat_file.exists():
            self.heartbeat_file.write_text(HEARTBEAT_TEMPLATE, encoding="utf-8")
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self.thread: threading.Thread | None = None
        self.running_id: str | None = None

    # ------------------------------------------------------------ CRUD
    def list(self) -> list[dict]:
        return [a for a in self.store.load() if not a.get("archived")]

    def add(self, task: str, when: str, name: str = "", mode: str = "auto", notify: str = "always",
            created_by: str = "you") -> dict:
        rule = parse_when(when)
        nxt = next_run(rule, datetime.now())
        if nxt is None:
            raise ScheduleError("that time is already in the past")
        item = {"id": uuid.uuid4().hex[:8], "name": name or task[:60], "task": task, "when": when, "rule": rule,
                "schedule": describe(rule), "mode": mode, "notify": notify, "enabled": True, "kind": "task",
                "created": time.time(), "created_by": created_by, "next_run": nxt.isoformat(timespec="minutes"),
                "last_run": None, "last_result": ""}
        with self.store.lock:
            items = self.store.load()
            items.append(item)
            self.store.save(items)
        return item

    def update(self, aid: str, **changes) -> dict:
        with self.store.lock:
            items = self.store.load()
            for it in items:
                if it["id"] == aid:
                    if "when" in changes:
                        it["rule"] = parse_when(changes["when"])
                        it["schedule"] = describe(it["rule"])
                        n = next_run(it["rule"], datetime.now())
                        it["next_run"] = n.isoformat(timespec="minutes") if n else None
                    for k in ("enabled", "name", "task", "when", "mode", "notify", "archived"):
                        if k in changes:
                            it[k] = changes[k]
                    self.store.save(items)
                    return it
        raise KeyError(aid)

    def heartbeat_settings(self) -> dict:
        hb = self.cfg.data.get("heartbeat") or {}
        return {"enabled": bool(hb.get("enabled", False)), "minutes": int(hb.get("minutes", 60)),
                "start": int(hb.get("start_hour", 8)), "end": int(hb.get("end_hour", 22))}

    # ------------------------------------------------------------- run
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self._last_heartbeat = time.time()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="la-scheduler")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._catch_up()
        while not self._stop.wait(20):
            try:
                self.tick()
            except Exception:
                pass

    def _catch_up(self) -> None:
        """Computer was off: run each overdue task once (no backlog), reschedule the rest."""
        now = datetime.now()
        with self.store.lock:
            items = self.store.load()
            for it in items:
                if it.get("next_run") and datetime.fromisoformat(it["next_run"]) < now - timedelta(hours=12):
                    n = next_run(it["rule"], now)
                    it["next_run"] = n.isoformat(timespec="minutes") if n else None
            self.store.save(items)

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for it in self.list():
            if it["enabled"] and it.get("next_run") and datetime.fromisoformat(it["next_run"]) <= now:
                self.run(it["id"], now=now)
        hb = self.heartbeat_settings()
        if hb["enabled"] and hb["start"] <= now.hour < hb["end"] and time.time() - self._last_heartbeat >= hb["minutes"] * 60:
            self._last_heartbeat = time.time()
            self.heartbeat()

    def _agent_run(self, task: str, mode: str, label: str) -> str:
        from .agent import Agent
        rt = self.make_runtime()
        timeout = float((self.cfg.data.get("desktop") or {}).get("approval_timeout", 600))
        rt.approver = lambda q: self.owner.ask(f"[{label}] {q}", timeout=timeout)
        agent = Agent(rt, mode=mode, exclude_tools={"schedule_task"},  # automations can't spawn automations
                      system_extra=f"This is an unattended automation run ('{label}'). The user is not watching. "
                                   "Do the task, then report the result briefly.")
        agent.unattended = True
        try:
            return agent.run(task)
        finally:
            rt.shutdown()

    def run(self, aid: str, now: datetime | None = None) -> str:
        it = next((a for a in self.list() if a["id"] == aid), None)
        if it is None:
            raise KeyError(aid)
        with self._busy:
            self.running_id = aid
            try:
                result = self._agent_run(it["task"], it.get("mode", "auto"), it["name"])
            except Exception as e:
                result = f"Couldn't finish: {e}"
            finally:
                self.running_id = None
        due = datetime.fromisoformat(it["next_run"]) if it.get("next_run") else datetime.min
        now = max(now or datetime.now(), datetime.now(), due)
        n = next_run(it["rule"], now)
        with self.store.lock:
            items = self.store.load()
            for x in items:
                if x["id"] == aid:
                    x["last_run"] = now.isoformat(timespec="minutes")
                    x["last_result"] = result[:2000]
                    x["next_run"] = n.isoformat(timespec="minutes") if n else None
                    if n is None:
                        x["enabled"] = False
            self.store.save(items)
        if it.get("notify", "always") == "always" or QUIET not in result:
            self.owner.notify(it["name"], result, source=f"automation:{aid}", important=it.get("notify") == "always")
        return result

    def heartbeat(self) -> str | None:
        checklist = self.heartbeat_file.read_text(encoding="utf-8") if self.heartbeat_file.exists() else ""
        if not checklist.strip():
            return None
        task = (f"Heartbeat check. Go through this checklist the user wrote:\n\n{checklist}\n\n"
                f"If NOTHING needs the user's attention right now, reply with exactly {QUIET} and nothing else. "
                "Otherwise reply with a short note (under 80 words) about only the items that need attention.")
        with self._busy:
            result = self._agent_run(task, "quick", "heartbeat")
        if QUIET in result.strip()[:40] or not result.strip():
            return None
        self.owner.notify("Heartbeat", result, source="heartbeat", important=True)
        return result
