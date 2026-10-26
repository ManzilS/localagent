"""Notes: Obsidian vaults, Logseq graphs, any folder of Markdown, and Joplin.

Markdown notes are plain files on this computer, so LocalAgent reads them
directly. It only ever *adds*: new notes get a fresh name, and appending to
an existing note keeps the old version in agent_trash first.

Joplin is read from its own database (read-only), so it works even when
Joplin is closed. New Joplin notes go through Joplin's local Web Clipper
service, which needs Joplin open with that service turned on.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx

NOTE_EXT = {".md", ".markdown", ".txt", ".org"}
SKIP_DIRS = {".obsidian", ".trash", ".git", "logseq", "node_modules", ".stfolder", "agent_trash", "bak", ".logseq"}
TODO_RE = re.compile(r"^\s*[-*]\s+(\[ \]|TODO\b|LATER\b|NOW\b)\s*(.+)$", re.M)


# ------------------------------------------------------------- discovery
def detect() -> list[dict]:
    """Note apps found on this computer, ready to connect with one click."""
    found: list[dict] = []
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    for cfg in (appdata / "obsidian" / "obsidian.json", home / "Library" / "Application Support" / "obsidian" / "obsidian.json",
                home / ".config" / "obsidian" / "obsidian.json"):
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for v in (data.get("vaults") or {}).values():
            p = Path(v.get("path", ""))
            if p.is_dir():
                found.append({"type": "notes", "app": "obsidian", "path": str(p), "name": f"Obsidian · {p.name}"})
    graphs = home / ".logseq" / "graphs"
    if graphs.is_dir():
        for g in graphs.glob("logseq_local_*.transit"):
            enc = g.stem[len("logseq_local_"):]
            p = Path(enc.replace("+3A+", ":").replace("++", os.sep))
            if p.is_dir():
                found.append({"type": "notes", "app": "logseq", "path": str(p), "name": f"Logseq · {p.name}"})
    for prof in (home / ".config" / "joplin-desktop", home / "Library" / "Application Support" / "Joplin"):
        if (prof / "database.sqlite").exists():
            found.append({"type": "notes", "app": "joplin", "path": str(prof), "name": "Joplin"})
    return found


def joplin_token(profile: Path) -> str:
    try:
        return json.loads((profile / "settings.json").read_text(encoding="utf-8")).get("api.token", "")
    except (OSError, json.JSONDecodeError):
        return ""


# ------------------------------------------------------------ connection
class NotesConnection:
    kind = "notes"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec, self.secrets = cid, spec, secrets
        self.app = spec.get("app", "folder")
        self.root = Path(spec.get("path", "")).expanduser()
        self.label = spec.get("name") or f"Notes · {self.root.name}"

    # ---------------------------------------------------------- markdown
    def _files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for f in filenames:
                if Path(f).suffix.lower() in NOTE_EXT:
                    yield Path(dirpath) / f

    def _inside(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise PermissionError("that path is outside this notes folder")
        return p

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")

    # ------------------------------------------------------------ public
    def test(self) -> str:
        if self.app == "joplin":
            n = self._jdb().execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            return f"Connected: {n} Joplin notes" + ("" if self._japi_ok() else " (adding notes needs Joplin open with the Web Clipper service on)")
        if not self.root.is_dir():
            raise RuntimeError(f"{self.root} isn't there anymore")
        n = sum(1 for _ in self._files())
        return f"Connected: {n} notes in {self.root}"

    def recent(self, limit: int = 10) -> list[dict]:
        if self.app == "joplin":
            rows = self._jdb().execute("SELECT id, title, updated_time FROM notes ORDER BY updated_time DESC LIMIT ?", (limit,)).fetchall()
            return [{"id": r[0], "title": r[1], "updated": r[2] / 1000} for r in rows]
        items = sorted(((p.stat().st_mtime, p) for p in self._files()), reverse=True)[:limit]
        return [{"id": self._rel(p), "title": p.stem, "updated": t} for t, p in items]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        if self.app == "joplin":
            like = " AND ".join(["(lower(title) LIKE ? OR lower(body) LIKE ?)"] * len(words)) or "1=1"
            args = [a for w in words for a in (f"%{w}%", f"%{w}%")]
            rows = self._jdb().execute(f"SELECT id, title, body, updated_time FROM notes WHERE {like} "
                                       "ORDER BY updated_time DESC LIMIT ?", (*args, limit)).fetchall()
            return [{"id": r[0], "title": r[1], "snippet": _snippet(r[2], words), "updated": r[3] / 1000} for r in rows]
        hits = []
        for p in self._files():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = (p.stem + "\n" + text).lower()
            score = sum(low.count(w) for w in words) + 5 * sum(w in p.stem.lower() for w in words)
            if words and all(w in low for w in words):
                hits.append((score, p.stat().st_mtime, p, text))
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
        return [{"id": self._rel(p), "title": p.stem, "snippet": _snippet(t, words), "updated": m} for s, m, p, t in hits[:limit]]

    def read(self, note_id: str) -> dict:
        if self.app == "joplin":
            r = self._jdb().execute("SELECT id, title, body, updated_time FROM notes WHERE id = ?", (note_id,)).fetchone()
            if not r:
                raise FileNotFoundError(f"no Joplin note {note_id}")
            return {"id": r[0], "title": r[1], "text": r[2][:20000], "updated": r[3] / 1000}
        p = self._inside(note_id)
        return {"id": self._rel(p), "title": p.stem, "text": p.read_text(encoding="utf-8", errors="replace")[:20000],
                "updated": p.stat().st_mtime}

    def open_todos(self, limit: int = 30) -> list[dict]:
        if self.app == "joplin":
            rows = self._jdb().execute("SELECT id, title, todo_due FROM notes WHERE is_todo = 1 AND todo_completed = 0 "
                                       "ORDER BY CASE WHEN todo_due = 0 THEN 1 ELSE 0 END, todo_due LIMIT ?", (limit,)).fetchall()
            return [{"text": r[1], "note": r[1], "id": r[0], "due": r[2] / 1000 if r[2] else None} for r in rows]
        out = []
        for _, p in sorted(((p.stat().st_mtime, p) for p in self._files()), reverse=True)[:400]:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in TODO_RE.finditer(text):
                item = m.group(2).strip()
                due = re.search(r"(?:📅|due:?|SCHEDULED: <|DEADLINE: <)\s*(\d{4}-\d{2}-\d{2})", item)
                out.append({"text": item[:200], "note": p.stem, "id": self._rel(p),
                            "due": datetime.strptime(due.group(1), "%Y-%m-%d").timestamp() if due else None})
                if len(out) >= limit:
                    return out
        return out

    def add(self, title: str, text: str, folder: str = "", trash=None) -> str:
        if self.app == "joplin":
            return self._japi_add(title, text)
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title).strip(" .") or "Note"
        d = self._inside(folder) if folder else self.root
        if self.app == "logseq" and not folder:
            d = self.root / "pages"
        d.mkdir(parents=True, exist_ok=True)
        p, i = d / f"{safe}.md", 2
        while p.exists():                      # never overwrite an existing note
            p, i = d / f"{safe} ({i}).md", i + 1
        body = text if self.app == "logseq" or text.lstrip().startswith("#") else f"# {title}\n\n{text}"
        p.write_text(body.rstrip() + "\n", encoding="utf-8")
        return self._rel(p)

    def append(self, note_id: str, text: str, trash=None) -> str:
        if self.app == "joplin":
            raise RuntimeError("appending to Joplin notes isn't supported yet; add a new note instead")
        p = self._inside(note_id)
        if not p.exists():
            raise FileNotFoundError(note_id)
        if trash is not None:
            trash.backup(str(p), reason="before LocalAgent added to this note")
        with p.open("a", encoding="utf-8") as f:
            f.write(("\n" if not p.read_text(encoding="utf-8", errors="replace").endswith("\n") else "") + text.rstrip() + "\n")
        return self._rel(p)

    def daily_note(self) -> str:
        """Today's daily note (Obsidian/Logseq conventions), or ''."""
        today = datetime.now()
        for rel in (f"journals/{today:%Y_%m_%d}.md", f"Daily/{today:%Y-%m-%d}.md", f"{today:%Y-%m-%d}.md"):
            if (self.root / rel).exists():
                return rel
        return ""

    # ------------------------------------------------------------ joplin
    def _jdb(self) -> "_JoplinDB":
        db = self.root / "database.sqlite"
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        cols = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
        return _JoplinDB(con, "deleted_time" in cols)

    def _japi(self) -> tuple[str, str]:
        return f"http://127.0.0.1:{int(self.spec.get('port', 41184))}", joplin_token(self.root)

    def _japi_ok(self) -> bool:
        base, _ = self._japi()
        try:
            return httpx.get(base + "/ping", timeout=2).text.strip() == "JoplinClipperServer"
        except httpx.HTTPError:
            return False

    def _japi_add(self, title: str, text: str) -> str:
        base, tok = self._japi()
        if not self._japi_ok():
            raise RuntimeError("Joplin's Web Clipper service isn't running. Open Joplin → Tools → Options → "
                               "Web Clipper → Enable, then try again.")
        r = httpx.post(f"{base}/notes", params={"token": tok}, json={"title": title, "body": text}, timeout=15)
        r.raise_for_status()
        return r.json().get("id", "")


class _JoplinDB:
    """Read-only Joplin database that hides notes sitting in Joplin's trash."""
    def __init__(self, con: sqlite3.Connection, has_trash: bool):
        self.con, self.has_trash = con, has_trash

    def execute(self, sql: str, args: tuple = ()):
        if self.has_trash and "FROM notes" in sql:
            live = "(deleted_time IS NULL OR deleted_time = 0)"
            sql = sql.replace("FROM notes WHERE", f"FROM notes WHERE {live} AND") if "FROM notes WHERE" in sql \
                else sql.replace("FROM notes", f"FROM notes WHERE {live}", 1)
        return self.con.execute(sql, args)


def _snippet(text: str, words: list[str], width: int = 220) -> str:
    low = text.lower()
    pos = min([low.find(w) for w in words if low.find(w) >= 0] or [0])
    s = text[max(0, pos - 60): pos + width].replace("\n", " ").strip()
    return ("…" if pos > 60 else "") + s + ("…" if len(text) > pos + width else "")
