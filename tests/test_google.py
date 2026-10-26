import base64
import json
import time
from datetime import datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from localagent.connections import ConnectionManager, google_conn, registry_tools
from localagent.connections.secrets import Secrets


class Cfg:
    def __init__(self, tmp):
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = tmp / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.data = {"google_oauth": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-x"},
                     "workspace": str(self.workspace), "allowed_roots": [str(tmp)]}


def raw_mail(frm, subj, body, mid="<m1@x>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "nick@gmail.com", subj, mid
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


class FakeGoogle:
    def __init__(self):
        self.log, self.drafts, self.sent, self.events, self.files, self.revoked = [], [], [], [], {}, []
        self.tokens_issued = 0
        self.expire_next = False
        self.files = {"root": {"id": "root", "name": "My Drive", "mimeType": "application/vnd.google-apps.folder"},
                      "f1": {"id": "f1", "name": "School", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]},
                      "f2": {"id": "f2", "name": "lecture.mp4", "mimeType": "video/mp4", "size": "900000000", "parents": ["f1"],
                             "modifiedTime": "2025-01-01T00:00:00Z"},
                      "f3": {"id": "f3", "name": "Essay", "mimeType": "application/vnd.google-apps.document", "parents": ["f1"],
                             "modifiedTime": "2026-10-01T00:00:00Z"}}

    def handler(self, req: httpx.Request):
        u, m = req.url, req.method
        path = u.path
        self.log.append((m, str(u)))
        auth = req.headers.get("authorization", "")
        if u.host == "oauth2.googleapis.com":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&")) if req.content else {}
            if path == "/token":
                assert form["client_id"].startswith("123-abc") and form["client_secret"] == "GOCSPX-x"
                self.tokens_issued += 1
                if form["grant_type"] == "authorization_code":
                    assert form["code"] == "good" and form["code_verifier"]
                    return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/gmail.modify "
                                 "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/tasks"})
                assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "RT1"
                return httpx.Response(200, json={"access_token": f"AT{self.tokens_issued}", "expires_in": 3600})
            if path == "/revoke":
                self.revoked.append(form.get("token"))
                return httpx.Response(200)
        if self.expire_next and auth == "Bearer AT1":
            self.expire_next = False
            return httpx.Response(401, json={"error": {"message": "expired"}})
        if u.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "nick@gmail.com", "name": "Nick"})
        if u.host == "gmail.googleapis.com":
            if path.endswith("/profile"):
                return httpx.Response(200, json={"emailAddress": "nick@gmail.com", "messagesTotal": 4200})
            if path.endswith("/messages") and m == "GET":
                return httpx.Response(200, json={"messages": [{"id": "a1"}, {"id": "a2"}]})
            if "/messages/" in path and m == "GET":
                mid = path.rsplit("/", 1)[-1]
                fmt = u.params.get("format")
                if fmt == "raw":
                    return httpx.Response(200, json={"raw": raw_mail("Ann <ann@x.com>", "Lunch?", "Free Tuesday?", "<ann1@x>")})
                if fmt == "minimal":
                    return httpx.Response(200, json={"id": mid, "threadId": "T9"})
                hdrs = [{"name": "From", "value": "Ann <ann@x.com>" if mid == "a1" else "Shop <deals@shop.com>"},
                        {"name": "Subject", "value": "Lunch?" if mid == "a1" else "Sale"}]
                if mid == "a2":
                    hdrs.append({"name": "List-Unsubscribe", "value": "<mailto:x>"})
                return httpx.Response(200, json={"id": mid, "internalDate": "1792900000000", "labelIds": ["INBOX", "UNREAD"],
                                                 "payload": {"headers": hdrs}})
            if path.endswith("/drafts"):
                self.drafts.append(json.loads(req.content))
                return httpx.Response(200, json={"id": "d1"})
            if path.endswith("/messages/send"):
                self.sent.append(json.loads(req.content))
                return httpx.Response(200, json={"id": "s1"})
        if u.host == "www.googleapis.com" and path.startswith("/calendar/v3"):
            if path.endswith("/calendarList"):
                return httpx.Response(200, json={"items": [{"id": "nick@gmail.com", "summary": "Nick"}]})
            if path.endswith("/events") and m == "GET":
                return httpx.Response(200, json={"items": [
                    {"summary": "Advisor", "start": {"dateTime": "2026-10-26T15:00:00Z"}, "end": {"dateTime": "2026-10-26T16:00:00Z"},
                     "attendees": [{"email": "adv@madisoncollege.edu"}, {"email": "nick@gmail.com", "self": True}]},
                    {"summary": "Holiday", "start": {"date": "2026-10-27"}, "end": {"date": "2026-10-28"}}]})
            if path.endswith("/events") and m == "POST":
                self.events.append((dict(u.params), json.loads(req.content)))
                return httpx.Response(200, json={"id": "e1", "htmlLink": "https://calendar.google.com/e1"})
        if u.host == "www.googleapis.com" and path.startswith("/drive/v3"):
            if path.endswith("/about"):
                return httpx.Response(200, json={"storageQuota": {"limit": str(15 * 2**30), "usage": str(14 * 2**30), "usageInDriveTrash": "0"}})
            if path == "/drive/v3/files" and m == "GET":
                q = u.params.get("q", "")
                out = [f for f in self.files.values() if f["id"] != "root"]
                import re
                if "' in parents" in q:
                    pid = re.search(r"'([^']+)' in parents", q).group(1)
                    out = [f for f in out if pid in f.get("parents", [])]
                mm = re.search(r"name = '([^']+)'", q)
                if mm:
                    out = [f for f in out if f["name"] == mm.group(1)]
                if "mimeType != 'application/vnd.google-apps.folder'" in q:
                    out = [f for f in out if f["mimeType"] != "application/vnd.google-apps.folder"]
                if "quotaBytesUsed desc" in u.params.get("orderBy", ""):
                    out.sort(key=lambda f: -int(f.get("size") or 0))
                return httpx.Response(200, json={"files": out})
            if path.startswith("/drive/v3/files/") and m == "GET":
                fid = path.split("/")[4]
                if path.endswith("/export"):
                    return httpx.Response(200, text="My essay text")
                return httpx.Response(200, json=self.files[fid])
            if path == "/drive/v3/files" and m == "POST":
                b = json.loads(req.content)
                fid = f"n{len(self.files)}"
                self.files[fid] = {"id": fid, "name": b["name"], "mimeType": b["mimeType"], "parents": b["parents"]}
                return httpx.Response(200, json={"id": fid})
            if path.startswith("/drive/v3/files/") and m == "PATCH":
                fid = path.split("/")[4]
                f = self.files[fid]
                f["parents"] = [u.params["addParents"]]
                f["name"] = json.loads(req.content)["name"]
                return httpx.Response(200, json={"id": fid})
            if m == "DELETE":
                raise AssertionError("LocalAgent must never delete")
        if u.host == "www.googleapis.com" and path.startswith("/upload/drive"):
            body = req.content
            meta = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
            fid = f"u{len(self.files)}"
            self.files[fid] = {"id": fid, "name": meta["name"], "mimeType": "application/octet-stream", "parents": meta["parents"]}
            return httpx.Response(200, json={"id": fid})
        if u.host == "tasks.googleapis.com":
            if path.endswith("/lists"):
                return httpx.Response(200, json={"items": [{"id": "L1", "title": "My Tasks"}]})
            if path.endswith("/tasks") and m == "GET":
                return httpx.Response(200, json={"items": [{"id": "t1", "title": "Finish FAFSA", "due": "2026-11-01T00:00:00Z"}]})
            if path.endswith("/tasks") and m == "POST":
                return httpx.Response(200, json={"id": "t2"})
        return httpx.Response(404, json={"error": {"message": f"unmocked {m} {u}"}})


@pytest.fixture
def g(tmp_path, monkeypatch):
    fake = FakeGoogle()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(google_conn, "_http", client)
    cfg = Cfg(tmp_path)
    m = ConnectionManager(cfg, secrets=Secrets(tmp_path, use_keychain=False))
    url = google_conn.start_signin(cfg, "http://127.0.0.1:7860/oauth/google", ["gmail", "calendar", "drive", "tasks", "contacts"], m.secrets)
    q = parse_qs(urlparse(url).query)
    info = google_conn.finish_signin(q["state"][0], "good")
    r = m.save_google(info)
    return fake, m, r, q, info


def test_signin_link_and_partial_consent(g):
    fake, m, r, q, info = g
    assert q["redirect_uri"] == ["http://127.0.0.1:7860/oauth/google"] and q["access_type"] == ["offline"]
    assert q["code_challenge_method"] == ["S256"] and "gmail.modify" in q["scope"][0] and "contacts.readonly" in q["scope"][0]
    assert info["email"] == "nick@gmail.com" and info["declined"] == ["contacts"]     # unticked on Google's page
    assert r["services"] == ["gmail", "calendar", "drive", "tasks"]
    assert "RT1" not in json.dumps(m.specs()) and "RT1" in json.dumps(m.secrets._load())   # tokens live in the vault only
    with pytest.raises(RuntimeError, match="expired"):
        google_conn.finish_signin(q["state"][0], "good")                                  # one-time


def test_gmail_is_an_email_account(g, tmp_path):
    fake, m, *_ = g
    gm = next(c for c in m.of_kind("email"))
    items = gm.recent(unread_only=True)
    assert [i["subject"] for i in items] == ["Lunch?", "Sale"] and [i["bulk"] for i in items] == [False, True]
    assert "Free Tuesday" in gm.read("a1")["body"]

    class RT:
        cfg = m.cfg
        connections = m
        answers = []
        def request_approval(self, q):
            self.q = q
            return self.answers.pop(0) if self.answers else False
    rt = RT()
    tools = {t.name: t for t in registry_tools(rt)}
    out = tools["email_draft"].func(rt, body="Yes, Tuesday works!", reply_to_id="a1")
    raw = base64.urlsafe_b64decode(fake.drafts[0]["message"]["raw"])
    assert "Nothing was sent" in out and fake.drafts[0]["message"]["threadId"] == "T9" and b"In-Reply-To: <ann1@x>" in raw
    assert "Not sent" in tools["email_send"].func(rt, body="hi", to="ann@x.com", subject="Hey") and not fake.sent
    rt.answers = [True]
    tools["email_send"].func(rt, body="hi", to="ann@x.com", subject="Hey")
    assert len(fake.sent) == 1


def test_google_calendar_reads_and_adds(g):
    fake, m, *_ = g
    cal = next(c for c in m.of_kind("calendar"))
    evs = cal.events(start=datetime(2026, 10, 26).astimezone(), days=3)
    adv = next(e for e in evs if e["title"] == "Advisor")
    assert adv["start"].utcoffset() is not None and adv["start"].astimezone(datetime.now().astimezone().tzinfo).hour == \
        datetime(2026, 10, 26, 15, tzinfo=__import__("datetime").timezone.utc).astimezone().hour
    assert adv["attendees"] == ["adv@madisoncollege.edu"] and next(e for e in evs if e["title"] == "Holiday")["all_day"]

    class RT:
        cfg = m.cfg
        connections = m
        def request_approval(self, q):
            self.q = q
            return False
    rt = RT()
    tools = {t.name: t for t in registry_tools(rt)}
    out = tools["calendar_add_event"].func(rt, title="Study group", start="2026-10-28T18:00")
    assert "Added to Google Calendar" in out and fake.events[-1][0]["sendUpdates"] == "none"
    out = tools["calendar_add_event"].func(rt, title="Coffee", start="2026-10-29T10:00", invite="sam@x.com")
    assert "Not added" in out and "sam@x.com" in rt.q and len(fake.events) == 1        # inviting people asks first


def test_drive_is_storage_and_never_deletes(g, tmp_path):
    fake, m, *_ = g
    dr = next(c for c in m.of_kind("storage") if c.mode == "google")
    a = dr.about()
    assert a["total"] == 15 * 2**30 and a["used"] == 14 * 2**30
    assert dr.big_files(min_mb=100)[0]["path"] == "School/lecture.mp4"
    assert dr.read_text("School/Essay") == "My essay text"
    f = tmp_path / "notes.txt"
    f.write_text("x", encoding="utf-8")
    assert dr.upload(f, "School/notes.txt") == "School/notes.txt"
    assert dr.upload(f, "School/notes.txt") == "School/notes (2).txt"                    # never replaces
    where = dr.trash("School/lecture.mp4")
    assert where.startswith("agent_trash/") and fake.files["f2"]["name"] == "lecture.mp4"
    assert not any(mth == "DELETE" for mth, _ in fake.log)


def test_tokens_refresh_and_disconnect_revokes(g):
    fake, m, r, *_ = g
    acct = m.get(r["id"])
    t = acct._tokens()
    t["expires_at"] = time.time() - 10
    acct.save_tokens(t)
    assert acct.token().startswith("AT") and acct.token() != "AT1"                         # refreshed when expired
    t = acct._tokens(); t["access_token"] = "AT1"; t["expires_at"] = time.time() + 3600; acct.save_tokens(t)
    fake.expire_next = True
    assert next(c for c in m.of_kind("email")).test().startswith("Connected")              # 401 -> refresh -> retry
    m.remove(r["id"])
    assert fake.revoked == ["RT1"] and m.of_kind("email") == [] and "RT1" not in json.dumps(m.secrets._load())


def test_tasks_tools(g):
    fake, m, *_ = g

    class RT:
        cfg = m.cfg
        connections = m
    tools = {t.name: t for t in registry_tools(RT())}
    assert "Finish FAFSA" in tools["tasks_list"].func(RT()) and "(due 2026-11-01)" in tools["tasks_list"].func(RT())
    assert "My Tasks" in tools["tasks_add"].func(RT(), title="Email advisor")
    assert "contacts_search" not in tools                                                  # not granted


def test_no_client_means_clear_message(tmp_path, monkeypatch):
    cfg = Cfg(tmp_path)
    cfg.data.pop("google_oauth")
    monkeypatch.delenv("LOCALAGENT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setattr(google_conn, "client_config", lambda c: None)
    with pytest.raises(RuntimeError, match="registered with Google"):
        google_conn.start_signin(cfg, "http://127.0.0.1:1/x", ["gmail"], Secrets(tmp_path, use_keychain=False))
