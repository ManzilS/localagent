"""Connections: your email, calendars and notes, plus hosted apps (Notion,
Asana, Linear, Zapier, …) through MCP.

Config lives under `connections:` in config.yaml (no secrets there);
passwords, calendar links and sign-in tokens live in the OS keychain.

The agent gets tools for each kind only when something of that kind is
connected, so a small local model isn't distracted by tools it can't use.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta
from email.utils import getaddresses
from pathlib import Path

from .secrets import Secrets

KINDS = ("email", "calendar", "notes", "storage", "feeds", "weather", "web", "passwords")


def _make(cid: str, spec: dict, secrets, home: Path | None = None, cfg=None):
    t = spec.get("type")
    if t == "storage":
        from .storage_conn import StorageConnection
        return StorageConnection(cid, spec, secrets, home or Path.home() / ".localagent")
    if t == "feeds":
        from .extras import FeedsConnection
        return FeedsConnection(cid, spec, secrets)
    if t == "weather":
        from .extras import WeatherConnection
        return WeatherConnection(cid, spec, secrets)
    if t == "web":
        from .web_conn import WebConnection
        return WebConnection(cid, spec, secrets)
    if t == "passwords":
        from .passwords_conn import PasswordConnection
        return PasswordConnection(cid, spec, secrets)
    if t == "google":
        from .google_conn import GoogleAccount
        return GoogleAccount(cid, spec, secrets, cfg)
    if t == "email":
        from .email_conn import EmailConnection
        return EmailConnection(cid, spec, secrets)
    if t == "calendar":
        from .calendar_conn import CalendarConnection
        return CalendarConnection(cid, spec, secrets)
    if t == "notes":
        from .notes_conn import NotesConnection
        return NotesConnection(cid, spec, secrets)
    raise ValueError(f"unknown connection type {t!r}")


class ConnectionManager:
    def __init__(self, cfg, save_patch=None, secrets: Secrets | None = None):
        self.cfg = cfg
        self.save_patch = save_patch          # fn(dict) that writes config.yaml
        self.secrets = secrets or Secrets(cfg.home)
        self._lock = threading.RLock()
        self._live: dict[str, object] = {}
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------ store
    def specs(self) -> dict[str, dict]:
        return {k: v for k, v in (self.cfg.data.get("connections") or {}).items() if isinstance(v, dict)}

    def _write(self, specs: dict) -> None:
        self.cfg.data["connections"] = specs
        if self.save_patch:
            self.save_patch({"connections": specs})
        self._live.clear()

    def get(self, cid: str):
        with self._lock:
            if cid not in self._live:
                spec = self.specs().get(cid)
                if spec is None:
                    raise KeyError(cid)
                self._live[cid] = _make(cid, spec, self.secrets, self.cfg.home, self.cfg)
            return self._live[cid]

    def of_kind(self, kind: str) -> list:
        out = [self.get(cid) for cid, s in self.specs().items() if s.get("type") == kind and s.get("enabled", True)]
        if kind in ("email", "calendar", "storage"):     # a Google account is also an email, calendar and storage account
            for cid, s in self.specs().items():
                if s.get("type") == "google" and s.get("enabled", True):
                    a = self.get(cid).adapters().get(kind)
                    if a is not None:
                        out.append(a)
        return out

    def google_accounts(self, service: str = "") -> list:
        return [self.get(cid) for cid, s in self.specs().items() if s.get("type") == "google" and s.get("enabled", True)
                and (not service or service in (s.get("services") or []))]

    def save_google(self, info: dict) -> dict:
        """Create or update a Google connection from a finished sign-in."""
        with self._lock:
            specs = self.specs()
            cid = info.get("cid") or next((k for k, s in specs.items() if s.get("type") == "google" and s.get("email") == info["email"]), "")
            cid = cid or f"google-{uuid.uuid4().hex[:6]}"
            old = specs.get(cid, {})
            services = sorted(set(old.get("services") or []) | set(info["services"])) if info.get("cid") else info["services"]
            specs[cid] = {"type": "google", "enabled": True, "email": info["email"], "name": f"Google ({info['email']})",
                          "services": services, "added": old.get("added") or datetime.now().isoformat(timespec="minutes")}
            self._write(specs)
        acct = self.get(cid)
        tokens = dict(info["tokens"])
        if not tokens.get("refresh_token"):                 # Google only sends it the first time; keep the old one
            tokens["refresh_token"] = acct._tokens().get("refresh_token", "")
        acct.save_tokens(tokens)
        return {"id": cid, "name": specs[cid]["name"], "services": services}

    def pick(self, kind: str, which: str = ""):
        conns = self.of_kind(kind)
        if not conns:
            raise RuntimeError(f"no {kind} connected. Connect one on the Connections page.")
        if which:
            w = which.lower()
            for c in conns:
                if w in (c.id.lower(), c.label.lower()) or w in c.label.lower() or w in getattr(c, "address", "").lower():
                    return c
        return conns[0]

    # ------------------------------------------------------------ manage
    def add(self, ctype: str, spec: dict, secret: dict | None = None, test: bool = True) -> dict:
        if ctype not in KINDS:
            raise ValueError(f"unknown connection type {ctype}")
        cid = f"{ctype}-{uuid.uuid4().hex[:6]}"
        spec = {k: v for k, v in spec.items() if v not in (None, "")}
        spec.update({"type": ctype, "enabled": True, "added": datetime.now().isoformat(timespec="minutes")})
        for k, v in (secret or {}).items():
            if v:
                self.secrets.set(f"{cid}:{k}", str(v))
        if ctype == "storage" and spec.get("mode") == "rclone":
            from .storage_conn import PROVIDERS, Rclone, install_rclone
            prov = PROVIDERS.get(spec.get("provider", ""))
            if not prov:
                raise ValueError(f"unknown storage service {spec.get('provider')!r}")
            install_rclone(self.cfg.home)
            spec["remote"] = cid.replace("-", "_")
            params = dict(prov["params"])
            if prov["auth"] == "password":   # passed to rclone once (it stores them obscured in LocalAgent's rclone.conf)
                params.update({"user": (secret or {}).get("user", ""), "pass": (secret or {}).get("password", "")})
                if (secret or {}).get("code"):
                    params["2fa"] = secret["code"]
                for k in ("user", "password", "code"):
                    self.secrets.forget(f"{cid}:{k}")
            Rclone(self.cfg.home).create(spec["remote"], spec["provider"], params)   # browser sign-in happens here
            spec.setdefault("service", prov["name"])
        if ctype == "weather" and not spec.get("lat"):
            from .extras import WeatherConnection
            spec.update(WeatherConnection.geocode(spec.get("place", "")))
        conn = _make(cid, spec, self.secrets, self.cfg.home)
        if test:
            try:
                msg = conn.test()
            except Exception as e:
                for k in (secret or {}):
                    self.secrets.forget(f"{cid}:{k}")
                raise RuntimeError(str(e)) from None
        else:
            msg = "added"
        spec["name"] = conn.label
        with self._lock:
            specs = self.specs()
            specs[cid] = spec
            self._write(specs)
        return {"id": cid, "name": conn.label, "message": msg}

    def remove(self, cid: str) -> None:
        with self._lock:
            specs = self.specs()
            spec = specs.pop(cid, None)
            if spec is None:
                raise KeyError(cid)
            for k in ("password", "url", "token", "client_id", "client_secret"):
                self.secrets.forget(f"{cid}:{k}")
            if spec.get("type") == "google":
                from .google_conn import GoogleAccount
                GoogleAccount(cid, spec, self.secrets, self.cfg).revoke()   # also tells Google to drop LocalAgent's access
            if spec.get("type") == "storage" and spec.get("remote"):
                from .storage_conn import Rclone
                try:
                    Rclone(self.cfg.home).remove_remote(spec["remote"])   # forgets the sign-in only; files untouched
                except Exception:
                    pass
            self._write(specs)

    def set_enabled(self, cid: str, on: bool) -> None:
        with self._lock:
            specs = self.specs()
            specs[cid]["enabled"] = bool(on)
            self._write(specs)

    def test(self, cid: str) -> str:
        try:
            msg = self.get(cid).test()
            self.errors.pop(cid, None)
            return msg
        except Exception as e:
            self.errors[cid] = str(e)
            raise

    def status(self) -> list[dict]:
        out = []
        for cid, s in self.specs().items():
            out.append({"id": cid, "type": s.get("type"), "name": s.get("name") or cid, "enabled": s.get("enabled", True),
                        "app": s.get("app") or s.get("preset") or s.get("provider") or "", "error": self.errors.get(cid, ""),
                        "detail": s.get("address") or s.get("path") or s.get("url") or
                                  (", ".join(x.capitalize() for x in s.get("services") or []) if s.get("type") == "google" else ""),
                        "signed_in": s.get("signed_in", True), "catalog": s.get("catalog", ""),
                        "locked": (s.get("type") == "passwords" and s.get("provider") == "bitwarden"
                                   and not self.get(cid).unlocked)})
        return out

    def detected(self) -> list[dict]:
        """Things on this computer that could be connected in one click."""
        from .notes_conn import detect
        from .storage_conn import detect_mounts
        have = {str(Path(s.get("path", ""))).lower() for s in self.specs().values() if s.get("path")}
        found = []
        for fn in (detect, detect_mounts):
            try:
                found += fn()
            except Exception:
                pass
        return [d for d in found if str(Path(d["path"])).lower() not in have]

    def kinds_present(self) -> set[str]:
        return {s.get("type") for s in self.specs().values() if s.get("enabled", True)}

    def summary(self) -> str:
        parts = []
        for kind in KINDS:
            names = [c.label for c in self.of_kind(kind)] if kind in self.kinds_present() else []
            if names:
                label = {"web": "websites signed in (use open_website)",
                         "passwords": "password managers (sign in on sites with browser action 'login')"}.get(kind, kind)
                parts.append(f"{label}: {', '.join(names)}")
        return "; ".join(parts)


# ------------------------------------------------------------------ tools
def _account_prop(kind: str) -> dict:
    return {"type": "string", "description": f"Which {kind} account, if more than one is connected (name or address). Optional."}


def registry_tools(rt) -> list:
    from ..tools import Tool
    mgr: ConnectionManager | None = getattr(rt, "connections", None)
    if mgr is None:
        return []
    tools: list = []
    T = lambda name, desc, props, req, fn: tools.append(
        Tool(name, desc, {"type": "object", "properties": props, "required": req}, fn, group="connections"))

    if mgr.of_kind("email"):
        accts = ", ".join(c.label for c in mgr.of_kind("email"))

        def fmt_list(items):
            if not items:
                return "No emails found."
            return "\n".join(f"[{i['id']}] {'● ' if i['unread'] else ''}{i['date']} — {i['from']} — {i['subject']}" for i in items)

        T("email_list", f"List recent emails in the inbox, newest first. Connected: {accts}.",
          {"unread_only": {"type": "boolean"}, "days": {"type": "integer", "description": "How far back (default 7)"},
           "limit": {"type": "integer"}, "account": _account_prop("email")}, [],
          lambda rt_, unread_only=False, days=7, limit=15, account="":
              fmt_list(mgr.pick("email", account).recent(limit=int(limit), unread_only=bool(unread_only), days=int(days))))
        T("email_search", "Search all email (sender, subject or words in the message). Gmail search operators work on Gmail.",
          {"query": {"type": "string"}, "limit": {"type": "integer"}, "account": _account_prop("email")}, ["query"],
          lambda rt_, query, limit=15, account="": fmt_list(mgr.pick("email", account).search(query, limit=int(limit))))

        def read(rt_, id, account=""):
            m = mgr.pick("email", account).read(id)
            att = f"\nAttachments: {', '.join(m['attachments'])}" if m["attachments"] else ""
            return f"From: {m['from']}\nTo: {m['to']}\n" + (f"Cc: {m['cc']}\n" if m["cc"] else "") + \
                   f"Date: {m['date']}\nSubject: {m['subject']}{att}\n\n{m['body']}"
        T("email_read", "Read one email by its id (from email_list or email_search). Doesn't mark it as read.",
          {"id": {"type": "string"}, "account": _account_prop("email")}, ["id"], read)

        def save_att(rt_, id, name="", account=""):
            dest = mgr.pick("email", account).save_attachment(id, name, Path(rt_.cfg.workspace) / "email attachments")
            return f"Saved to {dest}"
        T("email_save_attachment", "Save an email attachment into the workspace so you can read it.",
          {"id": {"type": "string"}, "name": {"type": "string", "description": "Attachment file name (blank = first)"},
           "account": _account_prop("email")}, ["id"], save_att)

        def draft(rt_, body, to="", subject="", cc="", reply_to_id="", account=""):
            c = mgr.pick("email", account)
            msg = c.compose(to, subject, body, cc=cc, reply_to_id=reply_to_id)
            folder = c.save_draft(msg)
            return f"Draft saved in {c.address}'s {folder} folder: to {msg['To']}, subject “{msg['Subject']}”. " \
                   "Nothing was sent; the user can review and send it from their email app."
        T("email_draft", "Write an email and save it to the Drafts folder (NOT sent). Preferred way to prepare replies. "
          "Use reply_to_id to reply in the same thread.",
          {"body": {"type": "string"}, "to": {"type": "string"}, "subject": {"type": "string"}, "cc": {"type": "string"},
           "reply_to_id": {"type": "string"}, "account": _account_prop("email")}, ["body"], draft)

        def send(rt_, body, to="", subject="", cc="", reply_to_id="", account=""):
            c = mgr.pick("email", account)
            msg = c.compose(to, subject, body, cc=cc, reply_to_id=reply_to_id)
            preview = f"send an email from {c.address} to {msg['To']}" + (f" (cc {cc})" if cc else "") + \
                      f" — subject “{msg['Subject']}”:\n\n{body[:600]}"
            if not rt_.request_approval(preview):
                return "Not sent: the user didn't approve. Offer to save it as a draft instead."
            c.send(msg)
            return f"Sent to {msg['To']}."
        T("email_send", "Send an email right away. Always asks the user first. Prefer email_draft unless they asked you to send.",
          {"body": {"type": "string"}, "to": {"type": "string"}, "subject": {"type": "string"}, "cc": {"type": "string"},
           "reply_to_id": {"type": "string"}, "account": _account_prop("email")}, ["body"], send)

    if mgr.of_kind("calendar"):
        from .calendar_conn import add_event_link, describe, write_ics
        cals = ", ".join(c.label for c in mgr.of_kind("calendar"))

        def events(rt_, days=7, start="today", query=""):
            st = _parse_day(start)
            evs = []
            for c in mgr.of_kind("calendar"):
                try:
                    evs += c.events(st, float(days), query)
                except Exception as e:
                    evs.append({"error": f"{c.label}: {e}"})
            errs = [e["error"] for e in evs if "error" in e]
            evs = sorted((e for e in evs if "error" not in e), key=lambda e: e["start"])
            text = "\n".join(describe(e) + (f"\n    {e['description'][:300]}" if e["description"] else "") for e in evs) or "No events."
            return text + ("\n\nProblems: " + "; ".join(errs) if errs else "")
        T("calendar_events", f"List calendar events. Connected calendars: {cals}.",
          {"days": {"type": "number", "description": "How many days to show (default 7)"},
           "start": {"type": "string", "description": "today, tomorrow, or YYYY-MM-DD"},
           "query": {"type": "string", "description": "Only events containing this text"}}, [], events)

        def add_event(rt_, title, start, duration_minutes=60, details="", location="", invite=""):
            st = datetime.fromisoformat(start)
            if st.tzinfo is None:
                st = st.astimezone()
            en = st + timedelta(minutes=float(duration_minutes))
            gcal = next((c for c in mgr.of_kind("calendar") if hasattr(c, "insert_event")), None)
            if gcal is not None:          # signed in with Google: put it on the calendar for real
                people = [a for _, a in getaddresses([invite]) if a] if invite else []
                if people and not rt_.request_approval(f"add “{title}” on {st:%a %b %d %H:%M} to your Google Calendar and email an "
                                                       f"invitation to {', '.join(people)}"):
                    return "Not added: the user didn't approve sending invitations. Offer to add it without inviting anyone."
                ev = gcal.insert_event(title, st, en, details, location, people)
                return f"Added to {gcal.label}: {title}, {st:%a %b %d %H:%M}–{en:%H:%M}." + (f" Invitations sent to {', '.join(people)}." if people else "") + \
                    (f" {ev.get('htmlLink')}" if ev.get("htmlLink") else "")
            prov = mgr.of_kind("calendar")[0].spec.get("provider", "google")
            link = add_event_link(prov, title, st, en, details, location)
            ics = write_ics(Path(rt_.cfg.workspace) / f"{re.sub(r'[^\w-]+', '_', title)[:40]}.ics", title, st, en, details, location)
            return (f"Prepared (not yet on the calendar). Give the user this link — it opens their calendar with the event "
                    f"filled in and they press Save:\n{link}\nAlso saved {ics} which any calendar app can open.")
        T("calendar_add_event", "Add an event to the user's calendar (Google: added directly; other calendars: a link they press Save on). "
          "Start like 2026-11-02T15:00. invite = email addresses to invite (asks the user first).",
          {"title": {"type": "string"}, "start": {"type": "string"}, "duration_minutes": {"type": "number"},
           "details": {"type": "string"}, "location": {"type": "string"}, "invite": {"type": "string"}}, ["title", "start"], add_event)

    if mgr.of_kind("notes"):
        srcs = ", ".join(c.label for c in mgr.of_kind("notes"))
        src_prop = {"type": "string", "description": f"Which notes app/vault: {srcs}. Optional."}

        def each(source):
            return [mgr.pick("notes", source)] if source else mgr.of_kind("notes")

        def nsearch(rt_, query, source="", limit=8):
            out = []
            for c in each(source):
                try:
                    out += [f"[{c.label}] {h['id']} — {h['title']}: {h['snippet']}" for h in c.search(query, int(limit))]
                except Exception as e:
                    out.append(f"[{c.label}] error: {e}")
            return "\n".join(out) or "No matching notes."
        T("notes_search", f"Search the user's notes. Connected: {srcs}.",
          {"query": {"type": "string"}, "source": src_prop, "limit": {"type": "integer"}}, ["query"], nsearch)

        def nread(rt_, id, source=""):
            for c in each(source):
                try:
                    n = c.read(id)
                    return f"[{c.label}] {n['title']}\n\n{n['text']}"
                except (FileNotFoundError, PermissionError, OSError):
                    continue
            return f"ERROR: no note {id!r}"
        T("notes_read", "Read a note by id (from notes_search / notes_recent).", {"id": {"type": "string"}, "source": src_prop}, ["id"], nread)

        def nrecent(rt_, source="", limit=10):
            out = []
            for c in each(source):
                out += [f"[{c.label}] {n['id']} — {n['title']} ({datetime.fromtimestamp(n['updated']):%b %d})" for n in c.recent(int(limit))]
            return "\n".join(out) or "No notes."
        T("notes_recent", "List recently changed notes.", {"source": src_prop, "limit": {"type": "integer"}}, [], nrecent)

        def ntodos(rt_, source=""):
            out = []
            for c in each(source):
                for t in c.open_todos():
                    due = f" (due {datetime.fromtimestamp(t['due']):%b %d})" if t.get("due") else ""
                    out.append(f"[{c.label} · {t['note']}] {t['text']}{due}")
            return "\n".join(out) or "No open to-dos in the notes."
        T("notes_todos", "List unchecked to-dos found in the user's notes.", {"source": src_prop}, [], ntodos)

        def nadd(rt_, title, text, source="", folder=""):
            c = mgr.pick("notes", source)
            return f"Created note {c.add(title, text, folder=folder, trash=rt_.trash)} in {c.label}."
        T("notes_add", "Create a new note (never overwrites an existing one).",
          {"title": {"type": "string"}, "text": {"type": "string"}, "source": src_prop,
           "folder": {"type": "string", "description": "Subfolder inside the vault (optional)"}}, ["title", "text"], nadd)

        def nappend(rt_, id, text, source=""):
            c = mgr.pick("notes", source)
            return f"Added to {c.append(id, text, trash=rt_.trash)} (the previous version is kept in the trash)."
        T("notes_append", "Add text to the end of an existing note.",
          {"id": {"type": "string"}, "text": {"type": "string"}, "source": src_prop}, ["id", "text"], nappend)
    storages = mgr.of_kind("storage")
    if storages:
        from .storage_conn import human, local_drives
        names = ", ".join(c.label for c in storages)
        acct = {"type": "string", "description": f"Which storage: {names}. Optional when only one."}

        def overview(rt_):
            lines = ["Cloud & synced storage:"]
            for c in storages:
                try:
                    a = c.about()
                    if a.get("synced_folder"):
                        lines.append(f"  {c.label}: a synced folder on this computer (its cloud space isn't visible from here)")
                        continue
                    pct = f" — {100 * a['used'] / a['total']:.0f}% full" if a["total"] else ""
                    lines.append(f"  {c.label}: {human(a['used'])} used" + (f" of {human(a['total'])}, {human(a['free'])} free{pct}" if a["total"] else "")
                                 + (f", {human(a['trashed'])} in its trash" if a.get("trashed") else ""))
                except Exception as e:
                    lines.append(f"  {c.label}: couldn't check ({e})")
            lines.append("Drives on this computer:")
            for d in local_drives():
                lines.append(f"  {d['name']} {d['label']}: {human(d['used'])} used of {human(d['total'])}, {human(d['free'])} free "
                             f"({100 * d['used'] / d['total']:.0f}% full)")
            return "\n".join(lines)
        T("storage_overview", f"How full each cloud storage account and each drive on this computer is. Connected: {names}.", {}, [], overview)

        def fmt(items, c=None):
            body = "\n".join(f"{i['path']}  {human(i['size'])}  {datetime.fromtimestamp(i['modified']):%Y-%m-%d}" for i in items) or "Nothing found."
            if c is not None and getattr(c, "partial", False):
                body += "\n(This storage is big, so the scan stopped early — these are from the part that was checked. Narrow it with a folder path.)"
            return body

        def slist(rt_, path="", account=""):
            items = mgr.pick("storage", account).list(path)
            return "\n".join(f"{'📁 ' if i['dir'] else ''}{i['path']}" + ("" if i["dir"] else f"  {human(i['size'])}") for i in items) or "Empty."
        T("storage_list", "List a folder in cloud storage.", {"path": {"type": "string"}, "account": acct}, [], slist)
        T("storage_search", "Find files by name in cloud storage (wildcards like *.pdf work).",
          {"name": {"type": "string"}, "account": acct}, ["name"],
          lambda rt_, name, account="": (lambda c: fmt(c.search(name), c))(mgr.pick("storage", account)))
        T("storage_big_files", "The biggest files in a storage account — the place to start when freeing up space.",
          {"min_mb": {"type": "number", "description": "Only files at least this many MB (default 100)"},
           "path": {"type": "string"}, "account": acct}, [],
          lambda rt_, min_mb=100, path="", account="": (lambda c: fmt(c.big_files(float(min_mb), path=path), c))(mgr.pick("storage", account)))

        def sdownload(rt_, path, account=""):
            return f"Copied to {mgr.pick('storage', account).download(path, Path(rt_.cfg.workspace) / 'downloads from cloud')}"
        T("storage_download", "Copy a file from cloud storage into the workspace (e.g. to read it).",
          {"path": {"type": "string"}, "account": acct}, ["path"], sdownload)

        def supload(rt_, local_path, dest="", account=""):
            from ..safety import resolve_path
            src = resolve_path(rt_.cfg, local_path)
            if not src.exists():
                return f"ERROR: {local_path} doesn't exist"
            c = mgr.pick("storage", account)
            return f"Copied to {c.label}: {c.upload(src, dest or src.name)} (the local file is unchanged)."
        T("storage_upload", "Copy a local file or folder into cloud storage. Never replaces anything there.",
          {"local_path": {"type": "string"}, "dest": {"type": "string", "description": "Path in the storage (default: same name at the top)"},
           "account": acct}, ["local_path"], supload)

        def smove(rt_, src, dest, account=""):
            c = mgr.pick("storage", account)
            return f"Moved {src} → {c.move(src, dest)} in {c.label}."
        T("storage_move", "Move or rename something inside one storage account (never overwrites).",
          {"src": {"type": "string"}, "dest": {"type": "string"}, "account": acct}, ["src", "dest"], smove)

        def strash(rt_, path, account=""):
            c = mgr.pick("storage", account)
            return f"Moved to {c.trash(path)} in {c.label}. Nothing was deleted; the user can move it back or empty agent_trash themselves."
        T("storage_trash", "'Remove' something from cloud storage by moving it into that storage's agent_trash folder.",
          {"path": {"type": "string"}, "account": acct}, ["path"], strash)

        def offload(rt_, local_path, dest="", account=""):
            from ..safety import resolve_path
            src = resolve_path(rt_.cfg, local_path)
            if not src.is_file():
                return f"ERROR: {local_path} isn't a file"
            c = mgr.pick("storage", account)
            size = human(src.stat().st_size)
            if not rt_.request_approval(f"move {src.name} ({size}) off this computer into {c.label}"
                                        f"{' at ' + dest if dest else ''}: copy it there, check the copy, then put the local file in LocalAgent's trash"):
                return "Not done: the user didn't approve."
            where = c.upload(src, dest or f"From my PC/{src.name}")
            if not c.verify(src, where):
                return f"ERROR: the copy in {c.label} ({where}) didn't match the original, so the local file was left where it was."
            rt_.trash.move(str(src), reason=f"offloaded: verified copy in {c.label}: {where}")
            return (f"{src.name} is now in {c.label} at {where} (copy verified). The local file is in LocalAgent's trash; "
                    f"{size} is freed on this computer once the user empties the trash.")
        T("storage_offload", "Free space on this computer: move a big local file to cloud storage (verified copy; asks first).",
          {"local_path": {"type": "string"}, "dest": {"type": "string"}, "account": acct}, ["local_path"], offload)

    if mgr.of_kind("feeds"):
        fc = mgr.of_kind("feeds")[0]

        def news(rt_, limit=12, query="", feed=""):
            items, errs = fc.latest(int(limit), query, feed)
            body = "\n".join(f"- [{i['source']}] {i['title']} ({datetime.fromtimestamp(i['time']):%b %d %H:%M})\n  {i['link']}"
                             + (f"\n  {i['summary'][:200]}" if i["summary"] else "") for i in items) or "No stories."
            return body + (f"\n\nCouldn't read: {'; '.join(errs)}" if errs else "")
        T("news_latest", f"Latest stories from the user's news feeds ({', '.join(fc.feeds)}).",
          {"limit": {"type": "integer"}, "query": {"type": "string", "description": "Only stories mentioning this"},
           "feed": {"type": "string", "description": "Only this feed"}}, [], news)

    g_tasks, g_people = mgr.google_accounts("tasks"), mgr.google_accounts("contacts")
    if g_tasks:
        from .google_conn import tasks_add, tasks_complete, tasks_list
        ga = g_tasks[0]
        T("tasks_list", f"The user's Google Tasks ({ga.address}).", {"include_done": {"type": "boolean"}}, [],
          lambda rt_, include_done=False: "\n".join(f"[{t['list']}] {'✓ ' if t['done'] else ''}{t['title']}" + (f" (due {t['due']})" if t["due"] else "")
                                                     + (f" — {t['notes'][:120]}" if t["notes"] else "") for t in tasks_list(ga, bool(include_done))) or "No tasks.")
        T("tasks_add", "Add a to-do to Google Tasks.", {"title": {"type": "string"}, "notes": {"type": "string"},
          "due": {"type": "string", "description": "YYYY-MM-DD"}, "list": {"type": "string"}}, ["title"],
          lambda rt_, title, notes="", due="", list="": f"Added “{title}” to {tasks_add(ga, title, notes, due, list)}.")
        T("tasks_complete", "Mark a Google Tasks to-do as done (by its title).", {"title": {"type": "string"}}, ["title"],
          lambda rt_, title: f"Marked “{tasks_complete(ga, title)}” as done.")
    if g_people:
        from .google_conn import contacts_search
        gp = g_people[0]
        T("contacts_search", f"Look someone up in the user's Google Contacts ({gp.address}).", {"name": {"type": "string"}}, ["name"],
          lambda rt_, name: "\n".join(f"{c['name']}: {', '.join(c['emails']) or 'no email'}" + (f"; {', '.join(c['phones'])}" if c["phones"] else "")
                                       + (f" ({c['org']})" if c["org"] else "") for c in contacts_search(gp, name)) or "Nobody found.")
    drives = [c for c in mgr.of_kind("storage") if hasattr(c, "read_text")]
    if drives:
        T("drive_read", "Read a Google Doc, Sheet (as CSV), Slides or text file from Google Drive by path (e.g. 'School/Essay').",
          {"path": {"type": "string"}}, ["path"], lambda rt_, path: drives[0].read_text(path)[:30000])

    webs = mgr.of_kind("web")
    if webs:
        listing = "; ".join(f"{w.label} ({w.url})" for w in webs)

        def open_site(rt_, site, page=""):
            from ..tools.browser import run
            w = next((x for x in webs if site.lower() in x.label.lower() or site.lower() in x.url.lower()), None)
            if w is None:
                return f"ERROR: no website connection called {site!r}. Connected: {listing}. Any other site: use the browser tool."
            url = w.url.rstrip("/") + "/" + page.lstrip("/") if page and not page.startswith("http") else (page or w.url)
            opened = run(rt_, "goto", url)
            text = run(rt_, "read")
            tips = f"\nHow this site works: {w.tips}" if w.tips else ""
            return f"{opened}{tips}\n\n{text}\n\n(Continue with the browser tool: snapshot to see clickable things, then click/fill.)"
        T("open_website", f"Open one of the user's signed-in websites in LocalAgent's browser and read it. Use this for services "
          f"that have no connector. Connected: {listing}.",
          {"site": {"type": "string"}, "page": {"type": "string", "description": "Optional path or full URL on that site"}},
          ["site"], open_site)

    if mgr.of_kind("weather"):
        wc = mgr.of_kind("weather")[0]
        T("weather", f"Weather now and the next days. Default place: {wc.spec.get('place', '')}.",
          {"place": {"type": "string", "description": "Another city (optional)"}, "days": {"type": "integer"}}, [],
          lambda rt_, place="", days=3: wc.forecast(place, int(days)))
    return tools


def _parse_day(s: str) -> datetime:
    s = (s or "today").strip().lower()
    base = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("today", "now", ""):
        return base
    if s == "tomorrow":
        return base + timedelta(days=1)
    if s == "yesterday":
        return base - timedelta(days=1)
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.astimezone()


UNTRUSTED = {"email_list": "email", "email_search": "email", "email_read": "an email",
             "calendar_events": "calendar invites", "news_latest": "news feeds", "open_website": "a website",
             "drive_read": "a Drive file"}
WRITES = {"notes_add": "create a note", "notes_append": "add to a note", "email_draft": None}
