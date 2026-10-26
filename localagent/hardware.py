"""Hardware detection and model setup."""
from __future__ import annotations

import platform
import shutil
import subprocess

import httpx

from .config import PROFILES


def detect() -> dict:
    info = {"os": platform.system(), "arch": platform.machine(), "ram_gb": 0.0, "gpu": None, "vram_gb": 0.0}
    try:
        import psutil
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:
        pass
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            if out:
                name, mem = out[0].rsplit(",", 1)
                info["gpu"], info["vram_gb"] = name.strip(), round(float(mem) / 1024, 1)
        except Exception:
            pass
    if info["os"] == "Darwin" and info["arch"] == "arm64":
        info["gpu"] = "Apple Silicon (unified memory)"
        info["vram_gb"] = round(info["ram_gb"] * 0.7, 1)  # Metal can use ~70% of RAM
    return info


def pick_profile(info: dict) -> str:
    v = info.get("vram_gb") or 0
    if v >= 15.5:
        return "gpu-xl"
    if v >= 10:
        return "gpu-large"
    if v >= 6:
        return "gpu-small"
    return "laptop"


def ollama_running(base_url: str = "http://localhost:11434") -> bool:
    try:
        return httpx.get(f"{base_url}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


def installed_models(base_url: str = "http://localhost:11434") -> set[str]:
    try:
        data = httpx.get(f"{base_url}/api/tags", timeout=5).json()
        names = set()
        for m in data.get("models", []):
            n = m.get("name", "")
            names.add(n)
            if n.endswith(":latest"):
                names.add(n[: -len(":latest")])
        return names
    except Exception:
        return set()


def pull(model: str) -> bool:
    if not shutil.which("ollama"):
        return False
    return subprocess.run(["ollama", "pull", model]).returncode == 0


def ensure_model(candidates: list[str], base_url: str, have: set[str], log=print) -> str | None:
    for m in candidates:
        if m in have:
            log(f"  ✓ {m} already installed")
            return m
    for m in candidates:
        log(f"  ↓ pulling {m} ...")
        if pull(m):
            return m
        log(f"  ✗ {m} not available, trying next")
    return None


def profile_table() -> str:
    rows = []
    for name, p in PROFILES.items():
        rows.append(f"{name:10}  main: {p['main_candidates'][0]:12} vision: {p['vision_candidates'][0]:12} "
                    f"ctx: {p['context_tokens']}")
    return "\n".join(rows)
