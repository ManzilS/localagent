"""Connections that need no account: news feeds (RSS/Atom) and weather.

Weather comes from Open-Meteo (open-meteo.com), free and keyless. Feeds are
any RSS/Atom address — news sites, blogs, YouTube channels, podcasts,
Reddit, Substack, GitHub releases.
"""
from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

UA = {"User-Agent": "LocalAgent/1 (+https://localhost)"}

STARTER_FEEDS = {
    "World news (BBC)": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Tech (The Verge)": "https://www.theverge.com/rss/index.xml",
    "Science (NASA)": "https://www.nasa.gov/feed/",
    "Hacker News": "https://hnrss.org/frontpage",
}

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "fog", 48: "freezing fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def _text(el, *names) -> str:
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or x.attrib.get("href")):
            return (x.text or x.attrib.get("href") or "").strip()
    return ""


def parse_feed(data: bytes, source: str = "") -> list[dict]:
    root = ET.fromstring(data)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        title = _text(root.find("channel"), "title") if root.find("channel") is not None else source
        for it in root.iter("item"):
            when = _text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")
            try:
                ts = parsedate_to_datetime(when).timestamp() if when else 0
            except (TypeError, ValueError):
                ts = _iso(when)
            items.append({"title": html.unescape(_text(it, "title")), "link": _text(it, "link"), "time": ts,
                          "summary": _clean(_text(it, "description")), "source": source or title})
    else:   # Atom
        title = _text(root, "{http://www.w3.org/2005/Atom}title") or source
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns)
            items.append({"title": html.unescape(_text(e, "{http://www.w3.org/2005/Atom}title")),
                          "link": link.attrib.get("href", "") if link is not None else "",
                          "time": _iso(_text(e, "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published")),
                          "summary": _clean(_text(e, "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content")),
                          "source": source or title})
    return items


def _iso(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()[:400]


class FeedsConnection:
    kind = "feeds"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec = cid, spec
        self.label = spec.get("name") or "News & feeds"
        self._cache: dict[str, tuple[float, list]] = {}

    @property
    def feeds(self) -> dict[str, str]:
        return dict(self.spec.get("feeds") or {})

    def fetch(self, name: str, url: str) -> list[dict]:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < 900:
            return hit[1]
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
        r.raise_for_status()
        items = parse_feed(r.content, name)
        self._cache[url] = (time.time(), items)
        return items

    def latest(self, limit: int = 15, query: str = "", feed: str = "") -> tuple[list[dict], list[str]]:
        out, errs = [], []
        for name, url in self.feeds.items():
            if feed and feed.lower() not in name.lower():
                continue
            try:
                out += self.fetch(name, url)
            except Exception as e:
                errs.append(f"{name}: {e}")
        if query:
            q = query.lower()
            out = [i for i in out if q in (i["title"] + " " + i["summary"]).lower()]
        out.sort(key=lambda i: -i["time"])
        return out[:limit], errs

    def test(self) -> str:
        items, errs = self.latest(limit=200)
        if not items and errs:
            raise RuntimeError("; ".join(errs))
        return f"Connected: {len(self.feeds)} feeds, {len(items)} recent stories" + (f" ({len(errs)} not working)" if errs else "")


class WeatherConnection:
    kind = "weather"

    def __init__(self, cid: str, spec: dict, secrets):
        self.id, self.spec = cid, spec
        self.label = spec.get("name") or f"Weather · {spec.get('place', '')}"

    @staticmethod
    def geocode(place: str) -> dict:
        r = httpx.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": place.split(",")[0].strip(), "count": 5},
                      headers=UA, timeout=15)
        r.raise_for_status()
        res = r.json().get("results") or []
        if not res:
            raise RuntimeError(f"couldn't find a place called {place!r}")
        rest = [w.strip().lower() for w in place.split(",")[1:]]
        best = next((x for x in res if all(any(w in str(x.get(k, "")).lower() for k in ("admin1", "country", "country_code")) for w in rest)), res[0])
        return {"lat": best["latitude"], "lon": best["longitude"],
                "place": ", ".join(str(best[k]) for k in ("name", "admin1", "country") if best.get(k))}

    def forecast(self, place: str = "", days: int = 3, units: str = "") -> str:
        if place:
            loc = self.geocode(place)
        else:
            loc = {"lat": self.spec["lat"], "lon": self.spec["lon"], "place": self.spec.get("place", "")}
        units = units or self.spec.get("units", "fahrenheit")
        r = httpx.get("https://api.open-meteo.com/v1/forecast", headers=UA, timeout=15, params={
            "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": max(1, min(int(days), 7)),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "temperature_unit": units, "wind_speed_unit": "mph" if units == "fahrenheit" else "kmh"})
        r.raise_for_status()
        d = r.json()
        u = "°F" if units == "fahrenheit" else "°C"
        c = d.get("current", {})
        lines = [f"{loc['place']} now: {c.get('temperature_2m')}{u} (feels {c.get('apparent_temperature')}{u}), "
                 f"{WMO.get(c.get('weather_code'), 'unknown')}, wind {c.get('wind_speed_10m')} {'mph' if u == '°F' else 'km/h'}"]
        dd = d.get("daily", {})
        for i, day in enumerate(dd.get("time", [])):
            lines.append(f"{datetime.fromisoformat(day):%a %b %d}: {WMO.get(dd['weather_code'][i], '?')}, "
                         f"{dd['temperature_2m_min'][i]}–{dd['temperature_2m_max'][i]}{u}, "
                         f"{dd['precipitation_probability_max'][i]}% chance of rain/snow")
        return "\n".join(lines)

    def test(self) -> str:
        return "Connected: " + self.forecast(days=1).splitlines()[0]
