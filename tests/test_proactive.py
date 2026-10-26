import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from localagent import proactive as proactive_mod
from localagent.proactive import Proactive, parse_ideas, words, similar


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """Tests must not depend on how full the real drive is."""
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(proactive_mod.shutil, "disk_usage", lambda p: U(1000, 500, 500))


class Cfg:
    def __init__(self, tmp, **proactive):
        self.home = tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = tmp / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.data = {"proactive": proactive}


class FakeTasks:
    def __init__(self):
        self.made, self.items = [], []

    def create(self, message, brain="auto", mode="auto", source="app", **k):
        t = {"id": f"t{len(self.made)}", "message": message, "title": message[:90], "brain": brain, "source": source,
             "status": "running", "created": time.time(), "finished": None, "result": ""}
        self.made.append(t)
        self.items.insert(0, t)
        return t

    def get(self, tid):
        return next((t for t in self.items if t["id"] == tid), None)

    def list(self):
        return list(self.items)


class FakeSched:
    def __init__(self):
        self.added = []

    def list(self):
        return self.added

    def add(self, task, when, name="", created_by="you", **k):
        a = {"task": task, "when": when, "name": name, "schedule": when}
        self.added.append(a)
        return a


class FakeOwner:
    def __init__(self):
        self.notes = []

    def notify(self, title, text, source="", important=True):
        self.notes.append((title, text))


class Reply:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, content):
        self.content, self.calls = content, []

    def chat(self, messages, tools=None, **k):
        self.calls.append(messages)
        return Reply(self.content)


class FakeRouter:
    def __init__(self, content=""):
        self.main = FakeModel(content)


def make(tmp, folders=None, now=None, content="", **settings):
    p = Proactive(Cfg(tmp, **settings), FakeRouter(content), FakeTasks(), FakeSched(), FakeOwner(),
                  now=(lambda: now) if now else datetime.now, folders=folders if folders is not None else [])
    return p


def test_new_document_becomes_a_suggestion_once(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "lease.pdf").write_bytes(b"x" * 500)
    (dl / "~$temp.docx").write_bytes(b"x" * 500)
    p = make(tmp_path, folders=[dl])
    added = p.notice()
    assert [c["kind"] for c in added] == ["summarize_new_doc"]
    assert "lease.pdf" in added[0]["title"]
    assert p.tasks.made == []            # not automatic until you've said yes twice
    assert p.notice() == []              # same file isn't suggested again


def test_two_yeses_make_a_read_only_kind_automatic(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    p = make(tmp_path, folders=[dl])
    for i in range(3):
        (dl / f"doc{i}.pdf").write_bytes(b"x" * 500)
    cards = p.notice()
    p.accept(cards[0]["id"])
    assert not p.is_auto("summarize_new_doc")
    c2 = p.accept(cards[1]["id"])
    assert p.is_auto("summarize_new_doc") and "automatically" in c2["note"]
    (dl / "doc9.pdf").write_bytes(b"x" * 500)
    before = len(p.tasks.made)
    p.notice()
    assert len(p.tasks.made) == before + 1          # started on its own
    assert p.tasks.made[-1]["brain"] == "local"     # automatic work stays on this computer
    assert p.tasks.made[-1]["source"] == "for you"


def test_never_blocks_a_kind_and_closes_its_cards(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    for i in range(2):
        (dl / f"a{i}.pdf").write_bytes(b"x" * 500)
    p = make(tmp_path, folders=[dl])
    cards = p.notice()
    p.dismiss(cards[0]["id"], never=True)
    assert all(c["kind"] != "summarize_new_doc" for c in p.cards())
    (dl / "b.pdf").write_bytes(b"x" * 500)
    assert p.notice() == []
    p.unblock("summarize_new_doc")
    assert len(p.notice()) == 1


def test_a_no_resets_the_streak(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    for i in range(3):
        (dl / f"a{i}.pdf").write_bytes(b"x" * 500)
    p = make(tmp_path, folders=[dl])
    c = p.notice()
    p.accept(c[0]["id"]); p.dismiss(c[1]["id"]); p.accept(c[2]["id"])
    assert not p.is_auto("summarize_new_doc")


def test_messy_downloads_is_suggested_not_done(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    for i in range(31):
        (dl / f"img{i}.png").write_bytes(b"x")
    p = make(tmp_path, folders=[dl])
    kinds = [c["kind"] for c in p.notice()]
    assert "tidy_downloads" in kinds and p.tasks.made == []
    card = next(c for c in p.cards() if c["kind"] == "tidy_downloads")
    assert "never delete" in card["task"].lower()


def test_off_means_nothing(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "x.pdf").write_bytes(b"x" * 500)
    p = make(tmp_path, folders=[dl], level="off")
    assert p.notice() == [] and p.cards() == []


def test_suggest_level_never_runs_on_its_own(tmp_path):
    p = make(tmp_path, level="suggest")
    c = p.add_card("disk_space", "Disk nearly full", "list big files", sig="d")
    assert c and p.tasks.made == []
    p.accept(c["id"])
    assert len(p.tasks.made) == 1 and p.tasks.made[0]["brain"] == "auto"   # you tapped it: normal routing


def test_repeated_request_offers_an_automation(tmp_path):
    p = make(tmp_path)
    base = datetime(2026, 10, 19, 8, 5)   # a Monday
    for d in range(3):
        t = p.tasks.create("Summarize the top tech news headlines for me")
        t["created"] = (base + timedelta(days=d)).timestamp()
        t["status"] = "done"
    cards = p.notice()
    rep = next(c for c in cards if c["kind"] == "automate_repeat")
    assert rep["when"] == "weekdays 08:00"
    p.accept(rep["id"])
    assert p.scheduler.added and p.scheduler.added[0]["when"] == "weekdays 08:00"
    assert p.notice() == [] or all(c["kind"] != "automate_repeat" for c in p.notice())


def test_failed_task_offers_retry_with_another_brain(tmp_path):
    p = make(tmp_path)
    t = p.tasks.create("Find flights to Denver")
    t.update(status="failed", finished=time.time())
    c = next(c for c in p.notice() if c["kind"] == "retry_failed")
    p.accept(c["id"])
    assert p.tasks.made[-1]["brain"] == "auto"


def test_brief_only_when_there_is_something_to_say(tmp_path):
    p = make(tmp_path, now=datetime(2026, 10, 26, 9, 0))
    assert p.maybe_brief() is None                       # nothing to report: no empty "good morning"
    (Path(p.cfg.workspace) / "todo.md").write_text("- pay rent friday")
    c = p.maybe_brief()
    assert c and c["kind"] == "morning_brief"
    assert len(p.tasks.made) == 1                        # read-only brief runs by itself
    assert p.maybe_brief() is None                       # once a day


def test_brief_not_before_brief_hour(tmp_path):
    p = make(tmp_path, now=datetime(2026, 10, 26, 6, 0))
    (Path(p.cfg.workspace) / "todo.md").write_text("- x")
    assert p.maybe_brief() is None


def test_ideas_use_names_not_contents_and_are_filtered(tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "budget-2026.xlsx").write_text("SECRET-CONTENT")
    reply = json.dumps({"ideas": [
        {"title": "Check this month's budget against last month?", "why": "You keep a budget.",
         "task": "Open budget-2026.xlsx and compare spending by category.", "when": "", "area": "money"},
        {"title": "Clean up old files", "why": "", "task": "Delete files older than a year", "when": "", "area": "files"},
        {"title": "Morning news", "why": "", "task": "Summarize tech news", "when": "weekdays 7am", "area": "news"},
        {"title": "Bad schedule", "why": "", "task": "Something", "when": "whenever you like", "area": "general"},
    ]})
    p = make(tmp_path, folders=[docs], content=reply)
    p.set_profile("I'm a student and I work part time", ["money", "news"])
    added = p.think(force=True)
    titles = [c["title"] for c in added]
    assert "Clean up old files" not in titles               # never suggests deleting
    assert len(added) == 3
    assert next(c for c in added if c["title"] == "Bad schedule")["when"] == ""
    sent = json.dumps(p.router.main.calls[0])
    assert "budget-2026.xlsx" in sent and "SECRET-CONTENT" not in sent
    assert "part time" in sent
    assert p.tasks.made == []                                # ideas always wait for a yes


def test_scheduled_idea_becomes_an_automation(tmp_path):
    p = make(tmp_path)
    c = p.add_card("idea:news", "Morning tech news?", "Summarize tech news", when="weekdays 7am", sig="n")
    p.accept(c["id"])
    assert p.scheduler.added[0]["when"] == "weekdays 7am" and p.tasks.made == []


def test_alerts_respect_quiet_hours_and_daily_cap(tmp_path):
    p = make(tmp_path, now=datetime(2026, 10, 26, 23, 30), max_alerts_per_day=1)
    p.add_card("idea:x", "Thing one", "do one", sig="1")
    p._alert_new()
    assert p.owner.notes == []                                # quiet hours
    p.now = lambda: datetime(2026, 10, 26, 10, 0)
    p._alert_new()
    assert len(p.owner.notes) == 1
    p.add_card("idea:y", "Thing two", "do two", sig="2")
    p._alert_new()
    assert len(p.owner.notes) == 1                            # daily cap


def test_card_status_follows_its_task(tmp_path):
    p = make(tmp_path)
    c = p.add_card("idea:x", "Thing", "do it", sig="z")
    p.accept(c["id"])
    p.tasks.made[-1].update(status="done", result="All done")
    assert p.cards() == []
    assert p.cards(include_closed=True)[0]["result"] == "All done"


def test_state_survives_restart(tmp_path):
    p = make(tmp_path)
    p.set_profile("teacher", ["writing"])
    c = p.add_card("idea:x", "Thing", "do it", sig="z")
    p.dismiss(c["id"], never=True)
    q = make(tmp_path)
    assert q.data["profile"]["about"] == "teacher" and q.blocked("idea:x")


def test_similarity_helpers():
    assert similar(words("Summarize the tech news"), words("summarize tech news please")) == 1.0
    assert parse_ideas("no json here") == []
    assert parse_ideas('<think>{"ideas":[]}</think>{"ideas":[{"title":"A","task":"B"}]}')[0]["title"] == "A"


def test_full_disk_is_checked_automatically(tmp_path, monkeypatch):
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(proactive_mod.shutil, "disk_usage", lambda p: U(1000 * 2**30, 940 * 2**30, 60 * 2**30))
    p = make(tmp_path)
    c = next(c for c in p.notice() if c["kind"] == "disk_space")
    assert "94% full" in c["title"]
    assert len(p.tasks.made) == 1 and "Don't move or remove anything" in p.tasks.made[0]["message"]
    assert all(c["kind"] != "disk_space" for c in p.notice())   # once a week, not every 10 minutes


def test_ideas_dont_reword_what_was_already_noticed(tmp_path):
    reply = json.dumps({"ideas": [{"title": "Can I help organize your Downloads folder?", "task": "sort it"},
                                  {"title": "Make a study plan for finals?", "task": "plan", "area": "learning"}]})
    p = make(tmp_path, content=reply)
    p.add_card("tidy_downloads", "Tidy your Downloads folder (220 loose files)", "x", sig="t")
    titles = [c["title"] for c in p.think(force=True)]
    assert titles == ["Make a study plan for finals?"]


def test_one_json_typo_doesnt_lose_every_idea():
    broken = """{ "ideas": [ {"title": "Find duplicate files", "why": "w", "task": "t1", "when": "", "area": "files"},
      {"title": "Weekly budget summary", "why": "w", "task": "t2", "when": "every monday 9am", "area": "money"},
      { " "title": "Research AI meetups in Madison", "why": "w", "task": "t3", "when": "", "area": "research" } ] }"""
    got = parse_ideas(broken)
    assert [i["title"] for i in got] == ["Find duplicate files", "Weekly budget summary", "Research AI meetups in Madison"]
    assert got[1]["when"] == "every monday 9am"


# ------------------------------------------------------------ connections
class FakeMail:
    id, label, address = "email-1", "Gmail (nick@gmail.com)", "nick@gmail.com"

    def recent(self, **k):
        return [{"id": "INBOX:7", "from": "Ann Lee <ann@x.com>", "subject": "Lunch Tuesday?", "bulk": False},
                {"id": "INBOX:8", "from": "Deals <news@shop.com>", "subject": "50% off", "bulk": True}]


class FakeCal:
    id, label = "cal-1", "Google Calendar"

    def __init__(self, now):
        self.now = now

    def events(self, start, days=1, query=""):
        s = self.now.astimezone() + timedelta(hours=1)
        return [{"title": "Advisor meeting", "start": s, "end": s + timedelta(hours=1), "all_day": False,
                 "location": "Room 2", "description": "Talk about transfer credits", "calendar": self.label},
                {"title": "Holiday", "start": s, "end": s, "all_day": True, "location": "", "description": "", "calendar": self.label}]


class FakeConns:
    def __init__(self, kinds, detected=()):
        self.kinds, self._det, self.added = kinds, list(detected), []

    def of_kind(self, k):
        return self.kinds.get(k, [])

    def detected(self):
        return self._det

    def add(self, t, spec, secret=None):
        self.added.append((t, spec))
        return {"message": "Connected: 12 notes"}

    def summary(self):
        return "email: Gmail"


def test_unread_email_from_a_person_offers_a_draft_not_a_send(tmp_path):
    p = make(tmp_path, now=datetime(2026, 10, 26, 10, 0))
    p.connections = FakeConns({"email": [FakeMail()]})
    cards = p.notice_connections()
    mail = [c for c in cards if c["kind"] == "email_reply"]
    assert len(mail) == 1 and "Ann Lee" in mail[0]["title"]          # the newsletter is skipped
    assert "Do NOT send" in mail[0]["task"] and "email_draft" in mail[0]["task"]
    assert p.tasks.made == []                                         # waits for a yes
    assert p.notice_connections() == [] or all(c["kind"] != "email_reply" for c in p.notice_connections())


def test_meeting_soon_offers_prep(tmp_path):
    now = datetime(2026, 10, 26, 10, 0)
    p = make(tmp_path, now=now)
    p.connections = FakeConns({"calendar": [FakeCal(now)], "notes": [object()]})
    prep = [c for c in p.notice_connections() if c["kind"] == "meeting_prep"]
    assert len(prep) == 1 and "Advisor meeting" in prep[0]["title"] and "notes" in prep[0]["task"]


def test_found_notes_app_connects_in_one_tap(tmp_path):
    p = make(tmp_path)
    p.connections = FakeConns({}, detected=[{"app": "obsidian", "path": "C:/Vault", "name": "Obsidian · Vault"}])
    cards = p.notice_connections()
    c = next(c for c in cards if c["kind"] == "connect_app" and "Obsidian" in c["title"])
    assert {x["title"] for x in cards} >= {"Connect your calendar?", "Connect your email?"}
    done = p.accept(c["id"])
    assert done["status"] == "done" and p.connections.added == [("notes", {"app": "obsidian", "path": "C:/Vault", "name": "Obsidian · Vault"})]
    assert p.tasks.made == []


def test_brief_uses_calendar_and_email(tmp_path):
    p = make(tmp_path, now=datetime(2026, 10, 26, 9, 0))
    p.connections = FakeConns({"calendar": [object()], "email": [object()]})
    c = p.maybe_brief()
    assert c and "calendar_events" in c["task"] and "email_list" in c["task"]
