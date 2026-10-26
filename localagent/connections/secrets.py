"""Passwords and tokens for connections.

Stored in the operating system's keychain (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) through `keyring`, never in
config.yaml. If no keychain is available, falls back to a file next to the
config that only your user account can read."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

SERVICE = "LocalAgent"
_lock = threading.Lock()


def _backend():
    if os.environ.get("LOCALAGENT_NO_KEYCHAIN"):
        return None
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__name__.lower():
            return None
        return keyring
    except Exception:
        return None


class Secrets:
    def __init__(self, home: Path, use_keychain: bool = True):
        self.file = home / "secrets.json"
        self.kr = _backend() if use_keychain else None

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _store(self, data: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.file)

    def get(self, key: str) -> str:
        if self.kr is not None:
            try:
                v = self.kr.get_password(SERVICE, key)
                if v is not None:
                    return v
            except Exception:
                pass
        with _lock:
            return self._load().get(key, "")

    def set(self, key: str, value: str) -> None:
        if self.kr is not None:
            try:
                self.kr.set_password(SERVICE, key, value)
                return
            except Exception:
                pass   # e.g. value too long for Windows Credential Manager: use the file
        with _lock:
            d = self._load()
            d[key] = value
            self._store(d)

    def forget(self, key: str) -> None:
        if self.kr is not None:
            try:
                self.kr.delete_password(SERVICE, key)
            except Exception:
                pass
        with _lock:
            d = self._load()
            if key in d:
                d.pop(key)
                self._store(d)
