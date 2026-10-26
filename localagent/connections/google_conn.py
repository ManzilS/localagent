"""Sign in with Google: Gmail, Calendar, Drive, Tasks and Contacts through
Google's own APIs — the same way Meta Muse or any email app connects.

How it works for the person: they press "Sign in with Google", pick their
account on Google's page, see what LocalAgent asks for, and press Allow.
LocalAgent never sees their password. They can remove access any time at
myaccount.google.com/permissions.

How it works underneath: OAuth for installed apps (loopback redirect + PKCE).
The app identity ("client") belongs to whoever publishes LocalAgent and is
the same for every user — users never create anything in Google Cloud.
It comes from config `google_oauth: {client_id, client_secret}`, the
LOCALAGENT_GOOGLE_CLIENT_ID/SECRET environment variables, or a
google_client.json bundled next to this package. (For installed apps Google
treats the client secret as non-confidential; it identifies the app, it
doesn't protect anyone's data.) Tokens are stored in the OS keychain.

Each Google service plugs into the tools LocalAgent already has: Gmail is an
"email" account (email_list, email_draft, morning brief, reply drafts…),
Google Calendar a "calendar", Drive a "storage" account (storage_overview,
big files, offload…). Tasks and Contacts get their own small tools.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import json
import os
import re
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SERVICES = {
    "gmail": {"name": "Gmail", "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
              "why": "read and search your email and save reply drafts (sending always asks you first)"},
    "calendar": {"name": "Calendar", "scopes": ["https://www.googleapis.com/auth/calendar"],
                 "why": "see your schedule and add events you approve"},
    "drive": {"name": "Drive", "scopes": ["https://www.googleapis.com/auth/drive"],
              "why": "find files, read Docs and Sheets, and help manage your storage (nothing is ever deleted)"},
    "tasks": {"name": "Tasks", "scopes": ["https://www.googleapis.com/auth/tasks"],
              "why": "see and add to-dos"},
    "contacts": {"name": "Contacts", "scopes": ["https://www.googleapis.com/auth/contacts.readonly"],
                 "why": "look up people's email addresses and numbers"},
}
BASE_SCOPES = ["openid", "email", "profile"]

PENDING: dict[str, dict] = {}
_lock = threading.Lock()
_http = httpx.Client(timeout=httpx.Timeout(60, connect=10))


# ------------------------------------------------------------------ client
def client_config(cfg) -> dict | None:
    c = dict((cfg.data.get("google_oauth") or {}))
    if not c.get("client_id"):
        c = {"client_id": os.environ.get("LOCALAGENT_GOOGLE_CLIENT_ID", ""),
             "client_secret": os.environ.get("LOCALAGENT_GOOGLE_CLIENT_SECRET", "")}
    if not c.get("client_id"):
        bundled = Path(__file__).resolve().parent.parent / "google_client.json"
        try:
            d = json.loads(bundled.read_text(encoding="utf-8"))
            d = d.get("installed") or d.get("web") or d       # accepts Google's downloaded JSON as-is
            c = {"client_id": d.get("client_id", ""), "client_secret": d.get("client_secret", "")}
        except (OSError, json.JSONDecodeError):
            pass
    return c if c.get("client_id") else None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start_signin(cfg, redirect_uri: str, services: list[str], store, login_hint: str = "", cid: str = "") -> str:
    client = client_config(cfg)
    if not client:
        raise RuntimeError("This copy of LocalAgent hasn't been registered with Google yet (no Google client set up).")
    services = [s for s in services if s in SERVICES] or list(SERVICES)
    scopes = BASE_SCOPES + [sc for s in services for sc in SERVICES[s]["scopes"]]
    verifier = _b64(pysecrets.token_bytes(48))
    state = pysecrets.token_urlsafe(24)
    q = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes),
         "state": state, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
    if login_hint:
        q["login_hint"] = login_hint
    with _lock:
        for k in [k for k, v in PENDING.items() if time.time() - v["time"] > 900]:
            PENDING.pop(k)
        PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "services": services, "time": time.time(),
                          "client": client, "store": store, "cid": cid}
    return AUTH_URL + "?" + urlencode(q)


def finish_signin(state: str, code: str, client: httpx.Client | None = None) -> dict:
    """Swap the code for tokens. Returns {email, name, services, granted, tokens}."""
    with _lock:
        flow = PENDING.pop(state, None)
    if flow is None:
        raise RuntimeError("This sign-in link expired or was already used. Start again from LocalAgent.")
    c = client or _http
    r = c.post(TOKEN_URL, data={"client_id": flow["client"]["client_id"], "client_secret": flow["client"].get("client_secret", ""),
                                "code": code, "code_verifier": flow["verifier"], "grant_type": "authorization_code",
                                "redirect_uri": flow["redirect_uri"]})
    if r.status_code >= 400:
        raise RuntimeError(f"Google didn't accept the sign-in ({r.text[:200]})")
    tok = r.json()
    granted = set((tok.get("scope") or "").split())
    who = c.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + tok["access_token"]}).json()
    services = [s for s in flow["services"] if all(sc in granted for sc in SERVICES[s]["scopes"])]
    return {"email": who.get("email", ""), "name": who.get("name", ""), "services": services,
            "declined": [s for s in flow["services"] if s not in services], "cid": flow["cid"], "store": flow["store"],
            "tokens": {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                       "expires_at": time.time() + float(tok.get("expires_in", 3600)), "scope": tok.get("scope", "")}}


# ----------------------------------------------------------------- account
class GoogleAccount:
    """One signed-in Google account. Hands out adapters for each service."""
    kind = "google"

    def __init__(self, cid: str, spec: dict, secrets, cfg=None, client: httpx.Client | None = None):
        self.id, self.spec, self.secrets, self.cfg = cid, spec, secrets, cfg
        self.address = spec.get("email", "")
        self.label = spec.get("name") or f"Google ({self.address})"
        self.http = client or _http
        self._tlock = threading.Lock()

    # ------------------------------------------------------------ tokens
    def _tokens(self) -> dict:
        try:
            return json.loads(self.secrets.get(f"{self.id}:google") or "{}")
        except json.JSONDecodeError:
            return {}

    def save_tokens(self, t: dict) -> None:
        self.secrets.set(f"{self.id}:google", json.dumps(t))

    def token(self, force: bool = False) -> str:
        with self._tlock:
            t = self._tokens()
            if not t:
                raise RuntimeError(f"{self.label} is signed out. Press Sign in with Google on the Connections page.")
            if force or t.get("expires_at", 0) - time.time() < 120:
                client = client_config(self.cfg) if self.cfg is not None else self.spec.get("_client")
                if not t.get("refresh_token") or not client:
                    raise RuntimeError(f"{self.label}: the Google sign-in expired. Sign in again on the Connections page.")
                r = self.http.post(TOKEN_URL, data={"client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                                                    "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
                if r.status_code >= 400:
                    raise RuntimeError(f"{self.label}: Google ended the sign-in ({r.json().get('error', r.status_code)}). "
                                       "Sign in again on the Connections page.")
                n = r.json()
                t.update(access_token=n["access_token"], expires_at=time.time() + float(n.get("expires_in", 3600)))
                self.save_tokens(t)
            return t["access_token"]

    def api(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in (0, 1):
            h = {"Authorization": "Bearer " + self.token(force=attempt == 1), **kw.pop("headers", {})}
            r = self.http.request(method, url, headers=h, **kw)
            if r.status_code != 401:
                break
        if r.status_code == 403 and "insufficient" in r.text.lower():
            raise PermissionError(f"{self.label}: you didn't give LocalAgent access to this part of Google. "
                                  "Sign in again and leave that box checked.")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:200])
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"Google said: {msg}")
        return r

    def j(self, method: str, url: str, **kw) -> dict:
        r = self.api(method, url, **kw)
        return r.json() if r.content else {}

    # ---------------------------------------------------------- services
    @property
    def services(self) -> list[str]:
        return list(self.spec.get("services") or [])

    def adapters(self) -> dict[str, object]:
        out = {}
        if "gmail" in self.services:
            out["email"] = GmailAdapter(self)
        if "calendar" in self.services:
            out["calendar"] = GCalendarAdapter(self)
        if "drive" in self.services:
            out["storage"] = DriveAdapter(self)
        return out

    def test(self) -> str:
        names = ", ".join(SERVICES[s]["name"] for s in self.services) or "nothing"
        return f"Signed in as {self.address}: {names}"

    def revoke(self) -> None:
        t = self._tokens()
        if t.get("refresh_token") or t.get("access_token"):
            try:
                self.http.post(REVOKE_URL, data={"token": t.get("refresh_token") or t["access_token"]})
            except httpx.HTTPError:
                pass
        self.secrets.forget(f"{self.id}:google")


# ------------------------------------------------------------------- gmail
GM = "https://gmail.googleapis.com/gmail/v1/users/me"


def _hdrs(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers") or []}


class GmailAdapter:
    """Looks like an email connection to the rest of LocalAgent."""
    kind = "email"

    def __init__(self, acct: GoogleAccount):
        self.acct = acct
        self.id = acct.id + ":gmail"
        self.address = acct.address
        self.label = f"Gmail ({acct.address})"
        self.spec = {"provider": "google"}

    def _summaries(self, ids: list[str]) -> list[dict]:
        out = []
        for mid in ids:
            m = self.acct.j("GET", f"{GM}/messages/{mid}", params=[("format", "metadata"), *[("metadataHeaders", h) for h in
                            ("From", "Subject", "Date", "List-Unsubscribe", "Precedence", "Auto-Submitted")]])
            h = _hdrs(m.get("payload") or {})
            ts = int(m.get("internalDate", 0)) / 1000
            labels = m.get("labelIds") or []
            out.append({"id": mid, "from": h.get("from", ""), "subject": h.get("subject", "") or "(no subject)",
                        "date": datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M") if ts else "", "ts": ts,
                        "unread": "UNREAD" in labels, "flagged": "STARRED" in labels,
                        "bulk": bool(h.get("list-unsubscribe") or h.get("precedence", "").lower() in ("bulk", "list", "junk")
                                     or h.get("auto-submitted", "no").lower() != "no"
                                     or any(l in labels for l in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"))
                                     or re.search(r"no-?reply|notifications?@|newsletter", h.get("from", ""), re.I))})
        return out

    def _ids(self, q: str, limit: int) -> list[str]:
        d = self.acct.j("GET", f"{GM}/messages", params={"q": q, "maxResults": max(1, min(int(limit), 50))})
        return [m["id"] for m in d.get("messages") or []]

    def recent(self, limit: int = 15, unread_only: bool = False, days: int = 14, folder: str = "INBOX") -> list[dict]:
        q = f"in:inbox newer_than:{int(days)}d" + (" is:unread" if unread_only else "")
        return self._summaries(self._ids(q, limit))

    def search(self, query: str, limit: int = 15, days: int = 365) -> list[dict]:
        return self._summaries(self._ids(f"{query} newer_than:{int(days)}d", limit))

    def _raw(self, mid: str) -> email.message.Message:
        d = self.acct.j("GET", f"{GM}/messages/{mid}", params={"format": "raw"})
        return email.message_from_bytes(base64.urlsafe_b64decode(d["raw"] + "=" * (-len(d["raw"]) % 4)), policy=email.policy.compat32)

    def read(self, mid: str) -> dict:
        from .email_conn import _decode, body_text
        msg = self._raw(mid)
        return {"id": mid, "from": _decode(msg.get("From")), "to": _decode(msg.get("To")), "cc": _decode(msg.get("Cc")),
                "subject": _decode(msg.get("Subject")), "date": msg.get("Date", ""), "message_id": msg.get("Message-ID", ""),
                "references": msg.get("References", ""), "attachments": [_decode(p.get_filename()) for p in msg.walk() if p.get_filename()],
                "body": body_text(msg)[:12000], "thread": self.acct.j("GET", f"{GM}/messages/{mid}", params={"format": "minimal"}).get("threadId", "")}

    def save_attachment(self, mid: str, name: str, dest_dir: Path) -> Path:
        from .email_conn import _decode
        msg = self._raw(mid)
        for p in msg.walk():
            fn = _decode(p.get_filename())
            if fn and (fn == name or not name):
                dest_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^\w.\- ]", "_", Path(fn).name) or "attachment"
                dest, i = dest_dir / safe, 1
                while dest.exists():
                    dest = dest_dir / f"{Path(safe).stem}_{i}{Path(safe).suffix}"
                    i += 1
                dest.write_bytes(p.get_payload(decode=True) or b"")
                return dest
        raise FileNotFoundError(f"no attachment named {name!r}")

    def compose(self, to: str, subject: str, body: str, cc: str = "", reply_to_id: str = "") -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.address
        thread = ""
        if reply_to_id:
            o = self.read(reply_to_id)
            to = to or parseaddr(o["from"])[1]
            subject = subject or (o["subject"] if o["subject"].lower().startswith("re:") else "Re: " + o["subject"])
            if o["message_id"]:
                msg["In-Reply-To"] = o["message_id"]
                msg["References"] = (o["references"] + " " + o["message_id"]).strip()
            thread = o.get("thread", "")
        msg["To"], msg["Subject"] = to, subject
        if cc:
            msg["Cc"] = cc
        msg["Message-ID"] = make_msgid(domain=self.address.rsplit("@", 1)[-1] or None)
        msg.set_content(body)
        msg._la_thread = thread  # type: ignore[attr-defined]
        return msg

    def _payload(self, msg: EmailMessage) -> dict:
        d = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if getattr(msg, "_la_thread", ""):
            d["threadId"] = msg._la_thread  # type: ignore[attr-defined]
        return d

    def save_draft(self, msg: EmailMessage) -> str:
        self.acct.j("POST", f"{GM}/drafts", json={"message": self._payload(msg)})
        return "Drafts"

    def send(self, msg: EmailMessage) -> None:
        self.acct.j("POST", f"{GM}/messages/send", json=self._payload(msg))

    def test(self) -> str:
        p = self.acct.j("GET", f"{GM}/profile")
        return f"Connected: {p.get('messagesTotal', 0)} messages in {p.get('emailAddress', self.address)}"


# ---------------------------------------------------------------- calendar
GC = "https://www.googleapis.com/calendar/v3"


class GCalendarAdapter:
    kind = "calendar"

    def __init__(self, acct: GoogleAccount):
        self.acct = acct
        self.id = acct.id + ":calendar"
        self.label = f"Google Calendar ({acct.address})"
        self.spec = {"provider": "google"}

    def events(self, start: datetime | None = None, days: float = 7, query: str = "") -> list[dict]:
        tz = datetime.now().astimezone().tzinfo
        start = start or datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        end = start + timedelta(days=days)
        cals = self.acct.j("GET", f"{GC}/users/me/calendarList", params={"minAccessRole": "reader"}).get("items") or []
        out = []
        for cal in cals:
            if cal.get("hidden") or cal.get("selected") is False:
                continue
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime",
                      "maxResults": 250}
            if query:
                params["q"] = query
            try:
                items = self.acct.j("GET", f"{GC}/calendars/{_q(cal['id'])}/events", params=params).get("items") or []
            except RuntimeError:
                continue
            for ev in items:
                if ev.get("status") == "cancelled":
                    continue
                s, e = ev.get("start") or {}, ev.get("end") or {}
                all_day = "date" in s
                sd = datetime.fromisoformat((s.get("dateTime") or s.get("date")).replace("Z", "+00:00"))
                ed = datetime.fromisoformat((e.get("dateTime") or e.get("date") or s.get("dateTime") or s.get("date")).replace("Z", "+00:00"))
                sd = sd.astimezone(tz) if sd.tzinfo else sd.replace(tzinfo=tz)     # all-day dates have no time zone
                ed = ed.astimezone(tz) if ed.tzinfo else ed.replace(tzinfo=tz)
                out.append({"title": ev.get("summary", "(no title)"), "start": sd, "end": ed, "all_day": all_day,
                            "location": ev.get("location", ""), "description": (ev.get("description") or "")[:1500],
                            "calendar": cal.get("summaryOverride") or cal.get("summary", ""),
                            "attendees": [a.get("email") for a in ev.get("attendees") or [] if not a.get("self")],
                            "link": ev.get("htmlLink", ""), "meet": ev.get("hangoutLink", "")})
        out.sort(key=lambda x: x["start"])
        return out

    def insert_event(self, title: str, start: datetime, end: datetime, details: str = "", location: str = "",
                     attendees: list[str] | None = None) -> dict:
        body = {"summary": title, "description": details, "location": location,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        return self.acct.j("POST", f"{GC}/calendars/primary/events", params={"sendUpdates": "all" if attendees else "none"}, json=body)

    def test(self) -> str:
        evs = self.events(days=14)
        return f"Connected: {len(evs)} events in the next two weeks"


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ------------------------------------------------------------------- drive
GD = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
EXPORT = {"application/vnd.google-apps.document": ("text/plain", ".txt", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
          "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
          "application/vnd.google-apps.presentation": ("text/plain", ".txt", "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx")}
FIELDS = "id,name,mimeType,size,quotaBytesUsed,modifiedTime,parents,webViewLink"


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class DriveAdapter:
    """Looks like a storage connection: paths like 'Projects/Report.docx' (or 'id:<fileId>')."""
    kind = "storage"
    mode = "google"
    root = None

    def __init__(self, acct: GoogleAccount):
        self.acct = acct
        self.id = acct.id + ":drive"
        self.label = f"Google Drive ({acct.address})"
        self.service = "Google Drive"
        self.partial = False
        self._paths: dict[str, str] = {}

    def _files(self, q: str, order: str = "", limit: int = 1000) -> list[dict]:
        out, token = [], None
        while len(out) < limit:
            params = {"q": q, "fields": f"nextPageToken,files({FIELDS})", "pageSize": min(1000, limit - len(out)),
                      "spaces": "drive", "includeItemsFromAllDrives": "false"}
            if order:
                params["orderBy"] = order
            if token:
                params["pageToken"] = token
            d = self.acct.j("GET", f"{GD}/files", params=params)
            out += d.get("files") or []
            token = d.get("nextPageToken")
            if not token:
                break
        return out

    def _resolve(self, path: str) -> dict:
        path = (path or "").strip("/")
        if path.startswith("id:"):
            return self.acct.j("GET", f"{GD}/files/{path[3:]}", params={"fields": FIELDS})
        if not path:
            return self.acct.j("GET", f"{GD}/files/root", params={"fields": FIELDS})
        parent = "root"
        cur = None
        for part in path.split("/"):
            name = part.replace("\\", "\\\\").replace("'", "\\'")
            hits = self._files(f"name = '{name}' and '{parent}' in parents and trashed = false", limit=5)
            if not hits:
                raise FileNotFoundError(f"nothing called {path!r} in Google Drive")
            cur = hits[0]
            parent = cur["id"]
        return cur

    def _path_of(self, f: dict) -> str:
        parts, cur, hops = [f["name"]], f, 0
        while cur.get("parents") and hops < 12:
            pid = cur["parents"][0]
            if pid not in self._paths:
                p = self.acct.j("GET", f"{GD}/files/{pid}", params={"fields": "id,name,parents"})
                self._paths[pid] = json.dumps(p)
            p = json.loads(self._paths[pid])
            if not p.get("parents"):     # reached My Drive
                break
            parts.append(p["name"])
            cur, hops = p, hops + 1
        return "/".join(reversed(parts))

    def _item(self, f: dict, with_path: bool = True) -> dict:
        size = int(f.get("size") or f.get("quotaBytesUsed") or 0)
        return {"name": f["name"], "dir": f["mimeType"] == FOLDER, "size": None if f["mimeType"] == FOLDER else size,
                "modified": _ts(f.get("modifiedTime", "")), "path": self._path_of(f) if with_path else f["name"], "id": f["id"],
                "link": f.get("webViewLink", "")}

    def exists(self, path: str) -> bool:
        try:
            self._resolve(path)
            return True
        except FileNotFoundError:
            return False

    def about(self) -> dict:
        q = self.acct.j("GET", f"{GD}/about", params={"fields": "storageQuota"}).get("storageQuota") or {}
        total = int(q["limit"]) if q.get("limit") else None
        used = int(q.get("usage") or 0)
        return {"total": total, "used": used, "free": (total - used) if total else None,
                "trashed": int(q.get("usageInDriveTrash") or 0), "note": "shared with Gmail and Photos"}

    def test(self) -> str:
        from .storage_conn import human
        a = self.about()
        return f"Connected: {human(a['used'])} used of {human(a['total'])} (shared with Gmail and Photos)" if a["total"] else \
            f"Connected: {human(a['used'])} used"

    def list(self, path: str = "", limit: int = 200) -> list[dict]:
        f = self._resolve(path)
        kids = self._files(f"'{f['id']}' in parents and trashed = false", order="folder,name", limit=limit)
        base = path.strip("/")
        return [{**self._item(k, with_path=False), "path": f"{base}/{k['name']}" if base else k["name"]} for k in kids]

    def walk(self, path: str = "", min_size: int = 0, pattern: str = "", limit: int = 5000, max_depth: int = 25,
             max_seconds: float = 90) -> list[dict]:
        self.partial = False
        q = "trashed = false and mimeType != 'application/vnd.google-apps.folder'"
        if pattern:
            core = pattern.strip("*").replace("'", "\\'")
            if core:
                q += f" and name contains '{core}'"
        if path:
            q += f" and '{self._resolve(path)['id']}' in parents"
        files = self._files(q, order="quotaBytesUsed desc" if min_size else "modifiedTime desc", limit=min(limit, 2000))
        out = []
        deadline = time.time() + max_seconds
        for f in files:
            if time.time() > deadline:
                self.partial = True
                break
            size = int(f.get("size") or f.get("quotaBytesUsed") or 0)
            if size >= min_size:
                out.append({"path": self._path_of(f), "size": size, "modified": _ts(f.get("modifiedTime", "")), "id": f["id"]})
        return out

    def search(self, query: str, limit: int = 30) -> list[dict]:
        core = query.strip("*").replace("'", "\\'")
        files = self._files(f"trashed = false and (name contains '{core}' or fullText contains '{core}')", limit=limit)
        return [{"path": self._path_of(f), "size": int(f.get("size") or 0), "modified": _ts(f.get("modifiedTime", ""))} for f in files]

    def big_files(self, min_mb: float = 100, limit: int = 20, path: str = "") -> list[dict]:
        return self.walk(path=path, min_size=int(min_mb * 2**20), limit=max(limit, 50))[:limit]

    def read_text(self, path: str) -> str:
        f = self._resolve(path)
        if f["mimeType"] in EXPORT:
            return self.acct.api("GET", f"{GD}/files/{f['id']}/export", params={"mimeType": EXPORT[f["mimeType"]][0]}).text
        r = self.acct.api("GET", f"{GD}/files/{f['id']}", params={"alt": "media"})
        return r.text

    def download(self, path: str, dest_dir: Path) -> Path:
        f = self._resolve(path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if f["mimeType"] in EXPORT:     # Google Docs/Sheets/Slides come down as Office files
            _, _, mime, ext = EXPORT[f["mimeType"]]
            data = self.acct.api("GET", f"{GD}/files/{f['id']}/export", params={"mimeType": mime}).content
            name = f["name"] + ext
        else:
            data = self.acct.api("GET", f"{GD}/files/{f['id']}", params={"alt": "media"}).content
            name = f["name"]
        from .storage_conn import _free_name
        dest = _free_name(dest_dir / re.sub(r'[<>:"/\\|?*]', "_", name))
        dest.write_bytes(data)
        return dest

    def _ensure_folder(self, path: str) -> str:
        parent = "root"
        for part in [p for p in path.strip("/").split("/") if p]:
            name = part.replace("'", "\\'")
            hits = self._files(f"name = '{name}' and '{parent}' in parents and mimeType = '{FOLDER}' and trashed = false", limit=1)
            if hits:
                parent = hits[0]["id"]
            else:
                parent = self.acct.j("POST", f"{GD}/files", json={"name": part, "mimeType": FOLDER, "parents": [parent]})["id"]
        return parent

    def upload(self, local: Path, dest: str) -> str:
        if local.is_dir():
            raise IsADirectoryError("upload files one at a time to Google Drive")
        dest = dest.strip("/") or local.name
        folder, _, name = dest.rpartition("/")
        stem, suf, i = Path(name).stem, Path(name).suffix, 2
        while self.exists(f"{folder}/{name}".strip("/")):         # never replaces a file
            name = f"{stem} ({i}){suf}"
            i += 1
        parent = self._ensure_folder(folder) if folder else "root"
        meta = json.dumps({"name": name, "parents": [parent]})
        boundary = "la" + pysecrets.token_hex(8)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n--{boundary}\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + local.read_bytes() + f"\r\n--{boundary}--".encode()
        self.acct.api("POST", "https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart"},
                      content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return f"{folder}/{name}".strip("/")

    def verify(self, local: Path, dest: str) -> bool:
        f = self.acct.j("GET", f"{GD}/files/{self._resolve(dest)['id']}", params={"fields": "size,md5Checksum"})
        h = hashlib.md5(local.read_bytes()).hexdigest()
        return int(f.get("size") or -1) == local.stat().st_size and f.get("md5Checksum", h) == h

    def move(self, src: str, dest: str) -> str:
        if self.exists(dest):
            raise FileExistsError(f"{dest} already exists; LocalAgent never replaces files.")
        f = self._resolve(src)
        folder, _, name = dest.strip("/").rpartition("/")
        parent = self._ensure_folder(folder) if folder else "root"
        self.acct.j("PATCH", f"{GD}/files/{f['id']}", params={"addParents": parent, "removeParents": ",".join(f.get("parents") or []),
                                                               "fields": "id"}, json={"name": name})
        return dest.strip("/")

    def trash(self, path: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.move(path, f"agent_trash/{stamp}/{path.strip('/').split('/')[-1]}")


# ------------------------------------------------------------- tasks/people
GT = "https://tasks.googleapis.com/tasks/v1"
GP = "https://people.googleapis.com/v1"


def tasks_list(acct: GoogleAccount, include_done: bool = False) -> list[dict]:
    out = []
    for tl in acct.j("GET", f"{GT}/users/@me/lists").get("items") or []:
        items = acct.j("GET", f"{GT}/lists/{tl['id']}/tasks", params={"showCompleted": str(include_done).lower(), "maxResults": 100}).get("items") or []
        for t in items:
            out.append({"list": tl["title"], "list_id": tl["id"], "id": t["id"], "title": t.get("title", ""), "notes": t.get("notes", ""),
                        "due": t.get("due", "")[:10], "done": t.get("status") == "completed"})
    return out


def tasks_add(acct: GoogleAccount, title: str, notes: str = "", due: str = "", list_name: str = "") -> str:
    lists = acct.j("GET", f"{GT}/users/@me/lists").get("items") or []
    tl = next((l for l in lists if list_name and list_name.lower() in l["title"].lower()), lists[0] if lists else None)
    if tl is None:
        raise RuntimeError("no task lists in Google Tasks")
    body = {"title": title, "notes": notes}
    if due:
        body["due"] = f"{due[:10]}T00:00:00.000Z"
    acct.j("POST", f"{GT}/lists/{tl['id']}/tasks", json=body)
    return tl["title"]


def tasks_complete(acct: GoogleAccount, title: str) -> str:
    t = next((x for x in tasks_list(acct) if title.lower() in x["title"].lower()), None)
    if t is None:
        raise FileNotFoundError(f"no open task matching {title!r}")
    acct.j("PATCH", f"{GT}/lists/{t['list_id']}/tasks/{t['id']}", json={"status": "completed"})
    return t["title"]


def contacts_search(acct: GoogleAccount, query: str, limit: int = 10) -> list[dict]:
    mask = "names,emailAddresses,phoneNumbers,organizations"
    acct.j("GET", f"{GP}/people:searchContacts", params={"query": "", "readMask": mask})   # Google asks for a warm-up call
    out = []
    for r in (acct.j("GET", f"{GP}/people:searchContacts", params={"query": query, "readMask": mask, "pageSize": limit}).get("results") or []):
        p = r.get("person") or {}
        out.append({"name": ((p.get("names") or [{}])[0]).get("displayName", ""),
                    "emails": [e.get("value") for e in p.get("emailAddresses") or []],
                    "phones": [x.get("value") for x in p.get("phoneNumbers") or []],
                    "org": ((p.get("organizations") or [{}])[0]).get("name", "")})
    return out
