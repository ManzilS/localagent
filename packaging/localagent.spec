# PyInstaller spec: builds the LocalAgent desktop app.
#   pip install ".[all]" pyinstaller && pyinstaller packaging/localagent.spec
# Output: dist/LocalAgent/ (Windows/Linux) or dist/LocalAgent.app (macOS)
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = collect_submodules("localagent") + ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                                             "uvicorn.lifespan.on", "ddgs"]
datas = [("../localagent/web/index.html", "localagent/web"), ("../localagent/trash.py", "localagent"),
         ("../localagent/skills_builtin", "localagent/skills_builtin")]
for pkg in ("webview",):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(["../localagent/launcher.py"], pathex=[".."], hiddenimports=hidden, datas=datas,
             excludes=["playwright", "tkinter.test"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalAgent", console=False,
          icon=None, argv_emulation=sys.platform == "darwin")
coll = COLLECT(exe, a.binaries, a.datas, name="LocalAgent")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="LocalAgent.app", bundle_identifier="dev.localagent.app",
                 info_plist={"NSAppleEventsUsageDescription": "LocalAgent controls apps you ask it to use.",
                             "NSHighResolutionCapable": True,
                             "NSMicrophoneUsageDescription": "LocalAgent listens when you press Speak."})
