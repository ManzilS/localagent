"""Use LocalAgent from your phone, anywhere, through Tailscale.

Tailscale puts your phone and PC on a private network. `tailscale serve`
makes LocalAgent reachable at https://<your-pc>.<tailnet>.ts.net — only
from devices signed into *your* Tailscale account, with HTTPS, and with no
ports opened on your router. On top of that, the phone needs a pairing link
(a long random key); revoking it locks every paired phone out.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess

DEFAULT_PORT = 7860


def _ts() -> str | None:
    exe = shutil.which("tailscale")
    if not exe:
        for p in (r"C:\Program Files\Tailscale\tailscale.exe", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
            if os.path.exists(p):
                return p
    return exe


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    exe = _ts()
    if not exe:
        raise RuntimeError("Tailscale isn't installed")
    return subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout)


def info() -> dict:
    out = {"installed": _ts() is not None, "running": False, "dns": "", "ip": ""}
    if not out["installed"]:
        return out
    try:
        r = _run(["status", "--json"], timeout=10)
        data = json.loads(r.stdout or "{}")
        me = data.get("Self") or {}
        out["running"] = data.get("BackendState") == "Running"
        out["dns"] = (me.get("DNSName") or "").rstrip(".")
        out["ip"] = (me.get("TailscaleIPs") or [""])[0]
    except Exception:
        pass
    return out


def serving(port: int) -> bool:
    try:
        r = _run(["serve", "status", "--json"], timeout=10)
        return f"127.0.0.1:{port}" in (r.stdout or "") or f"localhost:{port}" in (r.stdout or "")
    except Exception:
        return False


def start_serving(port: int) -> None:
    r = _run(["serve", "--bg", str(port)], timeout=60)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        if "https" in msg.lower() or "certificate" in msg.lower() or "enable" in msg.lower():
            raise RuntimeError("Tailscale needs HTTPS turned on for your network: open login.tailscale.com/admin/dns, "
                               "enable MagicDNS and HTTPS Certificates, then try again. (" + msg[:200] + ")")
        raise RuntimeError(msg[:300] or "tailscale serve failed")


def stop_serving() -> None:
    _run(["serve", "--https=443", "off"], timeout=30)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def qr_svg(text: str) -> str:
    try:
        import segno  # type: ignore
    except ImportError:
        return ""
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#17203A", light="#ffffff")
    return buf.getvalue().decode()
