"""Configuration: defaults, hardware profiles, and YAML loading.

Config lives at ~/.localagent/config.yaml (override with LOCALAGENT_HOME).
Anything in the YAML is deep-merged over DEFAULTS, so a config file only
needs the keys you want to change.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

HOME = Path(os.environ.get("LOCALAGENT_HOME", Path.home() / ".localagent")).expanduser()
CONFIG_PATH = HOME / "config.yaml"

# Hardware profiles. Each lists *candidate* models in preference order;
# `localagent setup` pulls the first one that exists in the Ollama library
# and writes the winner into config.yaml.
PROFILES: dict[str, dict[str, Any]] = {
    # 16 GB+ GPU or 32 GB+ Apple Silicon: room for a 14B model with a big window
    "gpu-xl": {
        "main_candidates": ["qwen3:14b", "qwen3.5:9b", "gemma4:12b", "qwen3:8b"],
        "vision_candidates": ["qwen3-vl:8b", "gemma4:12b", "qwen2.5vl:7b"],
        "context_tokens": 32768,
        "max_tool_output_chars": 12000,
        "max_parallel_subagents": 2,
    },
    # 10-16 GB GPU (e.g. RTX 3060 12 GB). Measured on a 3060: qwen3:8b with a 32K
    # window fits entirely in VRAM at 54 tok/s; qwen3:14b at 32K spills to the CPU
    # and drops to 7 tok/s.
    "gpu-large": {
        "main_candidates": ["qwen3:8b", "qwen3.5:9b", "gemma4:12b", "qwen3:14b"],
        "vision_candidates": ["qwen3-vl:8b", "gemma4:12b", "qwen2.5vl:7b"],
        "context_tokens": 32768,
        "max_tool_output_chars": 12000,
        "max_parallel_subagents": 2,
    },
    # 6-10 GB GPU
    "gpu-small": {
        "main_candidates": ["qwen3.5:9b", "qwen3:8b", "qwen3:4b"],
        "vision_candidates": ["qwen3-vl:4b", "qwen2.5vl:3b"],
        "context_tokens": 16384,
        "max_tool_output_chars": 8000,
        "max_parallel_subagents": 1,
    },
    # The floor: cheap laptop, 8 GB system RAM, CPU only
    "laptop": {
        "main_candidates": ["qwen3:4b", "qwen3.5:4b", "qwen2.5:3b"],
        "vision_candidates": ["qwen3-vl:4b", "qwen3-vl:2b", "qwen2.5vl:3b"],
        "context_tokens": 8192,
        "max_tool_output_chars": 4000,
        "max_parallel_subagents": 1,
    },
}

DEFAULTS: dict[str, Any] = {
    "profile": "laptop",
    "workspace": "~/agent_workspace",
    # Every "delete" becomes a move into this folder. Relative paths are
    # resolved against the workspace.
    "trash_dir": "agent_trash",
    # Folders the agent may write into. Reads are unrestricted.
    "allowed_roots": ["~"],
    "models": {
        "main": {"provider": "ollama", "model": "qwen3:4b"},
        "vision": {"provider": "ollama", "model": "qwen3-vl:4b"},
        "embed": {"provider": "ollama", "model": "nomic-embed-text"},
        # Hybrid: used for /deep mode and auto-escalation. Leave api_key_env
        # unset (or the env var empty) to stay 100% local.
        "cloud": {
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
    },
    # Stronger "brains" for deep mode and for when the local model gets stuck,
    # tried in order. Subscription bridges need no API key — just the official
    # app installed and signed in (see the Brains page in the app). Unavailable
    # ones are skipped. models.cloud (API key) is appended at the end.
    "brains": [
        {"provider": "chatgpt"},
        {"provider": "gemini"},
    ],
    # Operating apps on this computer (mouse, keyboard, screen).
    "desktop": {
        "enabled": True,
        # The agent pauses for your OK before clicking or pressing anything
        # that matches these (case-insensitive regex), e.g. delete buttons.
        "confirm_patterns": [
            r"\b(delete|remove|erase|trash|discard|uninstall|format|wipe|empty|clear all|reset)\b",
            r"\b(buy|purchase|pay|place order|checkout|subscribe|donate|transfer)\b",
        ],
        # What to do if nobody answers an approval request in time: deny.
        "approval_timeout": 300,
    },
    "providers": {
        "ollama": {"base_url": "http://localhost:11434"},
        "anthropic": {"base_url": "https://api.anthropic.com"},
        "openai": {"base_url": "https://api.openai.com/v1"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
    },
    "agent": {
        "max_steps": 40,
        "context_tokens": 8192,
        "max_tool_output_chars": 4000,
        "max_parallel_subagents": 1,
        "max_subagent_depth": 2,
        # auto mode: hand the task to the cloud model after this many
        # consecutive failures (bad tool calls, errors, empty replies)
        "escalate_after_failures": 3,
        # ...or once a task runs this many steps without finishing
        "escalate_after_steps": 18,
        "shell_timeout": 120,
        "temperature": 0.3,
        "auto_recall_memories": 5,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.home = HOME
        self.workspace = Path(data["workspace"]).expanduser().resolve()
        trash = Path(data["trash_dir"]).expanduser()
        self.trash_dir = (trash if trash.is_absolute() else self.workspace / trash).resolve()
        self.allowed_roots = [Path(p).expanduser().resolve() for p in data.get("allowed_roots") or []]
        self.agent = data["agent"]
        self.models = data["models"]
        self.providers = data["providers"]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def ensure_dirs(self) -> None:
        for d in (self.home, self.workspace, self.trash_dir, self.home / "sessions", self.home / "runs"):
            d.mkdir(parents=True, exist_ok=True)


def profile_overrides(profile: str) -> dict[str, Any]:
    p = PROFILES[profile]
    return {
        "profile": profile,
        "models": {
            "main": {"provider": "ollama", "model": p["main_candidates"][0]},
            "vision": {"provider": "ollama", "model": p["vision_candidates"][0]},
        },
        "agent": {
            "context_tokens": p["context_tokens"],
            "max_tool_output_chars": p["max_tool_output_chars"],
            "max_parallel_subagents": p["max_parallel_subagents"],
        },
    }


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    path = path or CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        prof = user.get("profile")
        if prof in PROFILES:
            data = deep_merge(data, profile_overrides(prof))
        data = deep_merge(data, user)
    if overrides:
        data = deep_merge(data, overrides)
    cfg = Config(data)
    cfg.ensure_dirs()
    return cfg


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return path
