"""The agent never deletes. Everything "deleted" is moved here instead.

Layout:
    agent_trash/
        manifest.jsonl                         one JSON line per trashed item
        20261023-102000-123456-ab12/           one folder per trash event
            home/nick/project/old_file.txt     original absolute path, mirrored

This module is stdlib-only on purpose: the sitecustomize hook and the rm/mv
shims load it inside arbitrary Python interpreters the agent launches.
It captures the real os functions at import time so it still works after
the hook has patched os.remove & friends.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# Real primitives, captured before anything can monkeypatch them.
_rename = os.rename
_unlink = os.unlink
_rmdir = os.rmdir
_scandir = os.scandir
_copy2 = shutil.copy2
_copystat = shutil.copystat

_lock = threading.Lock()


class TrashError(Exception):
    pass


def _mirror_parts(p: Path) -> list[str]:
    """/home/nick/a.txt -> ['home','nick','a.txt'];  C:\\x\\a.txt -> ['C','x','a.txt']"""
    parts = list(p.parts)
    if not parts:
        return ["unknown"]
    anchor = parts[0]
    rest = parts[1:]
    drive = anchor.replace(":", "").strip("\\/")
    return ([drive] if drive else []) + rest


def _hard_remove(p: str) -> None:
    """Remove a tree using the *captured* primitives. Only ever called on the
    source of a completed cross-device copy (so the data already lives in the
    trash) — never on user data that has not been preserved."""
    if os.path.isdir(p) and not os.path.islink(p):
        with _scandir(p) as it:
            for entry in it:
                _hard_remove(entry.path)
        _rmdir(p)
    else:
        _unlink(p)


def _move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        _rename(src, dst)
        return
    except OSError:
        pass  # most likely a cross-device move: copy, verify, then remove source
    if os.path.isdir(src) and not os.path.islink(src):
        _copytree(src, dst)
    else:
        _copy2(src, dst, follow_symlinks=False)
    if not os.path.lexists(dst):
        raise TrashError(f"copy to trash failed for {src}")
    _hard_remove(src)


def _copytree(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    with _scandir(src) as it:
        for entry in it:
            s, d = entry.path, os.path.join(dst, entry.name)
            if entry.is_dir(follow_symlinks=False):
                _copytree(s, d)
            else:
                _copy2(s, d, follow_symlinks=False)
    try:
        _copystat(src, dst)
    except OSError:
        pass


class Trash:
    def __init__(self, trash_dir: str | os.PathLike):
        self.dir = Path(trash_dir).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.dir / "manifest.jsonl"

    # ------------------------------------------------------------ helpers
    def _new_bucket(self) -> tuple[str, Path]:
        tid = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:4]
        return tid, self.dir / tid

    def _record(self, entry: dict) -> None:
        with _lock:
            with open(self.manifest, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def _check(self, src: Path) -> None:
        if not os.path.lexists(src):
            raise TrashError(f"no such file or directory: {src}")
        if src == self.dir or self.dir in src.parents:
            raise TrashError("that is already inside agent_trash; nothing to do")
        if src in self.dir.parents:
            raise TrashError(
                f"refusing to trash {src}: it contains the trash folder itself. "
                "Trash the individual items inside it instead."
            )

    # ------------------------------------------------------------ public API
    def move(self, path: str | os.PathLike, reason: str = "") -> Path:
        """Move a file/folder into the trash. Returns its new location."""
        src = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._check(src)
        tid, bucket = self._new_bucket()
        dest = bucket.joinpath(*_mirror_parts(src))
        _move(str(src), str(dest))
        self._record({
            "id": tid, "action": "move", "original": str(src), "stored": str(dest),
            "time": time.time(), "reason": reason,
        })
        return dest

    def backup(self, path: str | os.PathLike, reason: str = "overwrite") -> Path | None:
        """Copy (not move) a file into the trash before it gets overwritten."""
        src = Path(os.path.abspath(os.path.expanduser(str(path))))
        if not src.exists() or src.is_dir():
            return None
        if self.dir in src.parents:
            return None
        tid, bucket = self._new_bucket()
        dest = bucket.joinpath(*_mirror_parts(src))
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy2(str(src), str(dest))
        self._record({
            "id": tid, "action": "backup", "original": str(src), "stored": str(dest),
            "time": time.time(), "reason": reason,
        })
        return dest

    def entries(self) -> list[dict]:
        if not self.manifest.exists():
            return []
        out = []
        with open(self.manifest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def list(self, limit: int = 30, query: str = "") -> list[dict]:
        items = [e for e in self.entries() if e.get("action") in ("move", "backup", "restored")]
        if query:
            items = [e for e in items if query.lower() in e.get("original", "").lower()]
        live = [e for e in items if e.get("action") != "restored" and os.path.lexists(e.get("stored", ""))]
        return live[-limit:][::-1]

    def restore(self, tid: str, overwrite_backup: bool = True) -> Path:
        """Put a trashed item back. If something now occupies the original
        path, that thing is itself moved to the trash first (never lost)."""
        match = [e for e in self.entries() if e.get("id") == tid and e.get("action") in ("move", "backup")]
        if not match:
            raise TrashError(f"no trash entry with id {tid}")
        e = match[-1]
        stored, original = e["stored"], e["original"]
        if not os.path.lexists(stored):
            raise TrashError(f"trashed copy is missing: {stored}")
        if os.path.lexists(original):
            if not overwrite_backup:
                raise TrashError(f"{original} exists; restore would replace it")
            self.move(original, reason=f"displaced by restore of {tid}")
        _move(stored, original)
        self._record({"id": tid, "action": "restored", "original": original, "time": time.time()})
        return Path(original)
