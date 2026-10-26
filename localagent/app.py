"""Desktop app: `localagent app` (or the packaged LocalAgent executable).

Runs the app server on a free localhost port and shows it in a native
window (pywebview). While the agent controls the mouse and keyboard, the
main window gets out of the way and a small always-on-top bar with a Stop
button appears; it comes back when the task ends or needs your approval.
Falls back to the default browser if pywebview isn't available."""
from __future__ import annotations

import socket
import threading
import time
import webbrowser

DESKTOP_TOOLS = {"open_app", "see_screen", "find_on_screen", "click", "type_text", "press_keys", "scroll", "focus_window"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pick_port(preferred: int) -> int:
    """A fixed port keeps the phone link stable; fall back to any free port."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    return _free_port()


def _reconnect_phone(port: int) -> None:
    from . import remote
    try:
        if remote.info()["running"] and not remote.serving(port):
            remote.start_serving(port)
    except Exception:
        pass


def _serve(app, port: int) -> None:
    import uvicorn
    uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).run()


def _wait_up(port: int, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _ensure_streams() -> None:
    """Started without a console (pythonw / desktop shortcut) there is no
    stdout/stderr, and the web server crashes the first time it logs.
    Send output to ~/.localagent/app.log instead."""
    import sys
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .config import HOME
    HOME.mkdir(parents=True, exist_ok=True)
    log = open(HOME / "app.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main(config: str | None = None, minimized: bool = False) -> None:
    _ensure_streams()
    from .webui import create_app
    app = create_app(config)
    port = _pick_port(int((app.state.la.cfg.data.get("remote") or {}).get("port", 7860)))
    app.state.port = port
    remote_cfg = app.state.la.cfg.data.get("remote") or {}
    if remote_cfg.get("enabled"):
        threading.Thread(target=_reconnect_phone, args=(port,), daemon=True).start()
    threading.Thread(target=_serve, args=(app, port), daemon=True).start()
    if not _wait_up(port):
        raise SystemExit("LocalAgent couldn't start its window server.")
    url = f"http://127.0.0.1:{port}/"
    tok = app.state.token
    try:
        import webview  # pywebview
    except ImportError:
        print(f"Opening LocalAgent in your browser (install 'localagent[app]' for the desktop window)")
        webbrowser.open(f"{url}?t={tok}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    main_win = webview.create_window("LocalAgent", f"{url}?t={tok}", width=1180, height=800, min_size=(720, 520),
                                     minimized=minimized)
    bar = webview.create_window("LocalAgent — working", f"{url}overlay?t={tok}", width=420, height=56,
                                on_top=True, frameless=True, hidden=True, resizable=False, x=40, y=40)
    driving = {"on": False}

    def on_event(kind: str, data: dict) -> None:
        if kind == "tool_call" and data.get("name") in DESKTOP_TOOLS and not driving["on"]:
            driving["on"] = True
            try:
                main_win.minimize()
                bar.show()
            except Exception:
                pass
        elif kind in ("approval", "done", "final") and driving["on"]:
            if kind != "final" or not data.get("subagent"):
                try:
                    main_win.restore()
                    main_win.show()
                    if kind == "done":
                        bar.hide()
                        driving["on"] = False
                except Exception:
                    pass
        if kind == "tool_call" and driving["on"] and data.get("name") in DESKTOP_TOOLS:
            try:  # after an approval the agent resumes driving: get out of the way again
                main_win.minimize()
            except Exception:
                pass

    app.state.la.hooks.append(on_event)
    webview.start()


if __name__ == "__main__":
    main()
