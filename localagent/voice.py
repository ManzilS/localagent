"""Speech to text, fully local (optional: pip install 'localagent[voice]').

Uses faster-whisper. The model downloads once on first use (~150 MB for
'base'). Replies are read aloud by the app itself (the operating system's
built-in voices), so no extra install is needed for speaking.
"""
from __future__ import annotations

import threading

_model = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe(path: str, cfg=None) -> str:
    global _model
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise RuntimeError("voice input needs: pip install 'localagent[voice]'")
    v = (cfg.data.get("voice") if cfg else None) or {}
    with _lock:
        if _model is None:
            size = v.get("model") or ("small" if (cfg and cfg.data.get("profile", "").startswith("gpu")) else "base")
            try:
                _model = WhisperModel(size, device="auto", compute_type="auto")
            except Exception:
                _model = WhisperModel(size, device="cpu", compute_type="int8")
        segments, _info = _model.transcribe(path, vad_filter=True, language=v.get("language"))
        return " ".join(s.text.strip() for s in segments).strip()
