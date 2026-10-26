"""Backs the rm/rmdir/unlink/shred/mv/cp shims placed on the agent's PATH.

    rm -rf build/         -> build/ is moved into agent_trash
    mv new.txt old.txt    -> old.txt is moved into agent_trash, then mv runs
    cp a.txt b.txt        -> b.txt is moved into agent_trash, then cp runs
"""
from __future__ import annotations

import os
import shutil
import sys

from .trash import Trash, TrashError

REMOVERS = {"rm", "rmdir", "unlink", "shred"}


def _real_binary(name: str) -> str | None:
    shim_dir = os.path.realpath(os.environ.get("LOCALAGENT_SHIM_DIR", "")) if os.environ.get("LOCALAGENT_SHIM_DIR") else None
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.realpath(p) != shim_dir]
    return shutil.which(name, path=os.pathsep.join(parts))


def _exempt(path: str) -> bool:
    rp = os.path.realpath(os.path.abspath(path))
    for w in os.environ.get("LOCALAGENT_PROTECT", "").split(os.pathsep):
        if w and (rp == w or rp.startswith(w.rstrip(os.sep) + os.sep)):
            return False
    for e in os.environ.get("LOCALAGENT_TRASH_EXEMPT", "").split(os.pathsep):
        if e and (rp == e or rp.startswith(e.rstrip(os.sep) + os.sep)):
            return True
    return False


def _split(args: list[str]) -> tuple[list[str], list[str]]:
    flags, operands, end = [], [], False
    for a in args:
        if not end and a == "--":
            end = True
        elif not end and a.startswith("-") and a != "-":
            flags.append(a)
        else:
            operands.append(a)
    return flags, operands


def _remove(name: str, args: list[str], trash: Trash) -> int:
    flags, paths = _split(args)
    force = any(f in ("-f", "--force") or (f.startswith("-") and not f.startswith("--") and "f" in f) for f in flags)
    if not paths and not force:
        print(f"{name}: missing operand", file=sys.stderr)
        return 1
    real = _real_binary(name)
    status = 0
    for p in paths:
        if not os.path.lexists(p):
            if not force:
                print(f"{name}: cannot remove '{p}': No such file or directory", file=sys.stderr)
                status = 1
            continue
        if _exempt(p) and real:
            rc = os.spawnv(os.P_WAIT, real, [name] + flags + ["--", p])
            status = status or rc
            continue
        try:
            trash.move(p, reason=f"{name} {' '.join(flags)}".strip())
        except TrashError as e:
            print(f"{name}: {p}: {e}", file=sys.stderr)
            status = 1
    if paths and status == 0 and os.environ.get("LOCALAGENT_SHIM_VERBOSE"):
        print(f"[moved to agent_trash: {', '.join(paths)}]", file=sys.stderr)
    return status


def _overwrite_guard(name: str, args: list[str], trash: Trash) -> int:
    flags, operands = _split(args)
    no_clobber = any(f in ("-n", "--no-clobber", "--update=none") for f in flags)
    target_dir = None
    for i, a in enumerate(args):
        if a == "-t" and i + 1 < len(args):
            target_dir = args[i + 1]
        elif a.startswith("--target-directory="):
            target_dir = a.split("=", 1)[1]
    if target_dir and target_dir in operands:
        operands.remove(target_dir)
    if not no_clobber and operands:
        if target_dir:
            sources, dest, into_dir = operands, target_dir, True
        elif len(operands) >= 2:
            sources, dest = operands[:-1], operands[-1]
            into_dir = os.path.isdir(dest) and "-T" not in flags and "--no-target-directory" not in flags
        else:
            sources, dest, into_dir = [], None, False
        targets = [os.path.join(dest, os.path.basename(s.rstrip("/"))) for s in sources] if into_dir else ([dest] if dest else [])
        for t in targets:
            if t and os.path.isfile(t) and not _exempt(t):
                try:
                    trash.move(t, reason=f"would be overwritten by {name}")
                except TrashError:
                    pass
    real = _real_binary(name)
    if not real:
        print(f"{name}: real binary not found", file=sys.stderr)
        return 127
    os.execv(real, [name] + args)
    return 0  # unreachable


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(2)
    name, args = sys.argv[1], sys.argv[2:]
    trash_dir = os.environ.get("LOCALAGENT_TRASH")
    if not trash_dir:
        print(f"{name}: LOCALAGENT_TRASH not set; refusing to run", file=sys.stderr)
        sys.exit(1)
    trash = Trash(trash_dir)
    if name == "trash":      # used by the Windows PowerShell layer
        status = 0
        for p in args:
            if not os.path.lexists(p):
                continue
            if _exempt(p):
                continue
            try:
                trash.move(p, reason="Remove-Item")
            except TrashError as e:
                print(f"Remove-Item: {p}: {e}", file=sys.stderr)
                status = 1
        sys.exit(status)
    if name == "backup":     # save a copy of a file that is about to be overwritten
        for p in args:
            if os.path.isfile(p) and not _exempt(p):
                try:
                    trash.backup(p, reason="about to be overwritten")
                except Exception as e:
                    print(f"backup failed for {p}: {e}", file=sys.stderr)
                    sys.exit(1)
        sys.exit(0)
    if name in REMOVERS:
        sys.exit(_remove(name, args, trash))
    sys.exit(_overwrite_guard(name, args, trash))


if __name__ == "__main__":
    main()
