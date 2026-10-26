"""Password managers: log in to sites without the AI ever seeing a password.

The agent can ask for a login ("sign in here"); it cannot read a password.
When it asks, LocalAgent:
  1. looks up saved logins whose website matches the page that's open — a
     login for github.com is never offered to a look-alike page,
  2. asks you to approve that fill (app, phone or desktop notification),
  3. gets the username/password (or one-time code) straight from your
     password manager and types it into the page itself.
The secret travels password manager → browser field. It never goes into the
model's context, a log, a file, or LocalAgent's settings.

Supported directly:
  * 1Password — through the official 1Password CLI (`op`) with the desktop
    app's "Integrate with 1Password CLI" setting, so each session is
    unlocked with Windows Hello / Touch ID by you.
  * Bitwarden (and Vaultwarden) — through the official Bitwarden CLI (`bw`).
    Your master password unlocks it for this session only and is never saved.
Any other password manager works through its own browser extension in
LocalAgent's browser.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

_sessions: dict[str, tuple[str, float]] = {}   # Bitwarden session keys, in memory only
_lock = threading.Lock()
SESSION_MINUTES = 60

PROVIDERS = {
    "1password": {"name": "1Password", "cli": "op",
                  "install": "winget install AgileBits.1Password.CLI",
                  "help": "https://developer.1password.com/docs/cli/get-started/"},
    "bitwarden": {"name": "Bitwarden", "cli": "bw",
                  "install": "winget install Bitwarden.CLI",
                  "help": "https://bitwarden.com/help/cli/"},
}


def host_of(url: str) -> str:
    try:
        h = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def same_site(page_host: str, item_host: str) -> bool:
    """Would a careful person accept this saved login for this page?
    Exact host, or one is a subdomain of the other (accounts.google.com ↔
    google.com). Never just "contains"."""
    a, b = page_host.lower().strip("."), item_host.lower().strip(".")
    if not a or not b:
        return False
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True
    return False


def mask(user: str) -> str:
    if not user:
        return "(no username)"
    if "@" in user:
        name, dom = user.split("@", 1)
        return (name[:2] + "•••@" + dom) if len(name) > 2 else ("•••@" + dom)
    return user[:2] + "•••" if len(user) > 3 else "•••"


def find_cli(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    la = os.environ.get("LOCALAPPDATA")
    if la:
        for p in (os.path.join(la, "Microsoft", "WinGet", "Links", f"{name}.exe"),):
            if os.path.exists(p):
                return p
    return None


class PasswordConnection:
    kind = "passwords"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec, self.secrets = cid, spec, secrets
        self.provider = spec.get("provider", "1password")
        self.label = spec.get("name") or PROVIDERS.get(self.provider, {}).get("name", "Password manager")

    # ----------------------------------------------------------- running
    def _run(self, args: list[str], env: dict | None = None, timeout: float = 90) -> str:
        exe = find_cli(PROVIDERS[self.provider]["cli"])
        if not exe:
            raise RuntimeError(f"The {self.label} command-line tool isn't installed. Install it with: "
                               f"{PROVIDERS[self.provider]['install']}")
        e = {**os.environ, **(env or {})}
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, env=e,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError(f"{self.label}: {msg[-1][:300] if msg else 'command failed'}")
        return r.stdout

    # ------------------------------------------------------- bitwarden
    def _bw_env(self) -> dict:
        env = {"BITWARDENCLI_APPDATA_DIR": self.spec.get("bw_dir", "")} if self.spec.get("bw_dir") else {}
        with _lock:
            sess = _sessions.get(self.id)
        if not sess or sess[1] < time.time():
            raise PermissionError(f"{self.label} is locked. Unlock it on the Connections page (your master password "
                                  "is used once and not saved).")
        env["BW_SESSION"] = sess[0]
        return env

    def bw_unlock(self, master_password: str) -> str:
        """Log in with the API key if needed, then unlock. Keeps only the session key, in memory."""
        base = {"BITWARDENCLI_APPDATA_DIR": self.spec["bw_dir"]} if self.spec.get("bw_dir") else {}
        if self.spec.get("server"):
            try:
                self._run(["config", "server", self.spec["server"]], env=base)
            except RuntimeError:
                pass   # already logged in to this server
        st = json.loads(self._run(["status"], env=base) or "{}")
        if st.get("status") == "unauthenticated":
            cid, csec = self.secrets.get(f"{self.id}:client_id"), self.secrets.get(f"{self.id}:client_secret")
            if not (cid and csec):
                raise RuntimeError("Bitwarden needs your API key the first time (Bitwarden web vault → Settings → "
                                   "Security → Keys → View API key).")
            self._run(["login", "--apikey"], env={**base, "BW_CLIENTID": cid, "BW_CLIENTSECRET": csec})
        key = self._run(["unlock", "--passwordenv", "LA_BW_PW", "--raw"], env={**base, "LA_BW_PW": master_password}).strip()
        if not key:
            raise RuntimeError("Bitwarden didn't unlock (wrong master password?)")
        with _lock:
            _sessions[self.id] = (key, time.time() + SESSION_MINUTES * 60)
        return f"Unlocked for {SESSION_MINUTES} minutes."

    def lock(self) -> None:
        with _lock:
            _sessions.pop(self.id, None)

    @property
    def unlocked(self) -> bool:
        if self.provider != "bitwarden":
            return True
        with _lock:
            s = _sessions.get(self.id)
        return bool(s and s[1] > time.time())

    # ---------------------------------------------------------- lookups
    def test(self) -> str:
        if self.provider == "1password":
            vaults = json.loads(self._run(["vault", "list", "--format", "json"]) or "[]")
            return f"Connected: {len(vaults)} 1Password vault{'s' if len(vaults) != 1 else ''}. 1Password asks you to approve with Windows Hello."
        if not self.unlocked:
            return "Added. Unlock it to use it."
        n = len(json.loads(self._run(["list", "items", "--search", ""], env=self._bw_env()) or "[]"))
        return f"Connected: {n} items in Bitwarden."

    def logins_for(self, url: str) -> list[dict]:
        """Saved logins for this page's site. No secrets here — just what to show the user."""
        host = host_of(url)
        out = []
        if self.provider == "1password":
            items = json.loads(self._run(["item", "list", "--categories", "Login", "--format", "json"]) or "[]")
            for it in items:
                urls = [u.get("href", "") for u in it.get("urls") or []]
                if any(same_site(host, host_of(u)) for u in urls if u):
                    out.append({"id": it["id"], "title": it.get("title", ""), "user": mask(it.get("additional_information", "")),
                                "source": self.label})
        else:
            items = json.loads(self._run(["list", "items", "--url", f"https://{host}"], env=self._bw_env()) or "[]")
            for it in items:
                login = it.get("login") or {}
                urls = [u.get("uri", "") for u in login.get("uris") or []]
                if any(same_site(host, host_of(u)) for u in urls if u):
                    out.append({"id": it["id"], "title": it.get("name", ""), "user": mask(login.get("username", "")),
                                "source": self.label, "has_otp": bool(login.get("totp"))})
        return out

    def credentials(self, item_id: str, url: str) -> dict:
        """The secret itself. Only the browser tool calls this, and only after the user approved.
        Re-checks the site so an item can't be steered onto another page."""
        host = host_of(url)
        if self.provider == "1password":
            item = json.loads(self._run(["item", "get", item_id, "--reveal", "--format", "json"]) or "{}")
            urls = [u.get("href", "") for u in item.get("urls") or []]
            if not any(same_site(host, host_of(u)) for u in urls if u):
                raise PermissionError("that saved login belongs to a different website")
            fields = {(f.get("purpose") or f.get("id") or f.get("label") or "").lower(): f.get("value", "")
                      for f in item.get("fields") or []}
            otp = next((f.get("totp") or "" for f in item.get("fields") or [] if f.get("type") == "OTP"), "")
            return {"username": fields.get("username", ""), "password": fields.get("password", ""), "otp": otp}
        item = json.loads(self._run(["get", "item", item_id], env=self._bw_env()) or "{}")
        login = item.get("login") or {}
        urls = [u.get("uri", "") for u in login.get("uris") or []]
        if not any(same_site(host, host_of(u)) for u in urls if u):
            raise PermissionError("that saved login belongs to a different website")
        otp = ""
        if login.get("totp"):
            try:
                otp = self._run(["get", "totp", item_id], env=self._bw_env()).strip()
            except RuntimeError:
                otp = ""
        return {"username": login.get("username", ""), "password": login.get("password", ""), "otp": otp}
