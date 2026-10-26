"""LocalAgent's own browser — the way in when a service has no API.

It's a normal browser window (your installed Edge or Chrome, through
Playwright) with its own profile in ~/.localagent/browser, kept between
runs. You sign in to a site once, yourself; after that the agent can open it
and read or work in it the way you would. Your passwords are typed by you
into the real site; LocalAgent never sees or stores them.

It behaves like a person with a browser, not like a scraper in disguise: no
faked fingerprints, no CAPTCHA solving. If a site asks "are you a robot" or
for a second sign-in factor, the window comes forward and you deal with it.

Rules, same as the desktop: clicks that delete, buy/pay, or send/post/publish
wait for your OK. Page text is outside content (prompt-injection guard).

Playwright's sync API must stay on one thread, so every action goes through
a single worker thread — safe to call from parallel tasks.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import tool, truncate

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
_state: dict = {}

SEND_PATTERN = r"\b(send|post|publish|tweet|reply all|share|submit|place order|confirm purchase)\b"

MARK_JS = r"""
(max) => {
  document.querySelectorAll('[data-la-ref]').forEach(e => e.removeAttribute('data-la-ref'));
  const sel = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=option],[role=switch],[contenteditable=true],[onclick],[tabindex="0"]';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight * 3) continue;
    const st = getComputedStyle(el); if (st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1; el.setAttribute('data-la-ref', 'e' + n);
    const secret = el.type === 'password' || /password|one-time-code/.test(el.getAttribute('autocomplete') || '');
    const label = (el.getAttribute('aria-label') || el.innerText || (secret ? '' : el.value) || el.placeholder || el.title ||
                   el.getAttribute('alt') || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const kind = el.tagName === 'A' ? 'link' : el.tagName === 'INPUT' ? (el.type || 'input') : el.getAttribute('role') || el.tagName.toLowerCase();
    out.push('e' + n + ' ' + kind + (label ? ' "' + label + '"' : '') + (el.tagName === 'A' && el.href ? ' -> ' + el.href.slice(0, 90) : ''));
    if (n >= max) break;
  }
  return out.join('\n');
}
"""


def profile_dir(cfg) -> Path:
    return Path(cfg.home) / "browser"


def _channels() -> list[str | None]:
    if os.name == "nt":
        return ["msedge", "chrome", None]     # Edge is on every Windows PC
    return ["chrome", "msedge", None]


def _launch(cfg, visible: bool):
    from playwright.sync_api import sync_playwright  # type: ignore
    pw = sync_playwright().start()
    bcfg = cfg.data.get("browser") or {}
    headless = bool(bcfg.get("headless", False)) and not visible
    args = [] if visible or headless else ["--start-minimized"]
    last = None
    for ch in ([bcfg["channel"]] if bcfg.get("channel") else _channels()):
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir(cfg)), channel=None if ch in (None, "bundled", "chromium") else ch, headless=headless, args=args,
                viewport=None if not headless else {"width": 1366, "height": 900}, accept_downloads=True,
                ignore_default_args=["--disable-extensions"])   # so your password manager's extension works here
            _state.update(pw=pw, ctx=ctx, visible=visible, channel=ch or "chromium", profile=str(profile_dir(cfg)))
            pages = ctx.pages
            _state["page"] = pages[0] if pages else ctx.new_page()
            return _state["page"]
        except Exception as e:   # that browser isn't installed: try the next
            last = e
    pw.stop()
    raise RuntimeError(f"couldn't open a browser ({last}). Install Microsoft Edge or Google Chrome, "
                       "or run: playwright install chromium")


def _page(cfg, visible: bool = False):
    if "ctx" in _state and ((visible and not _state.get("visible")) or _state.get("profile") != str(profile_dir(cfg))):
        _close()                     # signing in needs a normal, visible window; a different profile needs a fresh browser
    if "ctx" not in _state:
        return _launch(cfg, visible)
    page = _state["page"]
    if page.is_closed():
        _state["page"] = page = (_state["ctx"].pages or [_state["ctx"].new_page()])[-1]
    return page


def _close() -> None:
    for k in ("ctx", "pw"):
        try:
            _state[k].close() if k == "ctx" else _state[k].stop()
        except Exception:
            pass
    _state.clear()


def _locate(page, target: str):
    t = target.strip()
    if re.fullmatch(r"e\d+", t):
        return page.locator(f'[data-la-ref="{t}"]').first
    if t.startswith(("css=", "xpath=", "#", ".", "[", "//")) or any(c in t for c in "[]>#"):
        return page.locator(t.removeprefix("css=")).first
    # what a person would call it: a button's name, a field's label or placeholder, a link, then any text
    for cand in (page.get_by_role("button", name=t, exact=True), page.get_by_label(t, exact=False),
                 page.get_by_placeholder(t, exact=False), page.get_by_role("link", name=t, exact=True),
                 page.get_by_role("button", name=t), page.get_by_role("textbox", name=t)):
        try:
            if cand.count():
                return cand.first
        except Exception:
            continue
    return page.get_by_text(t, exact=False).first


def _label(page, target: str) -> str:
    try:
        loc = _locate(page, target)
        txt = loc.evaluate("e => (e.getAttribute('aria-label') || e.innerText || (e.type === 'password' ? '' : e.value) || e.title || '').trim()", timeout=3000)
        return f"{target} ({txt[:80]})" if txt and txt != target else target
    except Exception:
        return target


def _confirm(rt, what: str, label: str) -> None:
    pats = list((rt.cfg.data.get("desktop") or {}).get("confirm_patterns") or []) + [SEND_PATTERN]
    if any(re.search(p, label or "", re.I) for p in pats):
        page = _state.get("page")
        where = f" on {page.url.split('/')[2]}" if page is not None and "://" in page.url else ""
        if not rt.request_approval(f"{what}{where}: “{label}”"):
            raise PermissionError(f"the user did not approve {what.lower()} '{label}'. Ask them how to proceed.")


def _settle(page, ms: int = 900) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def _do(rt, action: str, url: str, target: str, text: str) -> str:
    page = _page(rt.cfg, visible=action == "show")
    if action in ("goto", "show"):
        if url:
            page.goto(url if "://" in url or url.startswith(("about:", "data:", "file:")) else "https://" + url,
                      wait_until="domcontentloaded", timeout=45000)
            _settle(page)
        if action == "show":
            page.bring_to_front()
        from ..connections.web_conn import looks_like_login
        note = ""
        if action == "goto" and looks_like_login(page.url, page.title()):
            mgr = getattr(rt, "connections", None)
            if mgr is not None and mgr.of_kind("passwords"):
                note = ("\nThis page wants a sign-in. Call browser with action 'login' to fill the user's saved login from their "
                        "password manager (they approve it; you never see the password). If there's none, use 'show'.")
            else:
                note = ("\nThis page wants the user to sign in. Don't type a password: call browser with action 'show' (same URL) "
                        "so the window comes up, ask the user to sign in, wait for them, then continue.")
        return f"opened {page.url} — title: {page.title()}" + (" (window shown to the user)" if action == "show" else "") + note
    if action == "snapshot":
        items = page.evaluate(MARK_JS, 250)
        return truncate(f"URL: {page.url}\nTitle: {page.title()}\nClickable things (use the e-number as target):\n{items}",
                        rt.cfg.agent["max_tool_output_chars"])
    if action == "click":
        label = _label(page, target)
        _confirm(rt, "Click", label)
        _locate(page, target).click(timeout=10000)
        _settle(page)
        return f"clicked {label}; now at {page.url}"
    if action in ("fill", "type"):
        loc = _locate(page, target)
        loc.fill(text, timeout=10000) if action == "fill" else loc.press_sequentially(text, delay=30, timeout=30000)
        return f"typed into {target}"
    if action == "select":
        _locate(page, target).select_option(label=text, timeout=10000)
        return f"selected {text} in {target}"
    if action == "press":
        key = text or "Enter"
        if key.lower() in ("enter", "control+enter", "meta+enter"):
            focused = page.evaluate("() => { const a = document.activeElement; return a ? (a.getAttribute('aria-label') || a.innerText || (a.type === 'password' ? '' : a.value) || a.name || '').slice(0, 80) : '' }")
            _confirm(rt, f"Press {key}", focused)
        page.keyboard.press(key)
        _settle(page)
        return f"pressed {key}; now at {page.url}"
    if action == "scroll":
        page.mouse.wheel(0, -1200 if text == "up" else 1200)
        page.wait_for_timeout(600)
        return f"scrolled {text or 'down'}"
    if action == "back":
        page.go_back()
        _settle(page)
        return f"back at {page.url}"
    if action == "wait":
        page.wait_for_timeout(int(float(text or 2) * 1000))
        return "waited"
    if action == "read":
        body = page.inner_text("body")
        return truncate(f"URL: {page.url}\nTitle: {page.title()}\n\n{body}", rt.cfg.agent["max_tool_output_chars"])
    if action == "links":
        links = page.eval_on_selector_all("a[href]", "els => els.slice(0,150).map(e => (e.innerText||'').trim().slice(0,80) + ' -> ' + e.href)")
        return "\n".join(links)
    if action == "screenshot":
        out = rt.cfg.workspace / "screenshots" / f"page_{datetime.now():%Y%m%d_%H%M%S}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out), full_page=bool(text == "full"))
        return f"saved screenshot to {out} (use look_at on it to inspect)"
    if action == "login":
        return _login(rt, page, choice=target, code_only=(text or "").lower() in ("code", "otp", "2fa"))
    if action == "close":
        _close()
        return "browser closed"
    return f"unknown action {action}"


USER_SEL = ('input[autocomplete~="username"]:visible, input[type=email]:visible, input[name*="user" i]:visible, '
            'input[name*="email" i]:visible, input[id*="user" i]:visible, input[id*="email" i]:visible, '
            'input[name*="login" i]:visible, input[type=text]:visible, input:not([type]):visible')
PASS_SEL = 'input[type=password]:visible'
CODE_SEL = ('input[autocomplete="one-time-code"]:visible, input[name*="otp" i]:visible, input[name*="totp" i]:visible, '
            'input[name*="code" i]:visible, input[id*="code" i]:visible, input[inputmode=numeric]:visible')


def _login(rt, page, choice: str = "", code_only: bool = False) -> str:
    """Fill a saved login from the user's password manager into this page.
    The secret goes password manager -> page; it is never returned."""
    from ..connections.passwords_conn import host_of
    mgr = getattr(rt, "connections", None)
    pms = mgr.of_kind("passwords") if mgr is not None else []
    host = host_of(page.url)
    if not pms:
        return ("No password manager is connected. Use action 'show' so the user can sign in themselves "
                "(or they can connect 1Password/Bitwarden on the Connections page).")
    found, notes = [], []
    for pm in pms:
        try:
            found += [(pm, it) for it in pm.logins_for(page.url)]
        except PermissionError as e:
            notes.append(str(e))
        except Exception as e:
            notes.append(f"{pm.label}: {e}")
    if not found:
        return (f"No saved login for {host} in {', '.join(p.label for p in pms)}." + (" " + " ".join(notes) if notes else "") +
                " Use action 'show' and ask the user to sign in.")
    if choice:
        pick = [f for f in found if choice.lower() in f[1]["title"].lower() or choice == f[1]["id"]]
        found = pick or found
    if len(found) > 1 and not choice:
        return ("Several saved logins match " + host + ": " +
                "; ".join(f"“{it['title']}” ({it['user']}, {pm.label})" for pm, it in found) +
                ". Call login again with target = the title to use (ask the user if unsure).")
    pm, it = found[0]
    what = (f"fill a one-time code for {host} from {pm.label}" if code_only else
            f"sign in to {host} with your saved {pm.label} login “{it['title']}” ({it['user']})")
    if not rt.request_approval(what):
        raise PermissionError(f"the user did not approve: {what}")
    if not host_of(page.url) == host:           # page changed while waiting for approval
        raise PermissionError("the page changed while waiting for approval; not filling anything")
    creds = pm.credentials(it["id"], page.url)
    try:
        if code_only:
            if not creds.get("otp"):
                return f"“{it['title']}” has no one-time code saved in {pm.label}. Use 'show' and ask the user for the code."
            box = page.locator(CODE_SEL).first
            if not box.count():
                return "Couldn't find a code box on this page. Use snapshot to look, or 'show' and ask the user."
            box.fill(creds["otp"], timeout=8000)
            return f"Filled the one-time code from {pm.label} (not shown to you). Now press the page's Verify/Continue button."
        pw_box = page.locator(PASS_SEL).first
        filled = []
        if creds.get("username"):
            user_box = page.locator(USER_SEL).first
            if user_box.count():
                try:
                    current = user_box.input_value(timeout=3000)
                except Exception:
                    current = ""
                if current.strip().lower() != creds["username"].strip().lower():
                    user_box.fill(creds["username"], timeout=8000)
                filled.append("username")
        if pw_box.count() and creds.get("password"):
            pw_box.fill(creds["password"], timeout=8000)
            filled.append("password")
        if not filled:
            return "Couldn't find sign-in boxes on this page. Use snapshot to look, or 'show' and ask the user."
        nxt = ("Now click the page's Sign in / Log in button." if "password" in filled else
               "Only the username box was on this page. Click Next/Continue, then call login again for the password.")
        return f"Filled the {' and '.join(filled)} from {pm.label} for {host} (not shown to you). {nxt}"
    finally:
        creds.clear()


def run(rt, action: str, url: str = "", target: str = "", text: str = "", timeout: float = 120) -> str:
    try:
        import playwright  # noqa: F401  type: ignore
    except ImportError:
        return "ERROR: the browser part isn't installed. Run: pip install playwright. Meanwhile use web_fetch."
    return _executor.submit(_do, rt, action, url, target, text).result(timeout=timeout)


@tool("browser", "LocalAgent's own browser, where the user is signed in to their sites. Use it for any website or web app "
      "— especially services with no connector (open the site and read or work in it like a person would). "
      "Actions: goto(url), snapshot (numbered list of clickable things — then click/fill with target='e12'), read (page text), "
      "click(target), fill(target,text), type(target,text), select(target,text=option), press(text=key), scroll(text=up/down), "
      "back, wait(text=seconds), links, screenshot(text='full'), login (fill the user's saved login for this site from their "
      "password manager after they approve — you never see the password; text='code' fills a one-time code), "
      "show(url) to bring the window to the user (sign-ins with no saved login, CAPTCHAs, passkeys), close. "
      "Prefer web_fetch for simple public pages.",
      {"action": {"type": "string", "enum": ["goto", "snapshot", "read", "click", "fill", "type", "select", "press", "scroll",
                                             "back", "wait", "links", "screenshot", "login", "show", "close"]},
       "url": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"}}, ["action"], group="web")
def browser(rt, action: str, url: str = "", target: str = "", text: str = "") -> str:
    return run(rt, action, url, target, text)
