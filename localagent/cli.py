"""Command line: setup, chat, run, web, doctor, trash."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import CONFIG_PATH, DEFAULTS, PROFILES, load_config, profile_overrides, save_config, deep_merge

console = Console()

CLOUD_DEFAULTS = {
    "anthropic": {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"provider": "openai", "model": "gpt-5.5", "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {"provider": "openrouter", "model": "", "api_key_env": "OPENROUTER_API_KEY"},
}

HELP = """[bold]Commands[/bold]
  /mode quick|auto|deep     quick = local & fast · auto = local, escalates to cloud when stuck · deep = best model
  /contemplate <question>   several subagents solve it independently, then synthesize
  /image <path> [prompt]    attach an image (or several: /image a.png b.jpg -- what's different?)
  /trash [filter]           list agent_trash      /restore <id>   put an item back
  /remember <fact>          save to memory        /memories [q]   search memory
  /ps                       background processes  /models         show models in use
  /new                      fresh conversation    /exit           quit
  Paste multi-line input between two lines of \"\"\"."""


# ----------------------------------------------------------------- events

def _short(v, n=90) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def make_printer(verbose: bool = False):
    def on_event(kind: str, data: dict) -> None:
        pad = "  " * int(data.get("subagent", 0))
        sub = "[magenta]sub[/magenta] " if data.get("subagent") else ""
        if kind == "tool_call":
            args = ", ".join(f"{k}={_short(v, 60)}" for k, v in data["arguments"].items())
            console.print(f"{pad}{sub}[cyan]→ {data['name']}[/cyan]([dim]{args}[/dim])")
        elif kind == "tool_result":
            first = (data["output"].strip().splitlines() or [""])[0]
            mark = "[green]✓[/green]" if data["ok"] else "[red]✗[/red]"
            console.print(f"{pad}  {mark} [dim]{_short(first, 110)}[/dim]")
            if verbose:
                console.print(Panel(data["output"][:3000], expand=False, border_style="dim"))
        elif kind == "escalate":
            console.print(f"[yellow]⇪ handing off to {data['to']} ({data['why']})[/yellow]")
        elif kind == "status":
            console.print(f"{pad}[dim]… {data['text']}[/dim]")
        elif kind == "assistant_text" and verbose:
            console.print(f"{pad}[dim italic]{_short(data['text'], 200)}[/dim italic]")
        elif kind == "error":
            console.print(f"[red]{data['text']}[/red]")
    return on_event


def _build(args, on_event=None):
    from .agent import Agent
    from .runtime import Runtime
    cfg = load_config(Path(args.config) if getattr(args, "config", None) else None)
    rt = Runtime(cfg, on_event=on_event)
    rt.approver = _console_approver
    return cfg, rt, Agent(rt, mode=getattr(args, "mode", "auto") or "auto")


def _console_approver(question: str) -> bool:
    try:
        ans = console.input(f"[bold yellow]LocalAgent wants to: {question}. Allow? [y/N][/bold yellow] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() in ("y", "yes")


def _check_backend(rt) -> bool:
    from .hardware import ollama_running
    main = rt.router.main
    if main.name == "ollama" and not ollama_running(main.base_url):
        console.print(Panel("Ollama isn't running. Start it (`ollama serve`, or open the Ollama app) "
                            "then try again. First time? Run [bold]localagent setup[/bold].", border_style="red"))
        return False
    return True


# ---------------------------------------------------------------- commands

def cmd_setup(args) -> None:
    from .hardware import detect, ensure_model, installed_models, ollama_running, pick_profile
    import shutil as _sh
    info = detect()
    profile = args.profile or pick_profile(info)
    console.print(Panel(f"OS: {info['os']} {info['arch']}\nRAM: {info['ram_gb']} GB\n"
                        f"GPU: {info['gpu'] or 'none detected'}" + (f" ({info['vram_gb']} GB)" if info['gpu'] else "")
                        + f"\n\nProfile: [bold]{profile}[/bold]", title="Hardware"))
    p = PROFILES[profile]
    base = DEFAULTS["providers"]["ollama"]["base_url"]
    existing = {}
    if CONFIG_PATH.exists():
        import yaml
        existing = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    data = deep_merge(existing, profile_overrides(profile))
    data.setdefault("workspace", args.workspace or DEFAULTS["workspace"])
    if args.workspace:
        data["workspace"] = args.workspace
    data.setdefault("trash_dir", DEFAULTS["trash_dir"])

    if not _sh.which("ollama"):
        console.print(Panel("Ollama is not installed.\n\n"
                            "Linux:   curl -fsSL https://ollama.com/install.sh | sh\n"
                            "Mac/Win: download from https://ollama.com/download\n\n"
                            "Then run [bold]localagent setup[/bold] again.", border_style="yellow"))
    elif not ollama_running(base):
        console.print("[yellow]Ollama is installed but not running — start it with `ollama serve` and rerun setup.[/yellow]")
    else:
        have = installed_models(base)
        console.print("[bold]Main model[/bold]")
        main = ensure_model(p["main_candidates"], base, have, console.print)
        console.print("[bold]Vision model[/bold]")
        vision = ensure_model(p["vision_candidates"], base, have, console.print)
        console.print("[bold]Embedding model[/bold] (for memory search)")
        embed = ensure_model(["nomic-embed-text", "embeddinggemma"], base, have, console.print)
        data["models"]["main"] = {"provider": "ollama", "model": main or p["main_candidates"][0]}
        data["models"]["vision"] = {"provider": "ollama", "model": vision or main or p["vision_candidates"][0]}
        if embed:
            data["models"]["embed"] = {"provider": "ollama", "model": embed}

    cloud = args.cloud
    if cloud == "none":
        data["models"].pop("cloud", None)
        data["models"]["cloud"] = None
    else:
        spec = dict(CLOUD_DEFAULTS[cloud])
        if args.cloud_model:
            spec["model"] = args.cloud_model
        if not spec["model"]:
            console.print("[yellow]--cloud openrouter needs --cloud-model (e.g. a model slug from openrouter.ai)[/yellow]")
        data["models"]["cloud"] = spec
    path = save_config(data)
    key_env = (data["models"].get("cloud") or {}).get("api_key_env")
    key_note = ""
    if key_env:
        key_note = (f"\nCloud fallback: {data['models']['cloud']['model']} — "
                    + ("[green]key found[/green]" if os.environ.get(key_env) else
                       f"[yellow]set {key_env} to enable it[/yellow] (the agent stays fully local without it)"))
    console.print(Panel(f"Config written to {path}\nWorkspace: {Path(data['workspace']).expanduser()}\n"
                        f"Trash: {data['trash_dir']} (inside the workspace){key_note}\n\n"
                        "Start chatting: [bold]localagent[/bold]   ·   Web UI: [bold]localagent web[/bold]",
                        title="Ready", border_style="green"))
    if profile == "laptop":
        console.print("[dim]8 GB tip: set OLLAMA_MAX_LOADED_MODELS=1 and close browsers while it works.[/dim]")


def cmd_doctor(args) -> None:
    from .hardware import detect, installed_models, ollama_running
    from .safety import check_shell_command
    cfg, rt, agent = _build(args)
    t = Table(title=f"localagent {__version__} doctor", show_header=False)
    info = detect()
    t.add_row("hardware", f"{info['ram_gb']} GB RAM, GPU: {info['gpu'] or 'none'} {info['vram_gb'] or ''}")
    t.add_row("profile", cfg["profile"])
    base = cfg.providers["ollama"]["base_url"]
    up = ollama_running(base)
    t.add_row("ollama", "[green]running[/green]" if up else "[red]not reachable[/red]")
    have = installed_models(base) if up else set()
    for role in ("main", "vision", "embed"):
        spec = cfg.models.get(role) or {}
        m = spec.get("model", "-")
        ok = spec.get("provider") != "ollama" or m in have
        t.add_row(role, f"{m} " + ("[green]✓[/green]" if ok else "[red]missing — run localagent setup[/red]"))
    chain = rt.router.brain_chain()
    t.add_row("backup brains", ", ".join(p.label for p in chain) if chain else "[dim]none connected (fully local) — see `localagent brains`[/dim]")
    t.add_row("workspace", str(cfg.workspace))
    t.add_row("trash", str(cfg.trash_dir))
    t.add_row("delete guard", "[green]active[/green]" if check_shell_command("git clean -fd") else "[red]inactive[/red]")
    for mod, extra in (("playwright", "browser"), ("pypdfium2", "vision"), ("mss", "vision")):
        try:
            __import__(mod)
            t.add_row(mod, "[green]installed[/green]")
        except ImportError:
            t.add_row(mod, f"[dim]optional — pip install 'localagent\\[{extra}]'[/dim]")
    console.print(t)


def cmd_run(args) -> None:
    cfg, rt, agent = _build(args, on_event=make_printer(args.verbose))
    if not _check_backend(rt):
        sys.exit(1)
    task = " ".join(args.task)
    if task == "-":
        task = sys.stdin.read()
    images = args.image or None
    try:
        out = agent.run(task, images=images)
        console.print(Markdown(out))
    finally:
        rt.shutdown()


def cmd_trash(args) -> None:
    cfg = load_config()
    from .trash import Trash
    tr = Trash(cfg.trash_dir)
    if args.restore:
        console.print(f"restored {tr.restore(args.restore)}")
        return
    items = tr.list(limit=args.limit, query=args.query or "")
    t = Table("id", "kind", "original", "reason")
    for e in items:
        t.add_row(e["id"], e["action"], e["original"], e.get("reason", ""))
    console.print(t if items else "agent_trash is empty")


def _read_input() -> str | None:
    try:
        line = console.input("[bold green]›[/bold green] ")
    except (EOFError, KeyboardInterrupt):
        return None
    if line.strip() == '"""':
        lines = []
        while True:
            try:
                l = input()
            except EOFError:
                break
            if l.strip() == '"""':
                break
            lines.append(l)
        return "\n".join(lines)
    return line


def cmd_chat(args) -> None:
    from .tools.subagents import contemplate_run
    cfg, rt, agent = _build(args, on_event=make_printer(args.verbose))
    if not _check_backend(rt):
        sys.exit(1)
    cloud = rt.router.cloud
    console.print(Panel(f"[bold]LocalAgent[/bold] {__version__} · {rt.router.main.label}"
                        + (f" · cloud: {cloud.label}" if cloud else " · fully local")
                        + f"\nmode: {agent.mode} · workspace: {cfg.workspace}\n"
                        "Nothing is ever deleted — removed files go to agent_trash. Type /help.", border_style="blue"))
    try:
        while True:
            text = _read_input()
            if text is None:
                break
            text = text.strip()
            if not text:
                continue
            images = None
            if text.startswith("/"):
                cmd, _, rest = text.partition(" ")
                rest = rest.strip()
                if cmd in ("/exit", "/quit"):
                    break
                if cmd == "/help":
                    console.print(HELP); continue
                if cmd == "/new":
                    agent.reset(); console.print("[dim]new conversation[/dim]"); continue
                if cmd == "/mode":
                    try:
                        agent.set_mode(rest)
                        console.print(f"mode: {agent.mode} · model: {agent.provider.label}")
                    except ValueError as e:
                        console.print(f"[red]{e}[/red]")
                    continue
                if cmd == "/models":
                    for role in ("main", "vision", "embed", "cloud"):
                        p = rt.router.get(role)
                        console.print(f"{role:7} {p.label if p else '-'}")
                    continue
                if cmd == "/trash":
                    from .tools.files import list_trash
                    console.print(list_trash(rt, query=rest)); continue
                if cmd == "/restore":
                    try:
                        console.print(f"restored {rt.trash.restore(rest)}")
                    except Exception as e:
                        console.print(f"[red]{e}[/red]")
                    continue
                if cmd == "/remember":
                    console.print(f"saved memory #{rt.memory.add(rest)}"); continue
                if cmd == "/memories":
                    hits = rt.memory.search(rest, 10) if rest else rt.memory.recent(10)
                    for h in hits:
                        console.print(f"#{h['id']} {h['text']}")
                    if not hits:
                        console.print("[dim]no memories[/dim]")
                    continue
                if cmd == "/ps":
                    from .tools.shell import list_processes
                    console.print(list_processes(rt)); continue
                if cmd == "/contemplate":
                    try:
                        rt.local.agent = agent
                        console.print(Markdown(contemplate_run(rt, rest)))
                    except KeyboardInterrupt:
                        console.print("[yellow]interrupted[/yellow]")
                    continue
                if cmd == "/image":
                    paths, _, prompt = rest.partition(" -- ")
                    parts = paths.split()
                    if " -- " not in rest and len(parts) > 1 and not Path(os.path.expanduser(parts[-1])).exists():
                        prompt = " ".join(parts[1:]); parts = parts[:1]
                    images = [str(Path(os.path.expanduser(p)).resolve()) for p in parts]
                    missing = [p for p in images if not Path(p).exists()]
                    if missing:
                        console.print(f"[red]not found: {missing}[/red]"); continue
                    text = prompt or "What is in this image?"
                else:
                    console.print(f"[red]unknown command {cmd}[/red] — /help"); continue
            try:
                out = agent.run(text, images=images)
                console.print()
                console.print(Markdown(out))
                console.print()
            except KeyboardInterrupt:
                rt.cancel.set()
                agent.repair()
                console.print("[yellow]interrupted — tell me how to continue[/yellow]")
    finally:
        rt.shutdown()


def cmd_app(args) -> None:
    from .app import main as app_main
    app_main(getattr(args, "config", None))


def cmd_brains(args) -> None:
    from .bridges import BRIDGES
    if args.action in ("login", "install"):
        if args.name not in BRIDGES:
            console.print(f"[red]choose one of: {', '.join(BRIDGES)}[/red]"); return
        b = BRIDGES[args.name]()
        try:
            console.print(f"Opened a window: {b.login() if args.action == 'login' else b.install()}")
            if args.action == "login":
                console.print(b.signin_hint)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
        return
    t = Table("brain", "installed", "signed in", "how to connect")
    for key, cls in BRIDGES.items():
        st = cls().status()
        how = "" if st["signed_in"] else (f"localagent brains login {key}" if st["installed"]
                                          else (st["install_command"] or "install Node.js from nodejs.org first"))
        t.add_row(st["name"], "✓" if st["installed"] else "–", "✓" if st["signed_in"] else "–", how)
    console.print(t)
    console.print("[dim]Claude connects with an API key (Anthropic doesn't allow subscription sign-in for third-party apps).[/dim]")


def cmd_web(args) -> None:
    from .webui import serve
    serve(host=args.host, port=args.port, config=getattr(args, "config", None))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="localagent", description="Experimental local-first AI assistant with recoverable file-operation safeguards.")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="detect hardware, pull models, write config")
    s.add_argument("--profile", choices=list(PROFILES))
    s.add_argument("--workspace")
    s.add_argument("--cloud", choices=["anthropic", "openai", "openrouter", "none"], default="anthropic")
    s.add_argument("--cloud-model")
    s.set_defaults(fn=cmd_setup)

    c = sub.add_parser("chat", help="interactive chat (default)")
    c.add_argument("--mode", choices=["quick", "auto", "deep"], default="auto")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(fn=cmd_chat)

    r = sub.add_parser("run", help="run one task and exit (use - to read it from stdin)")
    r.add_argument("task", nargs="+")
    r.add_argument("--mode", choices=["quick", "auto", "deep"], default="auto")
    r.add_argument("--image", action="append")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_run)

    w = sub.add_parser("web", help="browser UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=7860)
    w.set_defaults(fn=cmd_web)

    a = sub.add_parser("app", help="open the desktop app")
    a.set_defaults(fn=cmd_app)

    b = sub.add_parser("brains", help="subscription brains: status, install, login")
    b.add_argument("action", nargs="?", default="status", choices=["status", "install", "login"])
    b.add_argument("name", nargs="?", default="")
    b.set_defaults(fn=cmd_brains)

    dm = sub.add_parser("daemon", help="run automations, heartbeat and phone control without a window")
    dm.add_argument("--host", default="127.0.0.1")
    dm.add_argument("--port", type=int, default=7860)
    dm.set_defaults(fn=cmd_web)

    d = sub.add_parser("doctor", help="check models, backend and safety guards")
    d.set_defaults(fn=cmd_doctor)

    t = sub.add_parser("trash", help="list or restore trashed items")
    t.add_argument("query", nargs="?")
    t.add_argument("--restore", metavar="ID")
    t.add_argument("--limit", type=int, default=30)
    t.set_defaults(fn=cmd_trash)

    argv = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(argv + ["chat"])
    args.fn(args)


if __name__ == "__main__":
    main()
