"""Desktop control: the agent operates apps on this computer like a person —
look at the screen, click, type, press shortcuts.

How it sees:
  • Windows — reads the real UI structure through UI Automation (buttons,
    text boxes, menu items with names and positions). Fast and precise.
  • macOS   — reads the front window through the Accessibility API
    (needs Accessibility permission for LocalAgent / your terminal).
  • Linux   — screenshots + the vision model.
Every look also gets a screenshot with numbered boxes drawn on the elements,
which the vision model describes. Anything not in the element list can be
found visually with find_on_screen (two-stage grid zoom).

Safety:
  • Clicks/keys that match desktop.confirm_patterns (delete, pay, ...) wait
    for the user's OK. No answer = no click.
  • Slam the mouse into a screen corner to stop instantly (failsafe), or
    press Stop in the app.
"""
from __future__ import annotations

import difflib
import glob
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser
from pathlib import Path

from . import tool, truncate

IS_WIN = os.name == "nt"
IS_MAC = platform.system() == "Darwin"

if IS_WIN:  # make screenshot pixels, UIA rectangles and mouse coordinates agree
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

SHOT_DIR = Path(tempfile.gettempdir()) / "localagent_screens"
INSTALL_HINT = "Desktop control needs extra packages: pip install 'localagent[desktop]'"


# ------------------------------------------------------------------ helpers

def _gui():
    try:
        import pyautogui  # type: ignore
    except Exception as e:  # ImportError, or no display (KeyError: DISPLAY)
        raise RuntimeError(f"{INSTALL_HINT} (and a desktop session). [{type(e).__name__}: {e}]")
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.12
    return pyautogui


def _state(rt) -> dict:
    if not hasattr(rt, "desktop_state"):
        rt.desktop_state = {"elements": {}, "found": {}, "n": 0}
    return rt.desktop_state


def _screenshot(rt):
    """Return (PIL image, path, scale) where scale = image px per mouse unit."""
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        raise RuntimeError(INSTALL_HINT)
    gui = _gui()
    with mss.mss() as s:
        raw = s.grab(s.monitors[1])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    lw, _ = gui.size()
    scale = img.width / float(lw or img.width)
    st = _state(rt)
    st["n"] = (st["n"] + 1) % 20  # rotate 20 files instead of deleting old ones
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"screen_{st['n']}.png"
    img.save(path)
    return img, str(path), scale


def _guard(rt, label: str, action: str) -> None:
    pats = (rt.cfg.data.get("desktop") or {}).get("confirm_patterns") or []
    if any(re.search(p, label or "", re.I) for p in pats):
        if not rt.request_approval(f"{action}: “{label}”"):
            raise PermissionError(f"the user did not approve {action.lower()} '{label}'. Ask them how to proceed.")


def _failsafe(fn):
    def wrapper(rt, *a, **k):
        rt.check_cancel()
        try:
            return fn(rt, *a, **k)
        except Exception as e:
            if type(e).__name__ == "FailSafeException":
                rt.cancel.set()
                return "Stopped: the mouse was moved into a screen corner (emergency stop)."
            raise
    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------- accessibility backends

WIN_TYPES = {"ButtonControl", "EditControl", "HyperlinkControl", "MenuItemControl", "ListItemControl",
             "TabItemControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl", "TreeItemControl",
             "SplitButtonControl", "DataItemControl", "MenuBarControl", "DocumentControl", "TextControl",
             "SliderControl", "SpinnerControl", "ImageControl"}


def _elements_windows(limit: int = 160, budget_s: float = 3.0) -> tuple[str, list[dict]]:
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return "", []
    win = auto.GetForegroundControl()
    try:
        win = win.GetTopLevelControl() or win
    except Exception:
        pass
    title = win.Name or ""
    out, t0 = [], time.time()
    for c, _depth in auto.WalkControl(win, includeTop=False, maxDepth=30):
        if time.time() - t0 > budget_s or len(out) >= limit:
            break
        try:
            kind = c.ControlTypeName
            if kind not in WIN_TYPES:
                continue
            name = (c.Name or "").strip()
            if kind == "TextControl" and (not name or len(name) > 120):
                continue
            if not name and kind not in ("EditControl", "ComboBoxControl", "DocumentControl"):
                continue
            r = c.BoundingRectangle
            if r.width() <= 2 or r.height() <= 2 or c.IsOffscreen:
                continue
            value = ""
            if kind == "EditControl":
                try:
                    value = c.GetValuePattern().Value[:60]
                except Exception:
                    pass
            out.append({"type": kind.replace("Control", ""), "name": name[:80], "value": value,
                        "x": r.left, "y": r.top, "w": r.width(), "h": r.height()})
        except Exception:
            continue
    return title, out


_MAC_SCRIPT = r'''
tell application "System Events"
  set p to first application process whose frontmost is true
  set out to (name of p) & linefeed
  tell p
    if (count of windows) is 0 then return out
    set w to front window
    set out to out & (name of w) & linefeed
    set n to 0
    repeat with e in (entire contents of w)
      if n > 160 then exit repeat
      try
        set r to role of e
        if r is in {"AXButton","AXTextField","AXTextArea","AXLink","AXMenuItem","AXMenuButton","AXCheckBox","AXRadioButton","AXPopUpButton","AXComboBox","AXTab","AXStaticText","AXCell","AXSearchField"} then
          set t to ""
          try
            set t to (title of e) as text
          end try
          if t is "" or t is "missing value" then
            try
              set t to (description of e) as text
            end try
          end if
          if t is "missing value" then set t to ""
          if r is not "AXStaticText" or t is not "" then
            set {x, y} to position of e
            set {ww, hh} to size of e
            set out to out & r & tab & t & tab & x & tab & y & tab & ww & tab & hh & linefeed
            set n to n + 1
          end if
        end if
      end try
    end repeat
  end tell
end tell
return out
'''


def _elements_mac() -> tuple[str, list[dict]]:
    try:
        r = subprocess.run(["osascript", "-e", _MAC_SCRIPT], capture_output=True, text=True, timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        return "", []
    lines = r.stdout.splitlines()
    if len(lines) < 1:
        return "", []
    title = " — ".join(l for l in lines[:2] if l)
    out = []
    for line in lines[2:]:
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        role, name, x, y, w, h = parts
        try:
            out.append({"type": role.replace("AX", ""), "name": name[:80], "value": "",
                        "x": int(float(x)), "y": int(float(y)), "w": int(float(w)), "h": int(float(h))})
        except ValueError:
            continue
    return title, out


def _elements() -> tuple[str, list[dict]]:
    if IS_WIN:
        return _elements_windows()
    if IS_MAC:
        return _elements_mac()
    title = ""
    if shutil.which("xdotool"):
        try:
            title = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True,
                                   text=True, timeout=3).stdout.strip()
        except Exception:
            pass
    return title, []


def _annotate(img, elements: list[dict], scale: float):
    from PIL import ImageDraw  # type: ignore
    im = img.copy()
    d = ImageDraw.Draw(im)
    for i, e in enumerate(elements[:80], 1):
        x0, y0 = e["x"] * scale, e["y"] * scale
        x1, y1 = (e["x"] + e["w"]) * scale, (e["y"] + e["h"]) * scale
        d.rectangle([x0, y0, x1, y1], outline=(255, 40, 120), width=2)
        tag = f"e{i}"
        d.rectangle([x0, y0, x0 + 9 * len(tag) + 4, y0 + 14], fill=(255, 40, 120))
        d.text((x0 + 2, y0 + 1), tag, fill=(255, 255, 255))
    return im


def _grid(img, cols: int, rows: int):
    from PIL import ImageDraw  # type: ignore
    im = img.copy()
    d = ImageDraw.Draw(im)
    cw, ch = im.width / cols, im.height / rows
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * cw, r * ch
            d.rectangle([x0, y0, x0 + cw, y0 + ch], outline=(0, 160, 255), width=2)
            n = r * cols + c + 1
            d.rectangle([x0 + 2, y0 + 2, x0 + 30, y0 + 20], fill=(0, 160, 255))
            d.text((x0 + 5, y0 + 4), str(n), fill=(255, 255, 255))
    return im


def grid_cell_center(n: int, cols: int, rows: int, width: float, height: float) -> tuple[float, float]:
    n = max(1, min(n, cols * rows)) - 1
    r, c = divmod(n, cols)
    return (c + 0.5) * width / cols, (r + 0.5) * height / rows


def _ask_cell(rt, image, what: str, cells: int) -> int:
    from .documents import ask_vision
    p = SHOT_DIR / "locate.png"
    image.save(p)
    ans = ask_vision(rt, [str(p)], f"The image has numbered blue grid cells 1-{cells}. Which cell contains the CENTER of: "
                                   f"{what}? Reply with only the cell number, or 0 if it is not visible.")
    m = re.search(r"\d+", ans or "")
    return int(m.group()) if m else 0


# -------------------------------------------------------------------- tools

@tool("open_app", "Open or switch to an app on this computer by name (e.g. 'Spotify', 'Excel', 'Settings', "
      "'File Explorer', 'Discord'), or open a website URL in the default browser.",
      {"name": {"type": "string"}}, ["name"], group="desktop")
@_failsafe
def open_app(rt, name: str) -> str:
    name = name.strip()
    if re.match(r"^(https?://|www\.)", name):
        webbrowser.open(name if "://" in name else "https://" + name)
        time.sleep(2)
        return f"opened {name} in the default browser. Use see_screen to look."
    if IS_WIN:
        shortcuts = []
        for root in (os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
                     os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs")):
            shortcuts += glob.glob(os.path.join(root, "**", "*.lnk"), recursive=True)
        stems = {Path(s).stem.lower(): s for s in shortcuts}
        hit = stems.get(name.lower()) or next((v for k, v in stems.items() if name.lower() in k), None)
        if not hit:
            close = difflib.get_close_matches(name.lower(), list(stems), n=1, cutoff=0.6)
            hit = stems[close[0]] if close else None
        if hit:
            os.startfile(hit)  # type: ignore[attr-defined]
        else:  # built-ins, store apps, ms-settings:, exe names on PATH
            subprocess.Popen(["cmd", "/c", "start", "", name], shell=False)
    elif IS_MAC:
        r = subprocess.run(["open", "-a", name], capture_output=True, text=True)
        if r.returncode != 0:
            apps = [Path(p).stem for p in glob.glob("/Applications/*.app") + glob.glob("/System/Applications/*.app")]
            close = difflib.get_close_matches(name, apps, n=1, cutoff=0.5)
            if not close:
                return f"couldn't find an app called {name}"
            subprocess.run(["open", "-a", close[0]])
            name = close[0]
    else:
        desktops = glob.glob("/usr/share/applications/*.desktop") + glob.glob(os.path.expanduser("~/.local/share/applications/*.desktop"))
        names = {}
        for f in desktops:
            try:
                m = re.search(r"^Name=(.+)$", Path(f).read_text(errors="ignore"), re.M)
                if m:
                    names[m.group(1).strip().lower()] = Path(f).stem
            except OSError:
                pass
        close = difflib.get_close_matches(name.lower(), list(names), n=1, cutoff=0.5)
        if close and shutil.which("gtk-launch"):
            subprocess.Popen(["gtk-launch", names[close[0]]])
        else:
            subprocess.Popen([name], start_new_session=True)
    time.sleep(2.5)
    return f"launched {name}. Use see_screen to check it opened."


@tool("see_screen", "Look at the screen: returns the active window, a numbered list of clickable elements "
      "(e1, e2, ...) and a description from the vision model. Ask a specific question for a better answer.",
      {"question": {"type": "string", "description": "what you need to know, e.g. 'is the message sent?'"},
       "describe": {"type": "boolean", "description": "run the vision model (default true)"}}, group="desktop")
@_failsafe
def see_screen(rt, question: str = "What app is open and what is on screen?", describe: bool = True) -> str:
    img, path, scale = _screenshot(rt)
    title, elements = _elements()
    st = _state(rt)
    st["elements"] = {f"e{i}": e for i, e in enumerate(elements, 1)}
    st["scale"] = scale
    annotated_path = path
    if elements:
        _annotate(img, elements, scale).save(path.replace(".png", "_marked.png"))
        annotated_path = path.replace(".png", "_marked.png")
    rt.emit("screen", {"path": annotated_path})
    gui = _gui()
    lw, lh = gui.size()
    lines = [f"Active window: {title or 'unknown'}", f"Screen: {lw}x{lh}"]
    if elements:
        lines.append("Elements (click by id):")
        for i, e in enumerate(elements, 1):
            val = f" = '{e['value']}'" if e.get("value") else ""
            lines.append(f"  e{i} {e['type']} '{e['name']}'{val} @({e['x'] + e['w'] // 2},{e['y'] + e['h'] // 2})")
    else:
        lines.append("No element list on this system — use find_on_screen to locate things, then click 'x,y'.")
    if describe:
        from .documents import ask_vision
        hint = " Pink numbered boxes (e1, e2...) mark elements; mention their ids." if elements else ""
        lines.append("\nVision: " + ask_vision(rt, [annotated_path], question + hint))
    return truncate("\n".join(lines), max(4000, rt.cfg.agent["max_tool_output_chars"]))


@tool("find_on_screen", "Find something on screen visually (icons, images, unlabeled buttons). Returns coordinates "
      "to click. Use when the element list from see_screen doesn't have it.",
      {"description": {"type": "string", "description": "e.g. 'the green play button'"}}, ["description"], group="desktop")
@_failsafe
def find_on_screen(rt, description: str) -> str:
    img, _, scale = _screenshot(rt)
    cols, rows = 8, 6
    small = img.resize((img.width // 2, img.height // 2)) if img.width > 2000 else img
    n = _ask_cell(rt, _grid(small, cols, rows), description, cols * rows)
    if n <= 0:
        return f"couldn't see '{description}' on screen"
    cx, cy = grid_cell_center(n, cols, rows, img.width, img.height)
    # zoom: crop a 2x2-cell window around it and refine on a 4x4 grid
    cw, ch = img.width / cols, img.height / rows
    x0, y0 = max(0, cx - cw), max(0, cy - ch)
    x1, y1 = min(img.width, cx + cw), min(img.height, cy + ch)
    crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
    crop = crop.resize((crop.width * 2, crop.height * 2))
    m = _ask_cell(rt, _grid(crop, 4, 4), description, 16)
    if m > 0:
        fx, fy = grid_cell_center(m, 4, 4, x1 - x0, y1 - y0)
        cx, cy = x0 + fx, y0 + fy
    x, y = int(cx / scale), int(cy / scale)
    st = _state(rt)
    st["found"][description.lower()] = (x, y)
    return f"'{description}' is at about ({x},{y}). Click it with click(target='{x},{y}')."


def _resolve(rt, target: str) -> tuple[int, int, str]:
    st = _state(rt)
    t = target.strip()
    if re.fullmatch(r"e\d+", t, re.I):
        e = st["elements"].get(t.lower())
        if not e:
            raise ValueError(f"no element {t} in the latest see_screen; call see_screen again")
        return e["x"] + e["w"] // 2, e["y"] + e["h"] // 2, e["name"] or e["type"]
    m = re.fullmatch(r"\(?\s*(-?\d+)\s*,\s*(-?\d+)\s*\)?", t)
    if m:
        return int(m.group(1)), int(m.group(2)), t
    names = {k: e for k, e in st["elements"].items() if e.get("name")}
    exact = [e for e in names.values() if e["name"].lower() == t.lower()]
    if not exact:
        close = difflib.get_close_matches(t.lower(), [e["name"].lower() for e in names.values()], n=1, cutoff=0.6)
        exact = [e for e in names.values() if close and e["name"].lower() == close[0]]
    if exact:
        e = exact[0]
        return e["x"] + e["w"] // 2, e["y"] + e["h"] // 2, e["name"]
    if t.lower() in st["found"]:
        x, y = st["found"][t.lower()]
        return x, y, t
    raise ValueError(f"couldn't resolve '{t}'. Use an element id from see_screen, 'x,y', or find_on_screen first.")


@tool("click", "Click on screen. target = element id from see_screen ('e12'), coordinates 'x,y', or the element's "
      "visible name. Actions that delete or pay ask the user first.",
      {"target": {"type": "string"}, "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "double": {"type": "boolean"}}, ["target"], group="desktop")
@_failsafe
def click(rt, target: str, button: str = "left", double: bool = False) -> str:
    x, y, label = _resolve(rt, target)
    _guard(rt, label, "Click")
    gui = _gui()
    gui.moveTo(x, y, duration=0.15)
    gui.click(x, y, clicks=2 if double else 1, button=button or "left")
    time.sleep(0.6)
    return f"{'double-' if double else ''}{button or 'left'}-clicked '{label}' at ({x},{y}). Check the result with see_screen."


@tool("type_text", "Type text into whatever has keyboard focus (click the field first). Set enter=true to press Enter after.",
      {"text": {"type": "string"}, "enter": {"type": "boolean"}}, ["text"], group="desktop")
@_failsafe
def type_text(rt, text: str, enter: bool = False) -> str:
    gui = _gui()
    if not text.isascii() or len(text) > 120:
        try:
            import pyperclip  # type: ignore
            pyperclip.copy(text)
            gui.hotkey("command" if IS_MAC else "ctrl", "v")
        except Exception:
            gui.write(text.encode("ascii", "ignore").decode(), interval=0.01)
    else:
        gui.write(text, interval=0.01)
    if enter:
        time.sleep(0.2)
        gui.press("enter")
    time.sleep(0.3)
    return f"typed {len(text)} characters" + (" and pressed Enter" if enter else "")


_KEYMAP = {"cmd": "command", "win": "win", "windows": "win", "super": "win", "control": "ctrl", "return": "enter",
           "esc": "escape", "del": "delete", "option": "alt", "pgup": "pageup", "pgdn": "pagedown"}


@tool("press_keys", "Press a key or shortcut, e.g. 'enter', 'ctrl+s', 'alt+tab', 'cmd+space', 'ctrl+shift+t', 'down'. "
      "Use 'repeat' to press it several times.",
      {"keys": {"type": "string"}, "repeat": {"type": "integer"}}, ["keys"], group="desktop")
@_failsafe
def press_keys(rt, keys: str, repeat: int = 1) -> str:
    parts = [_KEYMAP.get(k.strip().lower(), k.strip().lower()) for k in re.split(r"\s*\+\s*", keys) if k.strip()]
    combo = "+".join(parts)
    if combo in ("shift+delete", "ctrl+shift+delete", "shift+del") or (IS_MAC and combo == "command+option+backspace"):
        _guard(rt, "permanently delete", "Press " + combo)
    gui = _gui()
    for _ in range(max(1, min(int(repeat or 1), 50))):
        rt.check_cancel()
        gui.hotkey(*parts) if len(parts) > 1 else gui.press(parts[0])
        time.sleep(0.08)
    time.sleep(0.4)
    return f"pressed {combo}" + (f" x{repeat}" if repeat and repeat > 1 else "")


@tool("scroll", "Scroll the window under the mouse (or under target).",
      {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": {"type": "integer"},
       "target": {"type": "string", "description": "optional element id or 'x,y' to scroll over"}}, ["direction"], group="desktop")
@_failsafe
def scroll(rt, direction: str, amount: int = 5, target: str = "") -> str:
    gui = _gui()
    if target:
        x, y, _ = _resolve(rt, target)
        gui.moveTo(x, y, duration=0.1)
    n = max(1, min(int(amount or 5), 50))
    if direction in ("left", "right"):
        gui.hscroll(n * 40 * (-1 if direction == "left" else 1))
    else:
        gui.scroll(n * (120 if IS_WIN else 5) * (1 if direction == "up" else -1))
    time.sleep(0.4)
    return f"scrolled {direction} {n}"


@tool("list_windows", "List open windows (to find an app that's already running).", {}, group="desktop")
@_failsafe
def list_windows(rt) -> str:
    if IS_WIN:
        try:
            import uiautomation as auto  # type: ignore
            names = [w.Name for w in auto.GetRootControl().GetChildren() if w.Name]
            return "\n".join(names) or "no windows"
        except ImportError:
            return INSTALL_HINT
    if IS_MAC:
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of every application process whose visible is true'],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip().replace(", ", "\n") or "no windows"
    if shutil.which("wmctrl"):
        return subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout.strip()
    return "listing windows needs wmctrl on Linux (sudo apt install wmctrl)"


@tool("focus_window", "Bring an open window to the front by (part of) its title.", {"title": {"type": "string"}}, ["title"], group="desktop")
@_failsafe
def focus_window(rt, title: str) -> str:
    if IS_WIN:
        import uiautomation as auto  # type: ignore
        for w in auto.GetRootControl().GetChildren():
            if title.lower() in (w.Name or "").lower():
                try:
                    w.SetActive()
                    w.SetFocus()
                except Exception:
                    pass
                time.sleep(0.5)
                return f"focused '{w.Name}'"
        return f"no window matching '{title}'"
    if IS_MAC:
        subprocess.run(["osascript", "-e", f'tell application "{title}" to activate'])
        time.sleep(0.6)
        return f"activated {title}"
    if shutil.which("wmctrl"):
        subprocess.run(["wmctrl", "-a", title])
        return f"focused {title}"
    return "focusing windows needs wmctrl on Linux"


@tool("wait", "Wait for something to load (seconds, max 30).", {"seconds": {"type": "number"}}, ["seconds"], group="desktop")
def wait(rt, seconds: float) -> str:
    end = time.time() + max(0.0, min(float(seconds), 30.0))
    while time.time() < end:
        rt.check_cancel()
        time.sleep(0.2)
    return f"waited {seconds}s"
