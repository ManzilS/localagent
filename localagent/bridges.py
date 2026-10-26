"""Subscription brains — use a ChatGPT or Google (Gemini) subscription
without API keys, through each company's own official command-line app.

    ChatGPT  → OpenAI Codex CLI, signed in with "Sign in with ChatGPT"
    Gemini   → Google Antigravity CLI (agy), signed in with a Google account
               (the older Gemini CLI still works with an API key or a work licence)

The user installs the official app and signs in once (the GUI has buttons
for both). LocalAgent then sends it a prompt and reads the reply. The CLI's
own tools are kept out of the way: it runs in an empty scratch folder,
read-only sandbox, and is told to answer with <tool_call> blocks that
LocalAgent executes itself — so the never-delete guarantees still apply.

Why not drive chatgpt.com / gemini.google.com / claude.ai in a browser?
Those sites forbid automated use in their terms, change their pages often,
and throw CAPTCHAs; accounts can get flagged. The official CLIs are the
supported way to use a subscription from software.

Claude subscriptions are deliberately not bridged: Anthropic does not allow
third-party products to offer claude.ai login or subscription limits without
approval. Claude is available with an API key instead (provider: anthropic).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .llm import LLMError, LLMResponse, Provider

IS_WIN = os.name == "nt"
IS_MAC = platform.system() == "Darwin"


def flatten(messages: list[dict], max_chars: int = 120_000) -> str:
    """Render the agent transcript as one prompt for a text-in/text-out CLI."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "user":
            turns.append(f"[USER]\n{m.get('content') or ''}")
        elif role == "assistant":
            body = m.get("content") or ""
            for c in m.get("tool_calls") or []:
                body += "\n<tool_call>" + json.dumps({"name": c["name"], "arguments": c["arguments"]}) + "</tool_call>"
            turns.append(f"[ASSISTANT]\n{body.strip()}")
        elif role == "tool":
            turns.append(f"[TOOL RESULT: {m.get('name', '')}]\n{m.get('content') or ''}")
    # keep the first user turn (the request) and as much recent history as fits
    head = turns[:1]
    tail: list[str] = []
    budget = max_chars - len(system) - sum(len(t) for t in head) - 2000
    for t in reversed(turns[1:]):
        if budget - len(t) < 0:
            tail.append("[... earlier steps omitted for length ...]")
            break
        tail.append(t)
        budget -= len(t)
    convo = "\n\n".join(head + list(reversed(tail)))
    return (
        "=== INSTRUCTIONS ===\n" + system +
        "\n\n=== CONVERSATION SO FAR ===\n" + convo +
        "\n\n=== YOUR TURN ===\n"
        "Reply as the ASSISTANT in this conversation. You are only the reasoning engine: do NOT run commands, "
        "read or write files, browse, or use any tools of your own — LocalAgent does that. To take an action, "
        "output one or more <tool_call>{\"name\": ..., \"arguments\": {...}}</tool_call> blocks using the tools "
        "listed in the instructions, then stop and wait for the results. When the task is done, give the final "
        "answer as plain text with no tool_call blocks."
    )


def open_terminal(command: str, title: str = "LocalAgent") -> None:
    """Run a command in a new visible terminal window (for installs and sign-ins)."""
    if IS_WIN:
        subprocess.Popen(["cmd", "/c", "start", title, "cmd", "/k", command], creationflags=0x00000010)  # new console: shown on purpose
        return
    if IS_MAC:
        script = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                          "-e", 'tell application "Terminal" to activate'])
        return
    wrapped = f"{command}; echo; read -p 'Done — press Enter to close this window.' _"
    for term, args in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                       ("xfce4-terminal", ["-x"]), ("xterm", ["-e"])):
        if shutil.which(term):
            subprocess.Popen([term, *args, "bash", "-lc", wrapped])
            return
    raise RuntimeError(f"No terminal found. Run this yourself: {command}")


class CLIBridge(Provider):
    """A provider backed by an official, subscription-signed-in CLI."""
    key = "bridge"
    display = "CLI"
    exe = ""
    supports_tools = False   # tools are described in the prompt; replies use <tool_call> text
    accepts_images = False
    npm_package = ""
    signin_hint = ""

    def __init__(self, model: str = "", timeout: float = 600.0, **_):
        super().__init__(model or "default", "local-cli", None, timeout=timeout)
        self.name = self.key
        self.timeout = timeout
        self._status: bool | None = None

    @property
    def label(self) -> str:
        return f"{self.display}" + (f" ({self.model})" if self.model and self.model != "default" else "")

    # ---------------------------------------------------------- status
    def exe_path(self) -> str | None:
        return shutil.which(self.exe)

    def installed(self) -> bool:
        return self.exe_path() is not None

    def signed_in(self) -> bool:
        raise NotImplementedError

    def available(self, refresh: bool = False) -> bool:
        if self._status is None or refresh:
            try:
                self._status = self.installed() and self.signed_in()
            except Exception:
                self._status = False
        return self._status

    def status(self) -> dict:
        inst = self.installed()
        signed = False
        if inst:
            try:
                signed = self.signed_in()
            except Exception:
                signed = False
        self._status = inst and signed
        return {"key": self.key, "name": self.display, "installed": inst, "signed_in": signed,
                "node": shutil.which("npm") is not None, "install_command": self.install_command(),
                "login_command": self.login_command(), "hint": self.signin_hint}

    def install_command(self) -> str:
        if not shutil.which("npm"):
            return "winget install -e --id OpenJS.NodeJS.LTS" if IS_WIN else ""
        return f"npm install -g {self.npm_package}"

    def login_command(self) -> str:
        raise NotImplementedError

    def install(self) -> str:
        cmd = self.install_command()
        if not cmd:
            raise RuntimeError("Node.js is required first: install the LTS version from https://nodejs.org, then click Install again.")
        open_terminal(cmd, f"Install {self.display}")
        return cmd

    def login(self) -> str:
        cmd = self.login_command()
        open_terminal(cmd, f"Sign in to {self.display}")
        return cmd

    # ------------------------------------------------------------ chat
    def argv(self, out_file: str, images: list[str], prompt: str = "") -> list[str]:
        raise NotImplementedError

    def stdin_for(self, prompt: str) -> str | None:
        """What to send on standard input (the prompt, unless argv carries it)."""
        return prompt

    def parse(self, stdout: str, out_file: str) -> str:
        return stdout.strip()

    def should_retry(self, err: str) -> bool:
        """A failure the bridge can work around itself (e.g. by dropping a model the
        account can't use). Adjusts its own settings and returns True to try once more."""
        return False

    # Words that mean the provider no longer serves this kind of account through this app.
    INELIGIBLE_WORDS = ("ineligibletier", "unsupported_client", "no longer supported", "not eligible")
    ineligible_hint = ""
    update_hint = ""

    def explain(self, err: str, code: int) -> LLMError:
        """Turn a CLI failure into a message a person can act on."""
        low = err.lower()
        if any(w in low for w in self.INELIGIBLE_WORDS):
            self._status = False
            self.on_ineligible(err)
            return LLMError(f"{self.display}: {self.ineligible_hint or 'this account can no longer use this app.'} "
                            f"({err[:200]})")
        if self.update_hint and any(w in low for w in ("newer version", "please upgrade", "update to the latest")):
            return LLMError(f"{self.display}: {self.update_hint} ({err[:200]})")
        if any(w in low for w in ("login", "log in", "sign in", "unauthorized", "auth")):
            self._status = False
            return LLMError(f"{self.display}: not signed in. Open LocalAgent settings and click Sign in. ({err[:200]})")
        if any(w in low for w in ("rate limit", "usage limit", "quota", "429")):
            return LLMError(f"{self.display}: subscription usage limit reached ({err[:200]})")
        return LLMError(f"{self.display} failed (exit {code}): {err}")

    def on_ineligible(self, err: str) -> None:
        pass

    def chat(self, messages, tools=None, *, temperature=0.3, max_tokens=4096, think=None, num_ctx=None, on_token=None):
        exe = self.exe_path()
        if not exe:
            raise LLMError(f"{self.display} app is not installed")
        prompt = flatten(messages)
        images = [p for m in messages[-3:] for p in (m.get("images") or [])] if self.accepts_images else []
        with tempfile.TemporaryDirectory(prefix="la_brain_") as scratch:
            out_file = os.path.join(scratch, "reply.txt")
            for attempt in range(2):
                argv = [exe] + self.argv(out_file, images, prompt)
                self.last_error = ""
                try:
                    proc = subprocess.run(argv, input=self.stdin_for(prompt), capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=self.timeout, cwd=scratch,
                                          env={**os.environ, "NO_COLOR": "1"})
                except subprocess.TimeoutExpired:
                    raise LLMError(f"{self.display} timed out after {int(self.timeout)}s")
                text = self.parse(proc.stdout, out_file)
                if proc.returncode == 0 or text:
                    break
                err = (self.last_error or proc.stderr or proc.stdout or "").strip()[-600:]
                if attempt == 0 and self.should_retry(err):
                    continue
                raise self.explain(err, proc.returncode)
        return LLMResponse(content=text)

    last_error = ""   # set by parse() when the CLI reports an error inside its output


class ChatGPTBridge(CLIBridge):
    key = "chatgpt"
    display = "ChatGPT"
    exe = "codex"
    accepts_images = True
    npm_package = "@openai/codex"
    signin_hint = "A browser window opens — sign in with your ChatGPT account (Plus, Pro, Business, Edu or Enterprise)."

    def signed_in(self) -> bool:
        try:
            r = subprocess.run([shutil.which(self.exe) or self.exe, "login", "status"], capture_output=True,
                               text=True, timeout=20)
            if r.returncode == 0:
                return True
            if "not logged in" in (r.stdout + r.stderr).lower():
                return False
        except (OSError, subprocess.TimeoutExpired):
            pass
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        return (home / "auth.json").exists()

    def login_command(self) -> str:
        return "codex login"

    # Speed: LocalAgent only needs Codex to *think*; it runs tools itself. So skip
    # the user's Codex setup (MCP servers, plugins, memories — measured 7-20 s of
    # startup per call), don't save sessions (also avoids clashing with the
    # Codex desktop app's session database), and pass the model explicitly.
    FAST_FLAGS = ["--ignore-user-config", "--ephemeral", "--ignore-rules"]

    update_hint = ("this model needs a newer ChatGPT app. Update it with: npm install -g @openai/codex@latest "
                   "(or pick another model in Settings).")

    def __init__(self, model: str = "", timeout: float = 600.0, reasoning: str = "", **_):
        super().__init__(model=model, timeout=timeout)
        self.reasoning = reasoning
        self.use_default_model = False   # set when the chosen model is refused; Codex then picks its own

    MODEL_REFUSED = ("model_not_found", "not supported", "newer version", "does not exist", "unknown model",
                     "invalid model", "not available")

    def should_retry(self, err: str) -> bool:
        low = err.lower()
        if self.use_default_model or "model" not in low or not any(w in low for w in self.MODEL_REFUSED):
            return False
        self.use_default_model = True
        return True

    @staticmethod
    def user_settings() -> dict:
        """model / model_reasoning_effort from ~/.codex/config.toml (top-level keys only)."""
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        out: dict = {}
        try:
            for line in (home / "config.toml").read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("["):
                    break  # only the top-level table
                m = re.match(r'\s*(model|model_reasoning_effort)\s*=\s*"([^"]+)"', line)
                if m:
                    out[m.group(1)] = m.group(2)
        except OSError:
            pass
        return out

    def argv(self, out_file: str, images: list[str], prompt: str = "") -> list[str]:
        a = ["exec", "--skip-git-repo-check", "--sandbox", "read-only", *self.FAST_FLAGS, "--output-last-message", out_file]
        if not self.use_default_model:
            user = self.user_settings()
            model = self.model if self.model and self.model != "default" else user.get("model")
            if model:
                a += ["--model", model]
            effort = self.reasoning or user.get("model_reasoning_effort")
            if effort:
                a += ["-c", f'model_reasoning_effort="{effort}"']
        for img in images:
            a += ["--image", img]
        return a + ["-"]  # prompt from stdin

    def parse(self, stdout: str, out_file: str) -> str:
        try:
            text = Path(out_file).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
        return stdout.strip()


def _json_objects(text: str):
    """Every JSON object in CLI output: one per line (NDJSON), or one spanning the whole output."""
    start = text.find("{")
    if start < 0:
        return
    try:
        obj = json.loads(text[start:])
        if isinstance(obj, dict):
            yield obj
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _text_of(content) -> str:
    """Plain text from a message's content (a string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    return ""


class GeminiBridge(CLIBridge):
    """Google subscription brain.

    Google retired the Gemini CLI for personal accounts on 2026-06-18 (it now
    answers IneligibleTierError); its replacement for free, AI Pro and Ultra
    accounts is the Antigravity CLI (`agy`). This bridge uses `agy` when it's
    installed and falls back to the old `gemini` CLI, which still serves API
    keys and Code Assist Standard/Enterprise licences.
    """
    key = "gemini"
    display = "Gemini"
    exe = "agy"
    legacy_exe = "gemini"
    signin_hint = ("A terminal opens. Sign in with your Google account in the browser, then type /exit. "
                   "Works with a free Google account or Google AI Pro/Ultra.")
    ineligible_hint = ("Google retired the Gemini CLI for personal accounts. Click Install next to Gemini to get "
                       "Google's new Antigravity CLI, then Sign in.")

    # Longest prompt passed as a command-line argument; longer ones go on stdin as stream-json.
    # Windows caps a command line at 32,767 characters (8,191 through a .cmd/.bat shim).
    ARG_LIMIT = 24_000

    @staticmethod
    def retired_marker() -> Path:
        return Path(os.environ.get("LOCALAGENT_HOME", Path.home() / ".localagent")).expanduser() / "gemini_cli_retired"

    @staticmethod
    def find_agy() -> str | None:
        found = shutil.which("agy")
        if found:
            return found
        home = Path.home()
        # The installers put it in %LOCALAPPDATA%\agy\bin (Windows) or ~/.local/bin, and add that to PATH —
        # which an already-running LocalAgent doesn't see until it restarts.
        for p in (Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "agy" / "bin" / "agy.exe",
                  home / ".local" / "bin" / ("agy.exe" if IS_WIN else "agy")):
            if p.exists():
                return str(p)
        return None

    def engine(self) -> tuple[str, str | None]:
        """("agy" | "gemini" | "", path) — the CLI this bridge will drive."""
        agy = self.find_agy()
        if agy:
            return "agy", agy
        legacy = shutil.which(self.legacy_exe)
        if legacy and (self._has_api_key() or not self.retired_marker().exists()):
            return "gemini", legacy
        return "", None

    def exe_path(self) -> str | None:
        return self.engine()[1]

    @staticmethod
    def _has_api_key() -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def signed_in(self) -> bool:
        kind, _ = self.engine()
        g = Path.home() / ".gemini"
        if kind == "agy":
            # agy keeps its sign-in in the OS keychain; it creates its settings folder on first sign-in.
            return self._has_api_key() or (g / "antigravity-cli").exists()
        if kind == "gemini":
            return self._has_api_key() or (g / "oauth_creds.json").exists() or (g / "google_accounts.json").exists()
        return False

    def on_ineligible(self, err: str) -> None:
        if self.engine()[0] == "gemini":
            try:
                self.retired_marker().parent.mkdir(parents=True, exist_ok=True)
                self.retired_marker().write_text(err[:500], encoding="utf-8")
            except OSError:
                pass

    def install_command(self) -> str:
        if IS_WIN:
            return 'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://antigravity.google/cli/install.ps1 | iex"'
        return "curl -fsSL https://antigravity.google/cli/install.sh | bash"

    def install(self) -> str:
        cmd = self.install_command()
        open_terminal(cmd, f"Install {self.display}")
        return cmd

    def login_command(self) -> str:
        return "agy" if self.engine()[0] != "gemini" else "gemini"

    def _argv_via_stdin(self, exe: str, prompt: str) -> bool:
        limit = 7_000 if exe.lower().endswith((".cmd", ".bat")) else self.ARG_LIMIT
        return len(prompt) > limit or (IS_WIN and exe.lower().endswith((".cmd", ".bat")) and "\n" in prompt)

    def argv(self, out_file: str, images: list[str], prompt: str = "") -> list[str]:
        kind, exe = self.engine()
        model = ["--model", self.model] if self.model and self.model != "default" else []
        if kind != "agy":
            return ["--output-format", "json", *model,
                    "-p", "Respond to the conversation provided on standard input, following its instructions exactly."]
        # Headless agy: no slash commands, sandboxed terminal, and any tool needing approval is
        # refused (we never pass --dangerously-skip-permissions) — LocalAgent runs the tools.
        a = ["--disable-slash-commands", "--sandbox", "--print-timeout", f"{int(self.timeout)}s", *model]
        if self._argv_via_stdin(exe or "", prompt):
            return a + ["--input-format", "stream-json", "--output-format", "stream-json", "-p", ""]
        return a + ["--output-format", "json", "-p", prompt]

    def stdin_for(self, prompt: str) -> str | None:
        kind, exe = self.engine()
        if kind != "agy":
            return prompt
        if self._argv_via_stdin(exe or "", prompt):
            return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        return ""

    def parse(self, stdout: str, out_file: str) -> str:
        # agy json: {"status", "response", "error"?}. agy stream-json: NDJSON events — "step_update"
        # events carry "text_delta" pieces of the reply and a final {"event": "result", "result": {...json shape}}.
        final, pieces = "", []
        for obj in _json_objects(stdout):
            if isinstance(obj.get("step_update"), dict):
                su = obj["step_update"]
                if su.get("step_type") == "agent_response" and isinstance(su.get("text_delta"), str):
                    pieces.append(su["text_delta"])
                continue
            if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
                obj = obj["result"]
            status = str(obj.get("status", "")).upper()
            if obj.get("error") or status in ("ERROR", "INVALID", "CANCELED", "INTERRUPTED"):
                e = obj.get("error")
                self.last_error = (e.get("message") or json.dumps(e)) if isinstance(e, dict) else str(e or status)
                continue
            for k in ("response", "result", "text", "output"):
                if isinstance(obj.get(k), str) and obj[k].strip():
                    final = obj[k].strip()
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role", "assistant") == "assistant" and obj.get("event") != "user":
                pieces.append(_text_of(msg.get("content")))
        if final or pieces:
            return final or "".join(pieces).strip()
        if self.last_error:
            return ""
        return "" if stdout.lstrip().startswith("{") else stdout.strip()


BRIDGES: dict[str, type[CLIBridge]] = {"chatgpt": ChatGPTBridge, "gemini": GeminiBridge}


def all_bridges() -> list[CLIBridge]:
    return [cls() for cls in BRIDGES.values()]
