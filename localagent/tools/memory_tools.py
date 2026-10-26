from __future__ import annotations

from datetime import datetime

from . import tool


@tool("remember", "Save a durable fact for future sessions: the user's preferences, project details, "
      "decisions, where things live. Not for temporary task state.",
      {"text": {"type": "string"}, "tags": {"type": "string", "description": "comma-separated"}}, ["text"], group="memory")
def remember(rt, text: str, tags: str = "") -> str:
    mid = rt.memory.add(text.strip(), tags)
    return f"saved memory #{mid}"


@tool("recall", "Search long-term memory for relevant facts.",
      {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"], group="memory")
def recall(rt, query: str, k: int = 5) -> str:
    hits = rt.memory.search(query, int(k or 5))
    if not hits:
        return "no matching memories"
    return "\n".join(f"#{h['id']} ({datetime.fromtimestamp(h['created']):%Y-%m-%d}) {h['text']}" for h in hits)


@tool("archive_memory", "Retire an outdated or wrong memory by id (kept on disk, no longer recalled).",
      {"id": {"type": "integer"}}, ["id"], group="memory")
def archive_memory(rt, id: int) -> str:
    return f"archived memory #{id}" if rt.memory.archive(int(id)) else f"no memory #{id}"
