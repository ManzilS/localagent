"""Sign in to hosted MCP connectors (Notion, Asana, Linear, Atlassian, …).

Implements the client side of the MCP authorization spec:
  1. discover the server's authorization server (protected-resource metadata,
     falling back to the server's own origin),
  2. register LocalAgent with it (dynamic client registration),
  3. open the browser with PKCE; the service redirects back to LocalAgent's
     own window server at http://127.0.0.1:<port>/oauth/callback,
  4. swap the code for tokens, keep them in the OS keychain, refresh them
     before they expire.
The person signs in on the service's own page; LocalAgent never sees the
password, and the access can be revoked from the service's settings.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as pysecrets
import threading
import time
from urllib.parse import urlencode, urlparse

import httpx

PENDING: dict[str, dict] = {}       # state -> flow in progress
_lock = threading.Lock()


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _get_json(client: httpx.Client, url: str) -> dict | None:
    try:
        r = client.get(url, headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18"})
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        pass
    return None


def discover(server_url: str, client: httpx.Client | None = None) -> dict:
    """Return {"issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint", "scopes"}."""
    c = client or httpx.Client(timeout=20, follow_redirects=True)
    prm = None
    # (a) the 401 response may point at the metadata
    try:
        r = c.post(server_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "localagent", "version": "1"}}},
            headers={"Accept": "application/json, text/event-stream"})
        if r.status_code == 401:
            m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", ""))
            if m:
                prm = _get_json(c, m.group(1))
        elif r.status_code < 400 and ("json" in r.headers.get("content-type", "") or "event-stream" in r.headers.get("content-type", "")):
            return {"none": True}   # an MCP answer without asking for sign-in
    except httpx.HTTPError:
        pass
    # (b) well-known locations
    path = urlparse(server_url).path.rstrip("/")
    if prm is None:
        for u in (f"{_origin(server_url)}/.well-known/oauth-protected-resource{path}",
                  f"{_origin(server_url)}/.well-known/oauth-protected-resource"):
            prm = _get_json(c, u)
            if prm:
                break
    issuer = ((prm or {}).get("authorization_servers") or [_origin(server_url)])[0].rstrip("/")
    ip = urlparse(issuer)
    meta = None
    for u in (f"{ip.scheme}://{ip.netloc}/.well-known/oauth-authorization-server{ip.path}",
              f"{ip.scheme}://{ip.netloc}/.well-known/openid-configuration{ip.path}",
              f"{issuer}/.well-known/openid-configuration",
              f"{ip.scheme}://{ip.netloc}/.well-known/oauth-authorization-server"):
        meta = _get_json(c, u)
        if meta and meta.get("authorization_endpoint"):
            break
    if not meta:   # 2025-03 spec default endpoints
        meta = {"authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
                "registration_endpoint": issuer + "/register"}
    return {"issuer": issuer, "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"], "registration_endpoint": meta.get("registration_endpoint"),
            "scopes": (prm or {}).get("scopes_supported") or []}


def start(name: str, server_url: str, redirect_uri: str, store, client: httpx.Client | None = None) -> str:
    """Begin sign-in. Returns the URL to open in the browser."""
    c = client or httpx.Client(timeout=20, follow_redirects=True)
    d = discover(server_url, c)
    if d.get("none"):
        raise RuntimeError("this connector doesn't need a sign-in")
    if not d.get("registration_endpoint"):
        raise RuntimeError("this service doesn't let new apps register on their own; it needs a token instead")
    reg = c.post(d["registration_endpoint"], json={
        "client_name": "LocalAgent", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none", "application_type": "native"})
    if reg.status_code >= 400:
        raise RuntimeError(f"the service refused to register LocalAgent (HTTP {reg.status_code}: {reg.text[:200]})")
    client_info = reg.json()
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = pysecrets.token_urlsafe(24)
    q = {"response_type": "code", "client_id": client_info["client_id"], "redirect_uri": redirect_uri, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256", "resource": server_url}
    if d["scopes"]:
        q["scope"] = " ".join(d["scopes"])
    with _lock:
        for k in [k for k, v in PENDING.items() if time.time() - v["time"] > 900]:
            PENDING.pop(k)
        PENDING[state] = {"name": name, "server_url": server_url, "redirect_uri": redirect_uri, "verifier": verifier,
                          "client": client_info, "token_endpoint": d["token_endpoint"], "time": time.time(), "store": store}
    sep = "&" if "?" in d["authorization_endpoint"] else "?"
    return d["authorization_endpoint"] + sep + urlencode(q)


def finish(state: str, code: str, client: httpx.Client | None = None) -> str:
    """Called by /oauth/callback. Returns the connector name."""
    with _lock:
        flow = PENDING.pop(state, None)
    if flow is None:
        raise RuntimeError("this sign-in link expired or was already used; start again from LocalAgent")
    c = client or httpx.Client(timeout=20)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
            "client_id": flow["client"]["client_id"], "code_verifier": flow["verifier"], "resource": flow["server_url"]}
    if flow["client"].get("client_secret"):
        data["client_secret"] = flow["client"]["client_secret"]
    r = c.post(flow["token_endpoint"], data=data, headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"sign-in failed at the last step (HTTP {r.status_code}: {r.text[:200]})")
    tok = r.json()
    save_tokens(flow["store"], flow["name"], {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + float(tok.get("expires_in", 3600)) if tok.get("expires_in") else 0,
        "token_endpoint": flow["token_endpoint"], "client_id": flow["client"]["client_id"],
        "client_secret": flow["client"].get("client_secret", ""), "resource": flow["server_url"]})
    return flow["name"]


def save_tokens(store, name: str, data: dict) -> None:
    store.set(f"mcp:{name}:oauth", json.dumps(data))


def load_tokens(store, name: str) -> dict:
    try:
        return json.loads(store.get(f"mcp:{name}:oauth") or "{}")
    except json.JSONDecodeError:
        return {}


class TokenSource:
    """Gives HttpConn a fresh access token, refreshing when needed."""
    def __init__(self, store, name: str):
        self.store, self.name = store, name
        self._lock = threading.Lock()

    def token(self, force_refresh: bool = False) -> str:
        with self._lock:
            t = load_tokens(self.store, self.name)
            if not t:
                raise RuntimeError(f"{self.name} isn't signed in. Press Sign in on the Connections page.")
            soon = t.get("expires_at") and t["expires_at"] - time.time() < 120
            if (force_refresh or soon) and t.get("refresh_token"):
                data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"],
                        "resource": t.get("resource", "")}
                if t.get("client_secret"):
                    data["client_secret"] = t["client_secret"]
                r = httpx.post(t["token_endpoint"], data=data, headers={"Accept": "application/json"}, timeout=20)
                if r.status_code >= 400:
                    raise RuntimeError(f"{self.name}: the sign-in expired. Sign in again on the Connections page.")
                new = r.json()
                t.update({"access_token": new["access_token"],
                          "refresh_token": new.get("refresh_token") or t["refresh_token"],
                          "expires_at": time.time() + float(new.get("expires_in", 3600)) if new.get("expires_in") else 0})
                save_tokens(self.store, self.name, t)
            return t["access_token"]
