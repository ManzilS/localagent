"""Shell, Python, and background processes (dev servers, watchers, long jobs).
Every subprocess gets the never-delete environment from safety.build_env()."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

from ..safety import IS_WINDOWS, POWERSHELL_LAYER, _redirect_targets, build_env, check_shell_command, resolve_path
from . import tool, truncate


def shell_argv(command: str) -> list[str]:
    if IS_WINDOWS:
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if not ps:  # no PowerShell: refuse rather than run unprotected cmd.exe
            raise RuntimeError("PowerShell not found; LocalAgent needs it to run commands safely on Windows")
        # The protection layer loads first. If it fails to load, stop instead of running unprotected.
        script = POWERSHELL_LAYER + "\nif (-not $global:__la_ready) { Write-Error 'protection layer failed'; exit 97 }\n" + command
        return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    sh = shutil.which("bash") or "/bin/sh"
    return [sh, "-c", command]  # non-login: don't let profiles reorder PATH past the shims


def _popen_kwargs() -> dict:
    if IS_WINDOWS:  # own process group (so the tree can be stopped), and no flashing console window
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}
    return {"start_new_session": True}


def _kill(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        # Stop the whole tree (PowerShell plus whatever it started, e.g. a dev
        # server). CTRL_BREAK can't be delivered without a console, so use taskkill.
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _backup_redirect_targets(rt, command: str, cwd) -> None:
    """PowerShell `>` overwrites silently; save the old file to agent_trash first."""
    if not IS_WINDOWS:
        return
    from pathlib import Path
    for t in _redirect_targets(command):
        p = Path(t) if Path(t).is_absolute() else Path(cwd) / t
        if p.is_file():
            rt.trash.backup(p, reason="about to be overwritten by >")


def _run(rt, argv: list[str], cwd, timeout: int) -> str:
    proc = subprocess.Popen(argv, cwd=cwd, env=build_env(rt.cfg), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **_popen_kwargs())
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, _ = proc.communicate()
        code, timed_out = proc.returncode, True
    text = (out or b"").decode("utf-8", errors="replace")
    status = f"[timed out after {timeout}s — for long-running things use start_process]" if timed_out else f"[exit code {code}]"
    return truncate(text.strip(), rt.cfg.agent["max_tool_output_chars"]) + f"\n{status}"


@tool("run_shell", "Run a shell command and wait for it (bash on Linux/Mac, PowerShell on Windows). "
      "Use for git, package installs, builds, tests, system info. `rm` is safe: it moves to agent_trash. "
      "For servers or anything that keeps running, use start_process.",
      {"command": {"type": "string"}, "cwd": {"type": "string", "description": "default: workspace"},
       "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"])
def run_shell(rt, command: str, cwd: str = ".", timeout: int | None = None) -> str:
    refusal = check_shell_command(command)
    if refusal:
        return refusal
    wd = resolve_path(rt.cfg, cwd)
    wd.mkdir(parents=True, exist_ok=True)
    _backup_redirect_targets(rt, command, wd)
    return _run(rt, shell_argv(command), str(wd), int(timeout or rt.cfg.agent["shell_timeout"]))


@tool("run_python", "Run a Python script and return its output. Great for calculations, data analysis, "
      "charts (save with matplotlib to a file), file conversion, scraping. The script is saved for reference.",
      {"code": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, ["code"])
def run_python(rt, code: str, cwd: str = ".", timeout: int | None = None) -> str:
    runs = rt.cfg.home / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    script = runs / f"run_{datetime.now():%Y%m%d_%H%M%S_%f}.py"
    script.write_text(code, encoding="utf-8")
    wd = resolve_path(rt.cfg, cwd)
    wd.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "--run-python", str(script)] if getattr(sys, "frozen", False) else [sys.executable, str(script)]
    return _run(rt, argv, str(wd), int(timeout or rt.cfg.agent["shell_timeout"]))


class BackgroundProcess:
    def __init__(self, pid_label: str, command: str, proc: subprocess.Popen):
        self.label, self.command, self.proc = pid_label, command, proc
        self.buffer: deque[str] = deque(maxlen=2000)
        self.read_pos = 0
        self.total = 0
        self.started = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        assert self.proc.stdout
        for raw in iter(self.proc.stdout.readline, b""):
            self.buffer.append(raw.decode("utf-8", errors="replace"))
            self.total += 1

    def new_output(self) -> str:
        lines = list(self.buffer)
        first_kept = self.total - len(lines)
        start = max(self.read_pos - first_kept, 0)
        self.read_pos = self.total
        return "".join(lines[start:])


@tool("start_process", "Start a long-running command in the background (web servers, watchers, training runs). "
      "Returns an id. Check it with read_process; stop it with stop_process.",
      {"command": {"type": "string"}, "cwd": {"type": "string"}}, ["command"])
def start_process(rt, command: str, cwd: str = ".") -> str:
    refusal = check_shell_command(command)
    if refusal:
        return refusal
    wd = resolve_path(rt.cfg, cwd)
    wd.mkdir(parents=True, exist_ok=True)
    _backup_redirect_targets(rt, command, wd)
    proc = subprocess.Popen(shell_argv(command), cwd=str(wd), env=build_env(rt.cfg), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **_popen_kwargs())
    label = f"p{len(rt.processes) + 1}"
    rt.processes[label] = BackgroundProcess(label, command, proc)
    time.sleep(1.5)
    first = rt.processes[label].new_output()
    state = "running" if proc.poll() is None else f"exited with {proc.returncode}"
    return f"started {label} ({state}): {command}\n{truncate(first, 3000)}"


@tool("read_process", "Get new output from a background process and whether it is still running.",
      {"id": {"type": "string"}, "wait": {"type": "integer", "description": "seconds to wait first, max 60"}}, ["id"])
def read_process(rt, id: str, wait: int = 0) -> str:
    bp = rt.processes.get(id)
    if not bp:
        return f"no process {id}. Running: {', '.join(rt.processes) or 'none'}"
    if wait:
        time.sleep(min(int(wait), 60))
    code = bp.proc.poll()
    state = "running" if code is None else f"exited with code {code}"
    return f"{id} {state}\n" + truncate(bp.new_output() or "(no new output)", rt.cfg.agent["max_tool_output_chars"])


@tool("stop_process", "Stop a background process started with start_process.", {"id": {"type": "string"}}, ["id"])
def stop_process(rt, id: str) -> str:
    bp = rt.processes.get(id)
    if not bp:
        return f"no process {id}"
    if bp.proc.poll() is None:
        _kill(bp.proc)
    return f"stopped {id}"


@tool("list_processes", "List background processes.", {})
def list_processes(rt) -> str:
    if not rt.processes:
        return "no background processes"
    return "\n".join(f"{k}: {'running' if v.proc.poll() is None else 'exited ' + str(v.proc.returncode)}  "
                     f"{int(time.time() - v.started)}s  {v.command}" for k, v in rt.processes.items())
