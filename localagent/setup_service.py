"""Setup and settings logic shared by the GUI and the CLI: system status,
model downloads with progress, Ollama install/start, saving settings."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import webbrowser
from typing import Iterator

import httpx
import yaml

from .bridges import BRIDGES, open_terminal
from .config import CONFIG_PATH, DEFAULTS, PROFILES, deep_merge, profile_overrides, save_config
from .hardware import detect, installed_models, ollama_running, pick_profile

IS_WIN = os.name == "nt"
IS_MAC = platform.system() == "Darwin"


def read_user_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def write_user_config(data: dict) -> None:
    save_config(data)
    try:
        os.chmod(CONFIG_PATH, 0o600)  # may hold an API key
    except OSError:
        pass


def update_user_config(patch: dict) -> dict:
    data = deep_merge(read_user_config(), patch)
    write_user_config(data)
    return data


_HW: dict | None = None
_BRIDGE_CACHE: dict = {"t": 0.0, "v": None}


def _hardware() -> dict:
    global _HW
    if _HW is None:
        _HW = detect()  # hardware doesn't change while the app is open
    return _HW


def _bridge_statuses() -> dict:
    import time as _t
    if _BRIDGE_CACHE["v"] is None or _t.time() - _BRIDGE_CACHE["t"] > 60:
        _BRIDGE_CACHE["v"] = {key: cls().status() for key, cls in BRIDGES.items()}
        _BRIDGE_CACHE["t"] = _t.time()
    return _BRIDGE_CACHE["v"]


def refresh_bridges() -> None:
    _BRIDGE_CACHE["v"] = None


def status(cfg) -> dict:
    hw = _hardware()
    base = cfg.providers["ollama"]["base_url"]
    running = ollama_running(base)
    have = installed_models(base) if running else set()
    models = {}
    for role in ("main", "vision", "embed"):
        spec = cfg.models.get(role) or {}
        m = spec.get("model", "")
        models[role] = {"model": m, "installed": spec.get("provider") != "ollama" or m in have}
    bridges = []
    enabled = {b.get("provider"): b.get("enabled", True) for b in cfg.data.get("brains") or [] if b}
    for key, st in _bridge_statuses().items():
        st = dict(st)
        st["enabled"] = enabled.get(key, False)
        bridges.append(st)
    cloud = cfg.models.get("cloud") or {}
    has_key = bool(cloud and (cloud.get("api_key") or (cloud.get("api_key_env") and os.environ.get(cloud["api_key_env"]))))
    return {
        "setup_done": bool(read_user_config().get("setup_done")),
        "hardware": hw,
        "recommended_profile": pick_profile(hw),
        "profile": cfg["profile"],
        "profiles": {k: {"main": v["main_candidates"][0], "vision": v["vision_candidates"][0]} for k, v in PROFILES.items()},
        "ollama": {"installed": shutil.which("ollama") is not None, "running": running},
        "models": models,
        "bridges": bridges,
        "api": {"provider": cloud.get("provider", "anthropic"), "model": cloud.get("model", ""), "has_key": has_key},
        "workspace": str(cfg.workspace),
        "trash": str(cfg.trash_dir),
        "desktop": cfg.data.get("desktop") or {},
        "os": platform.system(),
    }


def install_ollama() -> str:
    if IS_WIN:
        if shutil.which("winget"):
            open_terminal("winget install -e --id Ollama.Ollama", "Install Ollama")
            return "installer opened in a new window"
        webbrowser.open("https://ollama.com/download/windows")
        return "download page opened"
    if IS_MAC:
        webbrowser.open("https://ollama.com/download/mac")
        return "download page opened"
    open_terminal("curl -fsSL https://ollama.com/install.sh | sh", "Install Ollama")
    return "installer opened in a new window"


def start_ollama() -> str:
    exe = shutil.which("ollama")
    if not exe:
        raise RuntimeError("Ollama isn't installed yet")
    if IS_MAC and os.path.exists("/Applications/Ollama.app"):
        subprocess.Popen(["open", "-a", "Ollama"])
    else:
        kw = {"creationflags": 0x08000000} if IS_WIN else {"start_new_session": True}  # no console window
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    return "starting"


def pull_stream(base_url: str, model: str) -> Iterator[dict]:
    """Yield {status, completed, total} while Ollama downloads a model."""
    with httpx.stream("POST", f"{base_url}/api/pull", json={"model": model, "stream": True}, timeout=None) as r:
        for line in r.iter_lines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield ev
            if ev.get("error"):
                return


def download_profile(cfg, profile: str) -> Iterator[dict]:
    """Pull main, vision and embedding models for a profile, trying fallbacks.
    Yields progress events; the final event says which models were chosen."""
    base = cfg.providers["ollama"]["base_url"]
    p = PROFILES[profile]
    have = installed_models(base)
    chosen = {}
    plan = [("main", p["main_candidates"]), ("vision", p["vision_candidates"]), ("embed", ["nomic-embed-text", "embeddinggemma"])]
    for role, candidates in plan:
        if role == "vision" and chosen.get("main") in candidates:
            chosen["vision"] = chosen["main"]  # one model can do both
            continue
        for m in candidates:
            if m in have:
                chosen[role] = m
                yield {"role": role, "model": m, "status": "already downloaded", "pct": 100}
                break
            ok = True
            for ev in pull_stream(base, m):
                if ev.get("error"):
                    ok = False
                    yield {"role": role, "model": m, "status": "not available, trying another", "pct": 0}
                    break
                total, done = ev.get("total"), ev.get("completed")
                pct = int(done * 100 / total) if total and done else None
                yield {"role": role, "model": m, "status": ev.get("status", ""), "pct": pct}
            if ok:
                chosen[role] = m
                yield {"role": role, "model": m, "status": "ready", "pct": 100}
                break
    data = deep_merge(read_user_config(), profile_overrides(profile))
    data.setdefault("models", {})
    for role, m in chosen.items():
        data["models"][role] = {"provider": "ollama", "model": m}
    write_user_config(data)
    yield {"done": True, "chosen": chosen}


def save_settings(body: dict) -> dict:
    """Apply settings from the GUI. Only known keys are accepted."""
    patch: dict = {}
    if "workspace" in body and body["workspace"]:
        patch["workspace"] = body["workspace"]
    if "profile" in body and body["profile"] in PROFILES:
        patch = deep_merge(patch, profile_overrides(body["profile"]))
    if "desktop_enabled" in body or "confirm" in body:
        d = dict(read_user_config().get("desktop") or DEFAULTS["desktop"])
        if "desktop_enabled" in body:
            d["enabled"] = bool(body["desktop_enabled"])
        if "confirm" in body:
            presets = {"delete": DEFAULTS["desktop"]["confirm_patterns"][0], "money": DEFAULTS["desktop"]["confirm_patterns"][1],
                       "send": r"\b(send|post|publish|submit|reply all)\b"}
            d["confirm_patterns"] = [presets[k] for k in body["confirm"] if k in presets]
        patch["desktop"] = d
    if "brains" in body:  # [{"provider": "chatgpt", "enabled": true}, ...] in priority order
        patch["brains"] = [{"provider": b["provider"], "enabled": bool(b.get("enabled", True)),
                            **({"model": b["model"]} if b.get("model") else {})}
                           for b in body["brains"] if b.get("provider") in BRIDGES]
    if "api" in body:
        a = body["api"]
        defaults = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5.5", "openrouter": ""}
        prov = a.get("provider", "anthropic")
        if prov in defaults:
            spec = {"provider": prov, "model": a.get("model") or defaults[prov]}
            if a.get("key"):
                spec["api_key"] = a["key"].strip()
            else:
                old = (read_user_config().get("models") or {}).get("cloud") or {}
                if old.get("api_key") and old.get("provider") == prov:
                    spec["api_key"] = old["api_key"]
                else:
                    spec["api_key_env"] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                                           "openrouter": "OPENROUTER_API_KEY"}[prov]
            patch.setdefault("models", {})["cloud"] = spec
    if body.get("setup_done"):
        patch["setup_done"] = True
    data = read_user_config()
    # lists replace, they don't merge
    for k in ("brains",):
        if k in patch:
            data.pop(k, None)
    if "desktop" in patch:
        data.pop("desktop", None)
    data = deep_merge(data, patch)
    write_user_config(data)
    return data


# ------------------------------------------------------------ autostart
def _launch_command() -> list[str]:
    import sys
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return [exe, "-m", "localagent.launcher", "--minimized"]


def _autostart_path():
    from pathlib import Path
    if IS_WIN:
        return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "LocalAgent.cmd"
    if IS_MAC:
        return Path.home() / "Library" / "LaunchAgents" / "dev.localagent.app.plist"
    return Path.home() / ".config" / "autostart" / "localagent.desktop"


def autostart_enabled() -> bool:
    try:
        return _autostart_path().exists()
    except Exception:
        return False


def set_autostart(enabled: bool) -> None:
    """Start LocalAgent at login so automations and the phone keep working.
    Turning it off moves the startup file to agent_trash (never deleted)."""
    p = _autostart_path()
    if not enabled:
        if p.exists():
            from .config import load_config
            from .trash import Trash
            Trash(load_config().trash_dir).move(p, reason="turned off start-at-login")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    cmd = _launch_command()
    if IS_WIN:
        p.write_text('@echo off\r\nstart "" ' + " ".join(f'"{c}"' if " " in c or c.endswith(".exe") else c for c in cmd) + "\r\n",
                     encoding="utf-8")
    elif IS_MAC:
        args = "".join(f"<string>{c}</string>" for c in cmd)
        p.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                     f'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                     f'<key>Label</key><string>dev.localagent.app</string><key>ProgramArguments</key><array>{args}</array>'
                     f'<key>RunAtLoad</key><true/></dict></plist>\n', encoding="utf-8")
    else:
        import shlex
        p.write_text("[Desktop Entry]\nType=Application\nName=LocalAgent\nExec=" + " ".join(shlex.quote(c) for c in cmd)
                     + "\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
