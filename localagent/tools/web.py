"""Web research: search (no API key — DuckDuckGo via `ddgs`, or a SearXNG
instance if you run one), page fetching with readable-text extraction, and
downloads."""
from __future__ import annotations

import io
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..safety import check_writable, resolve_path
from . import tool, truncate

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36 localagent/0.1")


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": UA})


def _search_ddgs(query: str, n: int) -> list[dict]:
    from ddgs import DDGS  # type: ignore
    with DDGS() as d:
        return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body", "")}
                for r in d.text(query, max_results=n)]


def _search_html(query: str, n: int) -> list[dict]:
    from bs4 import BeautifulSoup
    with _client() as c:
        r = c.post("https://html.duckduckgo.com/html/", data={"q": query})
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select(".result")[:n]:
        a = res.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snip = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href, "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return out


def _search_searxng(base: str, query: str, n: int) -> list[dict]:
    with _client() as c:
        r = c.get(f"{base.rstrip('/')}/search", params={"q": query, "format": "json"})
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


@tool("web_search", "Search the web. Returns titles, URLs and snippets. Follow up with web_fetch on the best URLs "
      "— snippets are often too short to answer from.",
      {"query": {"type": "string"}, "max_results": {"type": "integer", "description": "default 8"}}, ["query"], group="web")
def web_search(rt, query: str, max_results: int = 8) -> str:
    n = max(1, min(int(max_results or 8), 20))
    errors, results = [], []
    searx = rt.cfg.data.get("searxng_url") or os.environ.get("SEARXNG_URL")
    backends = ([("searxng", lambda: _search_searxng(searx, query, n))] if searx else []) + [
        ("ddgs", lambda: _search_ddgs(query, n)), ("ddg-html", lambda: _search_html(query, n))]
    for name, fn in backends:
        try:
            results = fn()
            if results:
                break
        except Exception as e:
            errors.append(f"{name}: {e}")
    if not results:
        return "no results" + (f" (errors: {'; '.join(errors)})" if errors else "")
    return "\n\n".join(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1))


def html_to_text(html: str, url: str = "") -> str:
    try:
        import trafilatura  # type: ignore
        text = trafilatura.extract(html, url=url, include_links=False, include_tables=True)
        if text and len(text) > 200:
            return text
    except Exception:
        pass
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return (f"# {title}\n\n" if title else "") + text


@tool("web_fetch", "Fetch a URL and return its readable text (HTML, PDF, JSON, plain text).",
      {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"], group="web")
def web_fetch(rt, url: str, max_chars: int | None = None) -> str:
    limit = int(max_chars or rt.cfg.agent["max_tool_output_chars"])
    if not urlparse(url).scheme:
        url = "https://" + url
    with _client() as c:
        r = c.get(url)
    ctype = r.headers.get("content-type", "")
    head = f"URL: {r.url}\nStatus: {r.status_code}\n\n"
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        text = "\n\n".join(f"--- page {i + 1} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages[:40]))
    elif "html" in ctype or "<html" in r.text[:500].lower():
        text = html_to_text(r.text, str(r.url))
    else:
        text = r.text
    return head + truncate(text, limit)


@tool("download_file", "Download a URL to a local file.",
      {"url": {"type": "string"}, "path": {"type": "string", "description": "destination file"}}, ["url", "path"], group="web")
def download_file(rt, url: str, path: str) -> str:
    p = resolve_path(rt.cfg, path)
    check_writable(rt.cfg, p)
    if p.exists():
        rt.trash.backup(p, reason="overwritten by download_file")
    p.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with _client() as c, c.stream("GET", url) as r:
        r.raise_for_status()
        with open(p, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
                size += len(chunk)
    return f"downloaded {size:,} bytes to {p}"
