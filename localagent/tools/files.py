"""File tools. Overwrites and edits back up the previous version into
agent_trash first, so no content is ever lost."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path

from ..safety import check_writable, resolve_path
from . import tool, truncate

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "agent_trash"}
TEXT_LIMIT = 2_000_000


def _limit(rt) -> int:
    return rt.cfg.agent["max_tool_output_chars"]


@tool("read_file", "Read a text file. Returns numbered lines. Use offset/limit for big files. "
      "For PDFs, Word, Excel, images use read_document / look_at instead.",
      {"path": {"type": "string"}, "offset": {"type": "integer", "description": "first line (1-based)"},
       "limit": {"type": "integer", "description": "max lines, default 400"}}, ["path"])
def read_file(rt, path: str, offset: int = 1, limit: int = 400) -> str:
    p = resolve_path(rt.cfg, path)
    if p.is_dir():
        return f"{p} is a directory; use list_dir"
    if p.stat().st_size > TEXT_LIMIT * 5:
        return f"{p} is {p.stat().st_size:,} bytes; read a slice with offset/limit or use search_text"
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    offset = max(1, int(offset or 1))
    chunk = lines[offset - 1 : offset - 1 + int(limit or 400)]
    body = "".join(f"{i:>5}\t{l}" for i, l in enumerate(chunk, start=offset))
    more = f"\n[showing lines {offset}-{offset + len(chunk) - 1} of {len(lines)}]" if len(lines) > len(chunk) else ""
    return truncate(body, _limit(rt)) + more


@tool("write_file", "Create or overwrite a text file (parent folders are created). "
      "If the file exists its old version is saved to agent_trash first.",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
def write_file(rt, path: str, content: str) -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    backed = rt.trash.backup(p, reason="overwritten by write_file") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    note = " (previous version saved to agent_trash)" if backed else ""
    return f"wrote {len(content):,} chars to {p}{note}"


@tool("edit_file", "Replace exact text in a file. old_text must match exactly once unless replace_all=true. "
      "Read the file first. Previous version is saved to agent_trash.",
      {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
       "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text"])
def edit_file(rt, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        # help small models: show the closest line
        first = old_text.strip().splitlines()[0] if old_text.strip() else ""
        hint = [l for l in text.splitlines() if first and first.strip()[:40] in l][:3]
        return "ERROR: old_text not found." + (f" Similar lines: {hint}" if hint else " Re-read the file and copy text exactly.")
    if count > 1 and not replace_all:
        return f"ERROR: old_text appears {count} times; add more surrounding context or set replace_all=true"
    rt.trash.backup(p, reason="edited by edit_file")
    p.write_text(text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"edited {p} ({count if replace_all else 1} replacement{'s' if replace_all and count > 1 else ''})"


@tool("append_file", "Append text to the end of a file (creates it if missing).",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
def append_file(rt, path: str, content: str) -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content)
    return f"appended {len(content):,} chars to {p}"


@tool("list_dir", "List a folder's contents (with sizes). depth>1 shows a tree.",
      {"path": {"type": "string", "description": "default: workspace"}, "depth": {"type": "integer"},
       "show_hidden": {"type": "boolean"}})
def list_dir(rt, path: str = ".", depth: int = 1, show_hidden: bool = False) -> str:
    root = resolve_path(rt.cfg, path)
    if not root.is_dir():
        return f"{root} is not a directory"
    lines, count = [str(root)], 0
    depth = max(1, min(int(depth or 1), 5))

    def walk(d: Path, level: int):
        nonlocal count
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            lines.append("  " * level + "[permission denied]")
            return
        for e in entries:
            if not show_hidden and e.name.startswith("."):
                continue
            count += 1
            if count > 500:
                return
            if e.is_dir():
                lines.append("  " * level + e.name + "/")
                if level < depth and e.name not in SKIP_DIRS:
                    walk(e, level + 1)
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append("  " * level + f"{e.name}  ({_human(size)})")

    walk(root, 1)
    if count > 500:
        lines.append("... [truncated at 500 entries]")
    return truncate("\n".join(lines), _limit(rt))


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


@tool("find_files", "Find files by glob pattern, e.g. '*.py' or '**/report*.pdf'.",
      {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])
def find_files(rt, pattern: str, path: str = ".") -> str:
    root = resolve_path(rt.cfg, path)
    pat = pattern.replace("**/", "")
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            if fnmatch.fnmatch(fn, pat) or fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
                if len(hits) >= 300:
                    break
        if len(hits) >= 300:
            break
    return "\n".join(hits) if hits else f"no files matching {pattern} under {root}"


@tool("search_text", "Search file contents with a regex (like grep -rn). Returns path:line: text.",
      {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string", "description": "e.g. *.py"},
       "ignore_case": {"type": "boolean"}}, ["pattern"])
def search_text(rt, pattern: str, path: str = ".", glob: str = "*", ignore_case: bool = False) -> str:
    root = resolve_path(rt.cfg, path)
    rx = re.compile(pattern, re.I if ignore_case else 0)
    out = []
    files = [root] if root.is_file() else None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            files += [Path(dirpath) / f for f in filenames if fnmatch.fnmatch(f, glob)]
    for f in files:
        try:
            if f.stat().st_size > TEXT_LIMIT:
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append(f"{os.path.relpath(f, root) if f != root else f.name}:{i}: {line.rstrip()[:240]}")
                        if len(out) >= 200:
                            return "\n".join(out) + "\n[stopped at 200 matches]"
        except (OSError, UnicodeError):
            continue
    return "\n".join(out) if out else "no matches"


@tool("move_path", "Move or rename a file/folder. Anything already at the destination is moved to agent_trash first.",
      {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"])
def move_path(rt, source: str, destination: str) -> str:
    src, dst = resolve_path(rt.cfg, source), resolve_path(rt.cfg, destination)
    check_writable(rt.cfg, src)
    check_writable(rt.cfg, dst)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if dst.is_dir():  # same as `mv a dir/` -> dir/a
        dst = dst / src.name
    note = ""
    if dst.exists():
        rt.trash.move(dst, reason=f"replaced by move of {src}")
        note = " (old destination moved to agent_trash)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src} -> {dst}{note}"


@tool("copy_path", "Copy a file or folder. Anything already at the destination is moved to agent_trash first.",
      {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"])
def copy_path(rt, source: str, destination: str) -> str:
    src, dst = resolve_path(rt.cfg, source), resolve_path(rt.cfg, destination)
    check_writable(rt.cfg, dst)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if dst.is_dir():
        dst = dst / src.name
    note = ""
    if dst.exists():
        rt.trash.move(dst, reason=f"replaced by copy of {src}")
        note = " (old destination moved to agent_trash)"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return f"copied {src} -> {dst}{note}"


@tool("make_dir", "Create a folder (and parents).", {"path": {"type": "string"}}, ["path"])
def make_dir(rt, path: str) -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    p.mkdir(parents=True, exist_ok=True)
    return f"created {p}"


@tool("trash_path", "The ONLY way to remove a file or folder: moves it into agent_trash (recoverable). "
      "Nothing is ever permanently deleted.",
      {"path": {"type": "string"}, "reason": {"type": "string"}}, ["path"])
def trash_path(rt, path: str, reason: str = "") -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    dest = rt.trash.move(p, reason=reason)
    return f"moved {p} to agent_trash ({dest})"


@tool("list_trash", "Show recently trashed items with their ids (for restore_from_trash).",
      {"query": {"type": "string", "description": "filter by path substring"}, "limit": {"type": "integer"}})
def list_trash(rt, query: str = "", limit: int = 20) -> str:
    items = rt.trash.list(limit=int(limit or 20), query=query)
    if not items:
        return "agent_trash is empty" + (f" for '{query}'" if query else "")
    return "\n".join(f"{e['id']}  [{e['action']}]  {e['original']}" + (f"  — {e['reason']}" if e.get("reason") else "")
                     for e in items)


@tool("restore_from_trash", "Restore a trashed item to its original location by id (see list_trash). "
      "Whatever now sits at that path is moved to the trash first.",
      {"id": {"type": "string"}}, ["id"])
def restore_from_trash(rt, id: str) -> str:
    p = rt.trash.restore(id)
    return f"restored {p}"
