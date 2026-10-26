"""Long-term memory: SQLite FTS5 keyword search, blended with cosine
similarity from a local embedding model when one is available. Memories are
archived, never deleted (same spirit as agent_trash)."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path


class MemoryStore:
    def __init__(self, path: Path, embedder=None):
        self.path = path
        self.embedder = embedder  # OllamaProvider with .embed(), or None
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, tags TEXT DEFAULT '',
            created REAL, archived INTEGER DEFAULT 0, embedding TEXT)""")
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, tags, content='memories', content_rowid='id')")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()
        self._embed_ok = embedder is not None

    def _embed(self, text: str) -> list[float] | None:
        if not self._embed_ok:
            return None
        try:
            vecs = self.embedder.embed([text])
            return vecs[0] if vecs else None
        except Exception:
            self._embed_ok = False  # embedding model missing; fall back to keywords
            return None

    def add(self, text: str, tags: str = "") -> int:
        emb = self._embed(text)
        with self._lock:
            cur = self.db.execute("INSERT INTO memories(text, tags, created, embedding) VALUES (?,?,?,?)",
                                  (text, tags, time.time(), json.dumps(emb) if emb else None))
            mid = cur.lastrowid
            if self.fts:
                self.db.execute("INSERT INTO memories_fts(rowid, text, tags) VALUES (?,?,?)", (mid, text, tags))
            self.db.commit()
        return mid

    def archive(self, mid: int) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE memories SET archived=1 WHERE id=?", (mid,))
            self.db.commit()
            return cur.rowcount > 0

    def recent(self, n: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT id, text, tags, created FROM memories WHERE archived=0 ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(id=r[0], text=r[1], tags=r[2], created=r[3]) for r in rows]

    def search(self, query: str, k: int = 5) -> list[dict]:
        scores: dict[int, float] = {}
        words = re.findall(r"\w{3,}", query.lower())
        if self.fts and words:
            q = " OR ".join(f'"{w}"' for w in words[:12])
            try:
                rows = self.db.execute(
                    "SELECT m.id, bm25(memories_fts) FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? AND m.archived=0 ORDER BY bm25(memories_fts) LIMIT 30", (q,)).fetchall()
                for rank, (mid, _) in enumerate(rows):
                    scores[mid] = scores.get(mid, 0) + 1.0 / (rank + 1)
            except sqlite3.OperationalError:
                pass
        elif words:
            for mid, text in self.db.execute("SELECT id, text FROM memories WHERE archived=0").fetchall():
                hits = sum(w in text.lower() for w in words)
                if hits:
                    scores[mid] = hits / len(words)
        qv = self._embed(query)
        if qv:
            rows = self.db.execute("SELECT id, embedding FROM memories WHERE archived=0 AND embedding IS NOT NULL").fetchall()
            sims = sorted(((mid, _cos(qv, json.loads(e))) for mid, e in rows), key=lambda x: -x[1])[:30]
            for rank, (mid, sim) in enumerate(sims):
                if sim > 0.35:
                    scores[mid] = scores.get(mid, 0) + 1.0 / (rank + 1)
        top = sorted(scores.items(), key=lambda x: -x[1])[:k]
        out = []
        for mid, s in top:
            r = self.db.execute("SELECT id, text, tags, created FROM memories WHERE id=?", (mid,)).fetchone()
            if r:
                out.append(dict(id=r[0], text=r[1], tags=r[2], created=r[3], score=round(s, 3)))
        return out


def _cos(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
