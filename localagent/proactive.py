"""Proactive assistant: LocalAgent finds useful things to do without being asked.

Most people stop using AI because they don't know what to ask. So instead of
waiting, LocalAgent keeps a "For you" feed of cards:

  * Noticed  — cheap checks with no AI involved: a new PDF in Downloads, a
               messy Downloads folder, a nearly full disk, a request you've
               typed three times, a task that failed.
  * Ideas    — every few hours, when the computer is idle, the local model
               looks at what it knows about you (the short "about you" answers,
               memory, recent task titles, file *names*) and suggests up to 3
               jobs. Only file names and titles are used here, never contents.
  * Brief    — a morning summary when there is something to summarize.

Each card is either done automatically or waits for one tap:
  * Automatic only for read-only kinds (disk check, morning brief) and for
    kinds you've said yes to twice in a row ("you always want this, so I'll
    just do it" — you're told when that happens and can undo it).
  * Everything runs as a normal background task: it can't delete (agent_trash),
    and anything risky (sending, buying, posting) still waits for your OK.
  * Automatic work uses the model on this computer unless you change it, so
    nothing of yours goes to a cloud service you didn't pick.
  * Quiet hours and a daily cap keep it from nagging. "Never suggest this"
    turns a kind off for good.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

DOC_EXT = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".csv", ".txt", ".md", ".rtf", ".epub"}
STOP = set("""a an the and or but to of in on for with at by from is are was were be been it this that these those
my me i you your please can could would should will just now some any all into about as do does did get make
tell show give find""".split())

# Kinds that only read and report, so they may run without asking.
READ_ONLY_KINDS = {"disk_space", "morning_brief", "summarize_new_doc", "weekly_recap", "meeting_prep",
                   "email_reply", "storage_full", "offload_suggest"}   # email_reply only saves a draft; it never sends
DEFAULT_AUTO = {"disk_space", "morning_brief"}

AREAS = {
    "email": "Email & calendar", "files": "Files & folders", "research": "Research & shopping",
    "writing": "Writing", "code": "Coding", "money": "Money & bills", "learning": "Learning & school",
    "health": "Health & fitness", "news": "News & topics I follow",
}


def _now() -> datetime:
    return datetime.now()


def words(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOP)


GENERIC = set("help organize organise folder folders files file your can want like could would new check".split())


def overlaps(a: str, b: str) -> bool:
    """Same topic? Compares the distinctive words of two short titles."""
    x, y = words(a) - GENERIC, words(b) - GENERIC
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.5


def similar(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class Proactive:
    def __init__(self, cfg, router, tasks, scheduler, owner, memory_recent: Callable[[], list[str]] | None = None,
                 now: Callable[[], datetime] = _now, folders: list[Path] | None = None, connections=None):
        self.connections = connections
        self.cfg = cfg
        self.router = router
        self.tasks = tasks
        self.scheduler = scheduler
        self.owner = owner
        self.memory_recent = memory_recent or (lambda: [])
        self.now = now
        self._folders = folders
        self.path: Path = cfg.home / "proactive.json"
        self.lock = threading.RLock()
        self.data = self._load()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.thinking = False

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        p = self.cfg.data.get("proactive") or {}
        return {"level": p.get("level", "auto"),           # off | suggest | auto
                "brain": p.get("brain", "local"),           # brain for work it starts on its own
                "brief_hour": int(p.get("brief_hour", 8)),
                "quiet_start": int(p.get("quiet_start", 22)), "quiet_end": int(p.get("quiet_end", 8)),
                "ideas_every_hours": float(p.get("ideas_every_hours", 4)),
                "max_alerts_per_day": int(p.get("max_alerts_per_day", 4)),
                "watch_folders": bool(p.get("watch_folders", True))}

    def folders(self) -> list[Path]:
        if self._folders is not None:
            return self._folders
        home = Path.home()
        return [p for p in (home / "Downloads", home / "Desktop", home / "Documents") if p.is_dir()]

    def quiet(self) -> bool:
        s, h = self.settings(), self.now().hour
        a, b = s["quiet_start"], s["quiet_end"]
        return (h >= a or h < b) if a > b else (a <= h < b)

    # --------------------------------------------------------------- store
    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        d.setdefault("cards", [])
        d.setdefault("feedback", {})   # kind -> {"yes": n, "no": n, "streak": n, "auto": bool|None, "never": bool}
        d.setdefault("seen", {})       # signature -> time first seen (so a thing is suggested once)
        d.setdefault("profile", {})    # {"about": str, "areas": [...], "done": bool}
        d.setdefault("alerts", {})     # date -> count
        d.setdefault("last_ideas", 0)
        d.setdefault("last_brief", "")
        d.setdefault("last_recap", "")
        return d

    def _save(self) -> None:
        with self.lock:
            self.data["cards"] = sorted(self.data["cards"], key=lambda c: c["created"], reverse=True)[:120]
            if len(self.data["seen"]) > 2000:
                keep = sorted(self.data["seen"].items(), key=lambda kv: kv[1])[-1500:]
                self.data["seen"] = dict(keep)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------- profile
    def set_profile(self, about: str = "", areas: list[str] | None = None) -> dict:
        with self.lock:
            self.data["profile"] = {"about": about.strip()[:600], "areas": [a for a in (areas or []) if a in AREAS],
                                    "done": True, "time": time.time()}
            self.data["last_ideas"] = 0   # think about them soon
            self._save()
        return self.data["profile"]

    # ------------------------------------------------------------- feedback
    def _fb(self, kind: str) -> dict:
        return self.data["feedback"].setdefault(kind, {"yes": 0, "no": 0, "streak": 0, "auto": None, "never": False})

    def is_auto(self, kind: str) -> bool:
        fb = self.data["feedback"].get(kind) or {}
        if fb.get("auto") is not None:
            return bool(fb["auto"])
        return kind in DEFAULT_AUTO

    def set_auto(self, kind: str, on: bool) -> None:
        with self.lock:
            self._fb(kind)["auto"] = bool(on)
            self._save()

    def blocked(self, kind: str) -> bool:
        return bool((self.data["feedback"].get(kind) or {}).get("never"))

    # ---------------------------------------------------------------- cards
    def cards(self, include_closed: bool = False) -> list[dict]:
        self._sync_task_status()
        with self.lock:
            out = [dict(c) for c in self.data["cards"] if include_closed or c["status"] in ("new", "running")]
        for c in out:
            c["automatic"] = self.is_auto(c["kind"])
        return out

    def add_card(self, kind: str, title: str, task: str, why: str = "", sig: str = "", when: str = "",
                 source: str = "noticed", brain: str = "", action: dict | None = None) -> dict | None:
        """Add a suggestion unless it's a duplicate, blocked, or proactivity is off."""
        if self.settings()["level"] == "off" or self.blocked(kind):
            return None
        sig = sig or f"{kind}:{title.lower()}"
        with self.lock:
            if sig in self.data["seen"]:
                return None
            if source == "idea" and any(overlaps(c["title"], title) or similar(words(c["title"]), words(title)) > 0.7
                                        for c in self.data["cards"] if c["status"] in ("new", "running")):
                return None   # the model rephrasing something it already suggested
            self.data["seen"][sig] = time.time()
            card = {"id": uuid.uuid4().hex[:8], "kind": kind, "title": title[:120], "why": why[:300], "task": task,
                    "when": when, "source": source, "brain": brain, "status": "new", "created": time.time(),
                    "task_id": None, "note": "", "action": action}
            self.data["cards"].append(card)
            self._save()
        if self.settings()["level"] == "auto" and self.is_auto(kind) and not when and not action:
            self._start(card, automatic=True)
        return card

    def accept(self, cid: str) -> dict:
        with self.lock:
            card = next((c for c in self.data["cards"] if c["id"] == cid), None)
            if card is None:
                raise KeyError(cid)
            if card["status"] != "new":
                return card
            fb = self._fb(card["kind"])
            fb["yes"] += 1
            fb["streak"] += 1
            promote = (fb["auto"] is None and fb["streak"] >= 2 and card["kind"] in READ_ONLY_KINDS | {"tidy_downloads"}
                       and not card.get("when"))
            if promote:
                fb["auto"] = True
                card["note"] = "You've said yes to this twice, so I'll do it automatically from now on. You can turn that off in For you → Settings."
        act = card.get("action") or {}
        if act.get("do") == "connect_notes" and self.connections is not None:
            try:
                r = self.connections.add("notes", {"app": act["app"], "path": act["path"], "name": act.get("name", "")})
                card["status"], card["note"] = "done", r["message"]
            except Exception as e:
                card["note"] = f"Couldn't connect it: {e}"
            self._save()
            return card
        if act.get("do") == "open":
            card["status"] = "done"
            self._save()
            return card
        if card.get("when"):
            try:
                a = self.scheduler.add(card["task"], card["when"], name=card["title"], created_by="agent")
                card["status"], card["note"] = "done", f"Set up: {a['schedule']}."
            except Exception as e:
                card["note"] = f"Couldn't schedule it: {e}"
            self._save()
            return card
        self._start(card, automatic=False)
        return card

    def dismiss(self, cid: str, never: bool = False) -> dict:
        with self.lock:
            card = next((c for c in self.data["cards"] if c["id"] == cid), None)
            if card is None:
                raise KeyError(cid)
            fb = self._fb(card["kind"])
            fb["no"] += 1
            fb["streak"] = 0
            if never:
                fb["never"] = True
                for c in self.data["cards"]:
                    if c["kind"] == card["kind"] and c["status"] == "new":
                        c["status"] = "dismissed"
            card["status"] = "dismissed"
            self._save()
        return card

    def unblock(self, kind: str) -> None:
        with self.lock:
            self._fb(kind)["never"] = False
            self._save()

    def _start(self, card: dict, automatic: bool) -> None:
        brain = card.get("brain") or (self.settings()["brain"] if automatic else "auto")
        try:
            t = self.tasks.create(card["task"], brain=brain, mode="auto", source="for you")
        except Exception as e:
            card["note"] = f"Couldn't start: {e}"
            self._save()
            return
        with self.lock:
            card["status"], card["task_id"], card["auto_ran"] = "running", t["id"], automatic
            self._save()

    def _sync_task_status(self) -> None:
        changed = False
        with self.lock:
            for c in self.data["cards"]:
                if c["status"] == "running" and c.get("task_id"):
                    t = self.tasks.get(c["task_id"])
                    if t is None or t["status"] in ("done", "failed", "stopped"):
                        c["status"] = "done"
                        c["result"] = (t or {}).get("result", "")[:1500]
                        changed = True
            if changed:
                self._save()

    # ------------------------------------------------------------ noticing
    def notice(self) -> list[dict]:
        """Cheap checks, no AI. Returns the cards it added."""
        added: list[dict] = []
        s = self.settings()
        add = lambda *a, **k: (c := self.add_card(*a, **k)) and added.append(c)

        # 1. nearly full disk
        try:
            root = Path(self.cfg.workspace).anchor or "/"
            du = shutil.disk_usage(root)
            if du.free / du.total < 0.10:
                week = self.now().strftime("%G-%V")
                add("disk_space", f"Your {root} drive is {100 - du.free * 100 // du.total}% full",
                    f"My {root} drive is almost full ({du.free // 2**30} GB free). Find the biggest files and folders "
                    "taking up space, especially old installers, duplicates and caches. Don't move or remove anything; "
                    "just list the top 15 with sizes and say which look safe to clear.",
                    why="Low disk space slows your computer down and can stop updates.", sig=f"disk:{week}")
        except OSError:
            pass

        if s["watch_folders"]:
            cutoff = time.time() - 36 * 3600
            for folder in self.folders():
                try:
                    entries = list(folder.iterdir())
                except OSError:
                    continue
                # 2. new documents
                for p in entries:
                    try:
                        if p.is_file() and p.suffix.lower() in DOC_EXT and p.stat().st_mtime > cutoff \
                                and not p.name.startswith(("~$", ".")) and p.stat().st_size > 200:
                            add("summarize_new_doc", f"Summarize “{p.name}”?",
                                f"Read the document at {p} and give me a short summary: what it is, the key points, "
                                "and anything I need to do (dates, amounts, deadlines).",
                                why=f"New in your {folder.name} folder.", sig=f"doc:{p}:{int(p.stat().st_mtime)}")
                    except OSError:
                        continue
                # 3. messy Downloads
                if folder.name.lower() == "downloads":
                    loose = [p for p in entries if p.is_file() and not p.name.startswith(".")]
                    if len(loose) >= 30:
                        month = self.now().strftime("%Y-%m")
                        add("tidy_downloads", f"Tidy your Downloads folder ({len(loose)} loose files)",
                            f"Organize the loose files in {folder} into subfolders by type (Documents, Images, "
                            "Installers, Archives, Videos, Other). Only move files; never delete anything. "
                            "Tell me what went where.",
                            why="A tidy Downloads makes things easier to find. Nothing gets deleted.", sig=f"tidy:{month}")

        # 4. a request you keep making
        for c in self._repeats():
            add("automate_repeat", f"Do “{c['text'][:60]}” automatically?", c["text"], when=c["when"],
                why=f"You've asked for this {c['count']} times. I can do it {c['when']} without being asked.",
                sig=f"repeat:{' '.join(sorted(words(c['text'])))[:120]}")

        # 5. email, calendar and notes
        added += self.notice_connections()

        # 6. a task that failed recently
        for t in self.tasks.list()[:30]:
            if t["status"] == "failed" and t.get("finished") and time.time() - t["finished"] < 86400 \
                    and t.get("source") != "for you":
                add("retry_failed", f"Try “{t['title'][:60]}” again?", t["message"],
                    why="It didn't finish last time. I'll try with a different brain.", sig=f"retry:{t['id']}",
                    brain="auto")
        return added

    def notice_connections(self) -> list[dict]:
        """Suggestions from connected email, calendars and notes (and offers to connect them)."""
        mgr, added = self.connections, []
        if mgr is None:
            return added
        add = lambda *a, **k: (c := self.add_card(*a, **k)) and added.append(c)
        now = self.now()
        # offer to connect note apps found on this computer
        try:
            for d in mgr.detected()[:3]:
                add("connect_app", f"Connect {d['name']}?", "", sig=f"connect:{d['path']}",
                    why="I found it on this computer. I'll be able to search your notes, add to them and remind you of to-dos.",
                    action={"do": "connect_notes", "app": d["app"], "path": d["path"], "name": d["name"]})
        except Exception:
            pass
        have = {k for k in ("email", "calendar", "notes", "storage", "weather", "feeds") if mgr.of_kind(k)}
        # cloud storage filling up / local disk full while the cloud has room (checked every 6 hours)
        if "storage" in have and time.time() - self.data.get("last_storage_check", 0) > 6 * 3600:
            self.data["last_storage_check"] = time.time()
            room = []
            for c in mgr.of_kind("storage"):
                try:
                    a = c.about()
                except Exception:
                    continue
                if a.get("total") and a.get("free") is not None:
                    pct = 100 * (a["total"] - a["free"]) / a["total"]
                    if pct >= 90:
                        add("storage_full", f"Your {c.label} is {pct:.0f}% full",
                            f"My {c.label} storage is {pct:.0f}% full. Use storage_big_files (account “{c.label}”) to find what takes "
                            "the most space, and look for obvious duplicates and old backups. List the top 15 with sizes and say "
                            "which look safe to clear. Don't move or remove anything.",
                            why="When it fills up, syncing and uploads stop.", sig=f"cloudfull:{c.id}:{now:%G-%V}")
                    elif a["free"] > 20 * 2**30:
                        # somewhere else to put things: a cloud account, or a synced/cloud drive that isn't the full disk
                        elsewhere = c.mode == "rclone" or (c.root is not None and
                                                           c.root.anchor.lower() != Path(self.cfg.workspace).anchor.lower())
                        if elsewhere:
                            room.append((a["free"], c))
            try:
                du = shutil.disk_usage(Path(self.cfg.workspace).anchor or "/")
                full = du.free / du.total < 0.10
            except OSError:
                full = False
            if full and room:
                free, c = max(room, key=lambda x: x[0])
                from .connections.storage_conn import human
                add("offload_suggest", f"Free up space on {Path(self.cfg.workspace).anchor} using {c.label}?",
                    f"My {Path(self.cfg.workspace).anchor} drive is nearly full, and {c.label} has {human(free)} free. Find the 15 biggest "
                    "files in my user folder (Downloads, Videos, Documents, Desktop) that I haven't changed in 6+ months — skip "
                    "Windows, Program Files and app folders. List them with sizes and dates and suggest which to move to "
                    f"{c.label} with storage_offload. Don't move anything yet; I'll pick.",
                    why=f"{c.label} has {human(free)} free. Moving files there is verified first, and nothing is deleted.",
                    sig=f"offload:{now:%G-%V}")
        if "calendar" not in have:
            add("connect_app", "Connect your calendar?", "", sig="connect:calendar",
                why="Then I can prep you for meetings and plan your day. It takes one link, no password.",
                action={"do": "open", "page": "connect"})
        if "email" not in have:
            add("connect_app", "Connect your email?", "", sig="connect:email",
                why="Then I can point out what needs a reply and draft answers for you to check. Nothing is sent without your OK.",
                action={"do": "open", "page": "connect"})

        # important unread email -> draft a reply (checked at most every 30 minutes)
        if "email" in have and time.time() - self.data.get("last_mail_check", 0) > 1800:
            self.data["last_mail_check"] = time.time()
            for c in mgr.of_kind("email"):
                try:
                    items = c.recent(limit=25, unread_only=True, days=2)
                except Exception:
                    continue
                n = 0
                for m in items:
                    if m.get("bulk") or n >= 3:
                        continue
                    who = re.sub(r"\s*<.*?>", "", m["from"]).strip('" ') or m["from"]
                    if add("email_reply", f"Draft a reply to {who[:40]}: “{m['subject'][:60]}”?",
                           f"Read the email {m['id']} in the {c.label} account (use email_read with account “{c.address}”). "
                           "If it needs a reply from me, write one in my voice — short and friendly — and save it with "
                           "email_draft (reply_to_id set). Do NOT send it. Then tell me in two lines what they want and what you drafted. "
                           "If it doesn't need a reply, just say so.",
                           why="Unread, from a person, and it may need an answer. I'll only save a draft.",
                           sig=f"mail:{c.id}:{m['id']}"):
                        n += 1

        # a meeting soon -> prep
        if "calendar" in have:
            for c in mgr.of_kind("calendar"):
                try:
                    evs = c.events(now.astimezone(), days=0.15)     # ~3.5 hours ahead
                except Exception:
                    continue
                for ev in evs:
                    if ev["all_day"] or ev["start"] < now.astimezone():
                        continue
                    extras = ", ".join(k for k in ("email", "notes") if k in have)
                    add("meeting_prep", f"Prep for “{ev['title'][:60]}” at {ev['start']:%I:%M %p}?".replace(" 0", " "),
                        f"I have “{ev['title']}” at {ev['start']:%H:%M} today" + (f" ({ev['location']})" if ev["location"] else "") +
                        f". Event details: {ev['description'][:500] or 'none'}. " +
                        (f"Look through my {extras} for anything related (search for the event's key words and people). " if extras else "") +
                        "Give me a short prep brief: what it's about, what I should know or bring, open questions. Under 120 words.",
                        why="Coming up soon. A two-minute brief so you walk in prepared.",
                        sig=f"prep:{ev['title']}:{ev['start']:%Y%m%d%H%M}")
        return added

    def _repeats(self) -> list[dict]:
        """Requests asked 3+ times on 2+ different days that aren't already automations."""
        msgs = [(t["message"], t["created"]) for t in self.tasks.list() if t.get("source") not in ("for you",)]
        existing = [words(a.get("task", "")) for a in self.scheduler.list()]
        groups: list[dict] = []
        for text, ts in msgs:
            w = words(text)
            if len(w) < 2:
                continue
            for g in groups:
                if similar(g["words"], w) >= 0.6:
                    g["times"].append(ts)
                    break
            else:
                groups.append({"words": w, "text": text.strip(), "times": [ts]})
        out = []
        for g in groups:
            days = {datetime.fromtimestamp(t).date() for t in g["times"]}
            if len(g["times"]) >= 3 and len(days) >= 2 and not any(similar(g["words"], e) >= 0.6 for e in existing):
                hours = sorted(datetime.fromtimestamp(t).hour for t in g["times"])
                h = hours[len(hours) // 2]
                weekdays_only = all(datetime.fromtimestamp(t).weekday() < 5 for t in g["times"])
                g["when"] = f"{'weekdays' if weekdays_only else 'daily'} {h:02d}:00"
                g["count"] = len(g["times"])
                out.append(g)
        return out

    # ---------------------------------------------------------------- brief
    def maybe_brief(self) -> dict | None:
        s, now = self.settings(), self.now()
        today = now.date().isoformat()
        if self.data.get("last_brief") == today or now.hour < s["brief_hour"] or now.hour >= s["brief_hour"] + 4:
            return None
        overnight = [t["title"] for t in self.tasks.list()
                     if t.get("finished") and t["finished"] > time.time() - 16 * 3600 and t["status"] == "done"][:8]
        todo = Path(self.cfg.workspace) / "todo.md"
        conns = [n for n, c in (self.cfg.data.get("mcp_servers") or {}).items() if isinstance(c, dict) and c.get("enabled", True)]
        mgr = self.connections
        have = {k for k in ("email", "calendar", "notes") if mgr is not None and mgr.of_kind(k)}
        if not (overnight or todo.exists() or conns or have):
            return None   # nothing real to brief on; don't send an empty "good morning"
        self.data["last_brief"] = today
        self._save()
        parts = []
        if "calendar" in have:
            parts.append("list today's events with calendar_events (start today, days 1)")
        if "email" in have:
            parts.append("check unread email from the last day with email_list (unread_only) and name only the ones from real people that need me")
        if "notes" in have:
            parts.append("mention open to-dos from my notes that are due today or overdue (notes_todos)")
        if conns:
            parts.append(f"check today's calendar and important unread email using these connectors if they can: {', '.join(conns)}")
        if todo.exists():
            parts.append(f"list anything due today or overdue in {todo}")
        if overnight:
            parts.append("mention these tasks that finished since yesterday: " + "; ".join(overnight))
        return self.add_card("morning_brief", f"Your brief for {now.strftime('%A')}",
                             f"Write my morning brief for {now.strftime('%A %B %d')}. " + "; ".join(parts) +
                             ". Keep it under 150 words, most important first. Don't invent anything.",
                             why="A quick look at your day.", sig=f"brief:{today}", source="brief")

    # ---------------------------------------------------------------- ideas
    def think(self, force: bool = False) -> list[dict]:
        """Ask the local model for up to 3 ideas. Uses names and titles only."""
        s = self.settings()
        if s["level"] == "off" or (not force and time.time() - self.data["last_ideas"] < s["ideas_every_hours"] * 3600):
            return []
        prov = self.router.main
        if prov is None:
            return []
        self.data["last_ideas"] = time.time()
        self._save()
        self.thinking = True
        try:
            reply = prov.chat([{"role": "system", "content": IDEAS_PROMPT},
                               {"role": "user", "content": self._context()}],
                              temperature=0.6, max_tokens=900, think=False)
            ideas = parse_ideas(reply.content)
        except Exception:
            ideas = []
        finally:
            self.thinking = False
        added = []
        for i in ideas[:3]:
            kind = "idea:" + (i.get("area") or "general")
            c = self.add_card(kind, i["title"], i["task"], why=i.get("why", ""), when=i.get("when", ""), source="idea",
                              sig=f"idea:{' '.join(sorted(words(i['title'])))[:100]}")
            if c:
                added.append(c)
        return added

    def _context(self) -> str:
        p = self.data["profile"]
        now = self.now()
        recent = [t["title"] for t in self.tasks.list()[:20]]
        names = []
        for f in self.folders():
            try:
                items = sorted(f.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:12]
                names.append(f"{f.name}: " + ", ".join(x.name for x in items))
            except OSError:
                pass
        liked = [k for k, v in self.data["feedback"].items() if v.get("yes", 0) > v.get("no", 0)]
        disliked = [k for k, v in self.data["feedback"].items() if v.get("never") or v.get("no", 0) > v.get("yes", 0) + 1]
        open_cards = [c["title"] for c in self.data["cards"] if c["status"] == "new"]
        autos = [a["name"] for a in self.scheduler.list()]
        conns = list((self.cfg.data.get("mcp_servers") or {}).keys())
        if self.connections is not None:
            try:
                conns.append(self.connections.summary())
            except Exception:
                pass
        return "\n".join([
            f"Now: {now.strftime('%A %B %d, %H:%M')}",
            f"About the user (their words): {p.get('about') or 'not given'}",
            f"Wants help with: {', '.join(AREAS[a] for a in p.get('areas', [])) or 'not given'}",
            "Things they've told me to remember: " + ("; ".join(self.memory_recent()[:15]) or "none"),
            "Their recent requests: " + ("; ".join(recent) or "none yet"),
            "Recently changed files (names only): " + (" | ".join(names) or "unknown"),
            "Connected apps: " + (", ".join(conns) or "none"),
            "Already automated: " + (", ".join(autos) or "nothing"),
            "Already suggested, waiting: " + (", ".join(open_cards) or "nothing"),
            "Kinds they liked: " + (", ".join(liked) or "unknown") + ". Kinds they turned down: " + (", ".join(disliked) or "none"),
        ])

    # ---------------------------------------------------------------- loop
    def tick(self) -> None:
        if self.settings()["level"] == "off":
            return
        self.notice()
        self.maybe_brief()
        self.maybe_recap()
        busy = any(t["status"] in ("queued", "running", "waiting") for t in self.tasks.list()[:20])
        if not busy and not self.quiet() and self.data["profile"].get("done"):
            self.think()
        self._alert_new()

    def maybe_recap(self) -> dict | None:
        now = self.now()
        week = now.strftime("%G-%V")
        if now.weekday() != 4 or now.hour < 16 or self.data.get("last_recap") == week:
            return None
        done = [t for t in self.tasks.list() if t["status"] == "done" and t.get("finished", 0) > time.time() - 7 * 86400]
        if len(done) < 3:
            return None
        self.data["last_recap"] = week
        self._save()
        titles = "; ".join(t["title"] for t in done[:25])
        return self.add_card("weekly_recap", "Your week with LocalAgent",
                             f"Write a short recap of what I got done with LocalAgent this week, grouped by theme, "
                             f"plus 2 things worth doing next week. Tasks finished: {titles}",
                             why=f"{len(done)} tasks finished this week.", sig=f"recap:{week}", source="brief")

    def _alert_new(self) -> None:
        """Ping the person about new suggestions, at most a few times a day and never in quiet hours."""
        if self.quiet():
            return
        today = self.now().date().isoformat()
        sent = self.data["alerts"].get(today, 0)
        fresh = [c for c in self.data["cards"] if c["status"] == "new" and not c.get("alerted")]
        if not fresh or sent >= self.settings()["max_alerts_per_day"]:
            return
        for c in fresh:
            c["alerted"] = True
        self.data["alerts"] = {today: sent + 1}
        self._save()
        title = fresh[0]["title"] if len(fresh) == 1 else f"{len(fresh)} things I can do for you"
        body = "\n".join("• " + c["title"] for c in fresh[:5]) + "\n\nOpen For you to say yes or no."
        self.owner.notify(title, body, source="for you", important=False)

    def start(self, every_seconds: float = 600) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()

        def loop():
            if self._stop.wait(90):   # let the app finish starting first
                return
            while True:
                try:
                    self.tick()
                except Exception:
                    pass
                if self._stop.wait(every_seconds):
                    return
        self.thread = threading.Thread(target=loop, daemon=True, name="la-proactive")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        s = self.settings()
        kinds = {k: {"auto": self.is_auto(k), "never": self.blocked(k), "yes": v.get("yes", 0), "no": v.get("no", 0)}
                 for k, v in self.data["feedback"].items()}
        for k in DEFAULT_AUTO:
            kinds.setdefault(k, {"auto": self.is_auto(k), "never": self.blocked(k), "yes": 0, "no": 0})
        return {"settings": s, "profile": self.data["profile"], "areas": AREAS, "kinds": kinds,
                "thinking": self.thinking, "cards": self.cards()}


IDEAS_PROMPT = """You help a busy person who doesn't know what to ask an AI assistant. Suggest exactly 3 concrete jobs
the assistant could do for them right now on their computer. It can: read and organize files, research on the
web, read documents, write drafts, run small programs, use connected apps, and run things on a schedule.

Rules:
- Specific to what you know about them. No generic tips. If you know very little, suggest simple, broadly useful jobs.
- Each job must be finishable by the assistant alone, without the person's help.
- Never suggest sending, posting, buying or deleting anything. Drafts are fine.
- Don't repeat or reword anything already suggested or already automated; pick other topics.
- The 3 ideas must be about 3 different things (different areas).
- "when" is only for things worth repeating, written like "weekdays 8am", "every monday 9am", "daily 18:00". Otherwise "".
- "area" is one of: email, files, research, writing, code, money, learning, health, news, general.

Answer with JSON only:
{"ideas": [{"title": "short offer, under 70 characters, phrased as a question or offer",
            "why": "one sentence on why this helps them",
            "task": "full instructions for the assistant",
            "when": "",
            "area": "general"}]}"""


def parse_ideas(text: str) -> list[dict]:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        items = data.get("ideas", []) if isinstance(data, dict) else []
    except json.JSONDecodeError:
        items = _salvage(m.group(0))   # small models slip up; keep the ideas that are readable
    out = []
    for i in items:
        if not isinstance(i, dict) or not str(i.get("title", "")).strip() or not str(i.get("task", "")).strip():
            continue
        task = str(i["task"]).strip()
        if re.search(r"\b(delete|erase|wipe|purchase|buy|pay|send|post|publish|tweet)\b", task, re.I) \
                and not re.search(r"\bdraft\b", task, re.I):
            continue  # belt and braces: ideas never send, buy or delete
        when = str(i.get("when") or "").strip()
        if when:
            try:
                from .automations import parse_when
                parse_when(when)
            except Exception:
                when = ""
        out.append({"title": str(i["title"]).strip()[:120], "why": str(i.get("why", "")).strip()[:300],
                    "task": task[:2000], "when": when, "area": str(i.get("area") or "general").strip().lower()[:20]})
    return out


def _salvage(text: str) -> list[dict]:
    """Pull ideas out of almost-JSON, one at a time, so one typo doesn't lose all three."""
    out = []
    starts = [m.start() for m in re.finditer(r'"title"\s*:', text)]
    for k, st in enumerate(starts):
        chunk = text[st: starts[k + 1] if k + 1 < len(starts) else len(text)]
        item = {}
        for key in ("title", "why", "task", "when", "area"):
            m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, chunk)
            if m:
                try:
                    item[key] = json.loads('"' + m.group(1) + '"')
                except json.JSONDecodeError:
                    item[key] = m.group(1)
        if item.get("title") and item.get("task"):
            out.append(item)
    return out
