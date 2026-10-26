"""Entry point for the packaged desktop app (PyInstaller).

A frozen app has no separate Python interpreter, so the executable also
serves the jobs that normally use `python -m ...`:
    LocalAgent --shim rm -rf build     the never-delete rm/mv/cp shims
    LocalAgent --run-python script.py  the agent's run_python tool (with the trash hook)
    LocalAgent cli ...                 the command line (e.g. `LocalAgent cli doctor`)
With no arguments it opens the desktop app."""
from __future__ import annotations

import runpy
import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--shim"]:
        from localagent.shim_cli import main as shim_main
        sys.argv = [sys.argv[0]] + args[1:]
        shim_main()
    elif args[:1] == ["--run-python"] and len(args) > 1:
        from localagent.safety import SITECUSTOMIZE
        exec(compile(SITECUSTOMIZE, "<localagent-trash-hook>", "exec"), {"__name__": "_la_hook"})
        sys.argv = args[1:]
        runpy.run_path(args[1], run_name="__main__")
    elif args[:1] == ["cli"]:
        from localagent.cli import main as cli_main
        cli_main(args[1:])
    else:
        from localagent.app import main as app_main
        app_main(minimized="--minimized" in args)


if __name__ == "__main__":
    main()
