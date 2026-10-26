"""LocalAgent — a local-first AI assistant with recoverable file-operation safeguards."""

__version__ = "0.4.0"

import os as _os

if _os.name == "nt":
    # The app runs without a console (pythonw). On Windows every console
    # program it starts (nvidia-smi, codex, git, ...) would otherwise flash
    # its own black window. Hide them unless a caller asks for a window.
    import subprocess as _sp

    _CREATE_NO_WINDOW = 0x08000000
    _orig_init = _sp.Popen.__init__

    def _quiet_init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        return _orig_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _quiet_init
