"""Websites as connections: the fallback when a service has no API.

Connecting = signing in once in LocalAgent's browser window. After that the
agent opens the site with that sign-in and works in it like a person
(see tools/browser.py). Nothing about the sign-in is stored by LocalAgent
beyond what the browser itself keeps in its profile, same as any browser.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

LOGIN_HINTS = re.compile(r"(/signin|/sign-in|/login|/log-in|/auth|accounts\.google\.com|login\.live\.com|login\.microsoftonline|"
                         r"appleid\.apple\.com|/ap/signin|/checkpoint|/uas/login|idmsa\.apple)", re.I)


def looks_like_login(url: str, title: str = "") -> bool:
    return bool(LOGIN_HINTS.search(url or "")) or bool(re.search(r"\b(sign in|log in|login)\b", title or "", re.I))


class WebConnection:
    kind = "web"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec = cid, spec
        self.url = spec.get("url", "")
        self.label = spec.get("name") or urlparse(self.url).hostname or "Website"
        self.tips = spec.get("tips", "")

    def test(self) -> str:
        return f"Ready: {self.label} opens in LocalAgent's browser with your sign-in."
