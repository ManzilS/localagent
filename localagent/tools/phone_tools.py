from __future__ import annotations

from ..safety import resolve_path
from . import tool


@tool("send_phone_message", "Send the user a message (and optionally a file) on their phone through their paired "
      "Telegram bot. Use when they asked to be told something on their phone or to get a file there.",
      {"text": {"type": "string"}, "file": {"type": "string", "description": "optional path of a file to send"}},
      ["text"], group="phone")
def send_phone_message(rt, text: str, file: str = "") -> str:
    tg = getattr(rt, "telegram", None)
    if tg is None or not tg.owner_id:
        return "The phone isn't connected. The user can pair it on the Phone page in the LocalAgent app."
    if file:
        p = resolve_path(rt.cfg, file)
        tg.send_file(str(p), caption=text)
        return f"sent {p.name} to the user's phone"
    tg.send(text)
    return "sent to the user's phone"
