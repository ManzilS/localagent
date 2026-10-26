"""Calendars through their private "iCal" link — no sign-in, no passwords.

Google Calendar: Settings → (your calendar) → Integrate calendar →
  "Secret address in iCal format".
Outlook.com / Microsoft 365: Settings → Calendar → Shared calendars →
  Publish a calendar → ICS link.
iCloud: Calendar app → share calendar → Public Calendar → copy link.

Read-only by design. To add an event, LocalAgent prepares it (a link that
opens your calendar with everything filled in, or an .ics file) and you press
Save — so nothing lands on your calendar without you seeing it.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

HELP = {
    "google": "Google Calendar on the web → ⚙ Settings → pick your calendar on the left → Integrate calendar → "
              "copy “Secret address in iCal format”.",
    "outlook": "Outlook on the web → ⚙ Settings → Calendar → Shared calendars → Publish a calendar → "
               "choose “Can view all details” → Publish → copy the ICS link.",
    "icloud": "Calendar on iPhone/Mac → tap ⓘ next to the calendar → turn on Public Calendar → Share Link.",
}


def _as_dt(v, tz) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=tz)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=tz)
    raise TypeError(v)


class CalendarConnection:
    kind = "calendar"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec, self.secrets = cid, spec, secrets
        self.label = spec.get("name") or "Calendar"
        self._cache: tuple[float, bytes] | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        u = self.secrets.get(f"{self.id}:url")   # the secret link is a password: it lives in the keychain
        return re.sub(r"^webcals?://", "https://", u.strip())

    def _fetch(self) -> bytes:
        with self._lock:
            if self._cache and time.time() - self._cache[0] < 600:
                return self._cache[1]
            if not self.url:
                raise RuntimeError(f"{self.label} has no calendar link saved. Reconnect it on the Connections page.")
            r = httpx.get(self.url, timeout=30, follow_redirects=True, headers={"User-Agent": "LocalAgent"})
            if r.status_code >= 400:
                raise RuntimeError(f"{self.label}: the calendar link returned HTTP {r.status_code}. "
                                   "It may have been reset; copy it again.")
            if b"BEGIN:VCALENDAR" not in r.content[:2000]:
                raise RuntimeError(f"{self.label}: that link isn't an iCal calendar (no BEGIN:VCALENDAR).")
            self._cache = (time.time(), r.content)
            return r.content

    def events(self, start: datetime | None = None, days: float = 7, query: str = "") -> list[dict]:
        import icalendar  # type: ignore
        import recurring_ical_events  # type: ignore
        tz = datetime.now().astimezone().tzinfo
        start = start or datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        end = start + timedelta(days=days)
        cal = icalendar.Calendar.from_ical(self._fetch())
        out = []
        for ev in recurring_ical_events.of(cal).between(start, end):
            try:
                s = ev.get("DTSTART").dt
                e = ev.get("DTEND").dt if ev.get("DTEND") else s
            except Exception:
                continue
            all_day = not isinstance(s, datetime)
            sd, ed = _as_dt(s, tz).astimezone(tz), _as_dt(e, tz).astimezone(tz)
            item = {"title": str(ev.get("SUMMARY", "(no title)")), "start": sd, "end": ed, "all_day": all_day,
                    "location": str(ev.get("LOCATION", "") or ""), "description": str(ev.get("DESCRIPTION", "") or "")[:1500],
                    "calendar": self.label}
            if query and query.lower() not in (item["title"] + " " + item["location"] + " " + item["description"]).lower():
                continue
            out.append(item)
        out.sort(key=lambda x: x["start"])
        return out

    def test(self) -> str:
        evs = self.events(days=14)
        return f"Connected: {len(evs)} events in the next two weeks" + (f" (next: {evs[0]['title']})" if evs else "")


def describe(ev: dict) -> str:
    if ev["all_day"]:
        when = ev["start"].strftime("%a %b %d") + " (all day)"
    else:
        when = ev["start"].strftime("%a %b %d %H:%M") + "–" + ev["end"].strftime("%H:%M")
    extra = f" @ {ev['location']}" if ev["location"] else ""
    return f"{when}  {ev['title']}{extra}" + (f"  [{ev['calendar']}]" if ev.get("calendar") else "")


def add_event_link(provider: str, title: str, start: datetime, end: datetime, details: str = "", location: str = "") -> str:
    """A link that opens the calendar with the event filled in; the person presses Save."""
    if provider == "outlook":
        q = {"path": "/calendar/action/compose", "rru": "addevent", "subject": title, "body": details,
             "location": location, "startdt": start.isoformat(), "enddt": end.isoformat()}
        return "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode(q, quote_via=quote)
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q = {"action": "TEMPLATE", "text": title, "dates": f"{fmt(start)}/{fmt(end)}", "details": details, "location": location}
    return "https://calendar.google.com/calendar/render?" + urlencode(q, quote_via=quote)


def write_ics(path: Path, title: str, start: datetime, end: datetime, details: str = "", location: str = "") -> Path:
    import uuid
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LocalAgent//EN", "BEGIN:VEVENT",
                        f"UID:{uuid.uuid4()}@localagent", f"DTSTAMP:{fmt(datetime.now(timezone.utc))}",
                        f"DTSTART:{fmt(start)}", f"DTEND:{fmt(end)}", f"SUMMARY:{esc(title)}",
                        f"DESCRIPTION:{esc(details)}", f"LOCATION:{esc(location)}", "END:VEVENT", "END:VCALENDAR", ""])
    path.write_text(body, encoding="utf-8")
    return path
