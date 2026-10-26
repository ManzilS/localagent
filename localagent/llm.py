"""Model providers behind one interface.

Internal message format (OpenAI-like):
    {"role": "system"|"user", "content": str, "images": [path, ...]?}
    {"role": "assistant", "content": str, "tool_calls": [{"id","name","arguments"}]?}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Each provider converts to its wire format. Local models that print tool
calls as text instead of using native tool calling are handled by
parse_text_tool_calls().
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    parsed_from_text: bool = False


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _b64(path: str) -> tuple[str, str]:
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return base64.b64encode(data).decode(), mime


# ------------------------------------------------------------ text fallback

_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCE_RE = re.compile(r"```(?:json|tool|tool_call)?\s*(\{.*?\})\s*```", re.S)


def _coerce_call(obj: Any, tool_names: set[str]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    if "function" in obj and isinstance(obj["function"], dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or name not in tool_names:
        return None
    args = obj.get("arguments", obj.get("args", obj.get("parameters", obj.get("input", {}))))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    return ToolCall(id=_new_id(), name=name, arguments=args if isinstance(args, dict) else {})


def _scan_json_objects(text: str) -> list[tuple[int, int, Any]]:
    """Find top-level balanced {...} JSON objects in free text."""
    found, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found.append((i, j + 1, json.loads(text[i : j + 1])))
                    except json.JSONDecodeError:
                        pass
                    break
            j += 1
        i = j + 1 if depth == 0 else i + 1
    return found


def parse_text_tool_calls(content: str, tool_names: set[str]) -> tuple[list[ToolCall], str]:
    """Extract tool calls a model wrote as text. Returns (calls, leftover_text)."""
    if not content or not tool_names:
        return [], content
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for rx in (_TAG_RE, _FENCE_RE):
        for m in rx.finditer(content):
            try:
                obj = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            objs = obj if isinstance(obj, list) else [obj]
            for o in objs:
                tc = _coerce_call(o, tool_names)
                if tc:
                    calls.append(tc)
                    spans.append(m.span())
        if calls:
            break
    if not calls:
        for s, e, obj in _scan_json_objects(content):
            tc = _coerce_call(obj, tool_names)
            if tc:
                calls.append(tc)
                spans.append((s, e))
    if not calls:
        return [], content
    leftover = content
    for s, e in sorted(spans, reverse=True):
        leftover = leftover[:s] + leftover[e:]
    return calls, leftover.strip()


# ------------------------------------------------------------- providers

class Provider:
    name = "base"
    supports_tools = True  # flipped off if the model rejects native tools

    def __init__(self, model: str, base_url: str, api_key: str | None = None,
                 timeout: float = 600.0, options: dict | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.options = options or {}
        self.client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *,
             temperature: float = 0.3, max_tokens: int = 4096, think: bool | None = None,
             num_ctx: int | None = None, on_token=None) -> LLMResponse:
        raise NotImplementedError

    def _post(self, url: str, payload: dict, headers: dict | None = None, retries: int = 2) -> dict:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = self.client.post(url, json=payload, headers=headers or {})
                if r.status_code in (429, 500, 502, 503, 529) and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"{self.label} HTTP {r.status_code}: {r.text[:500]}")
                return r.json()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"{self.label} unreachable: {last}")


class OllamaProvider(Provider):
    """Native /api/chat — lets us set num_ctx, which the OpenAI shim ignores."""
    name = "ollama"

    def _convert(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
                out.append(msg)
            else:
                msg = {"role": role, "content": m.get("content") or ""}
                if m.get("images"):
                    msg["images"] = [_b64(p)[0] for p in m["images"]]
                out.append(msg)
        return out

    # ---------------------------------------------------------- speed
    # Measured on an RTX 3060 12 GB: a cold model load costs ~14 s and a model
    # that spills out of VRAM generates ~3x slower. So: keep the model loaded,
    # and shrink the context window until the model fits on the GPU.
    CTX_STEPS = (32768, 24576, 16384, 12288, 8192)
    _fit_cache: dict[str, int] = {}
    _fit_lock = threading.Lock()

    @property
    def keep_alive(self) -> str:
        return str(self.options.get("keep_alive", "30m")) if isinstance(self.options, dict) else "30m"

    def _gen_options(self) -> dict:
        return {k: v for k, v in (self.options or {}).items() if k != "keep_alive"}

    def loaded(self) -> dict | None:
        try:
            for m in self.client.get(f"{self.base_url}/api/ps", timeout=5).json().get("models", []):
                if m.get("name") == self.model or m.get("model") == self.model or m.get("name", "").split(":latest")[0] == self.model:
                    return m
        except Exception:
            return None
        return None

    def warm(self, num_ctx: int | None = None) -> None:
        """Load the model now (in the background at app start) so the first request is fast."""
        opts = {"num_ctx": num_ctx} if num_ctx else {}
        self.client.post(f"{self.base_url}/api/generate", json={"model": self.model, "prompt": "", "keep_alive": self.keep_alive,
                                                               "options": opts}, timeout=600)

    def fitted_ctx(self, requested: int, store: "Path | None" = None) -> int:
        """Largest context <= requested that keeps the whole model in VRAM.
        Measured once per model and remembered in fit.json."""
        key = f"{self.model}@{requested}"
        if key in self._fit_cache:
            return self._fit_cache[key]
        with self._fit_lock:
            if key in self._fit_cache:
                return self._fit_cache[key]
            saved = {}
            if store and store.exists():
                try:
                    saved = json.loads(store.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    saved = {}
            if key in saved:
                self._fit_cache[key] = int(saved[key])
                return self._fit_cache[key]
            choice = requested
            try:
                steps = [c for c in self.CTX_STEPS if c <= requested] or [requested]
                for ctx in steps:
                    self.warm(ctx)
                    m = self.loaded()
                    if not m or not m.get("size"):
                        choice = ctx
                        break
                    choice = ctx
                    if m.get("size_vram", 0) >= m["size"] * 0.98:  # fully on the GPU (or CPU-only machine: size_vram 0 everywhere)
                        break
                    if m.get("size_vram", 0) == 0:  # no GPU at all: smaller context still helps RAM, keep going
                        continue
            except Exception:
                return requested  # never block a request on the probe
            self._fit_cache[key] = choice
            if store:
                saved[key] = choice
                try:
                    store.write_text(json.dumps(saved, indent=1), encoding="utf-8")
                except OSError:
                    pass
            return choice

    def chat(self, messages, tools=None, *, temperature=0.3, max_tokens=4096, think=None, num_ctx=None, on_token=None):
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert(messages),
            "stream": bool(on_token),
            "keep_alive": self.keep_alive,
            "options": {"temperature": temperature, "num_predict": max_tokens, **self._gen_options()},
        }
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        send = self._stream if on_token else (lambda pl, cb: self._post(f"{self.base_url}/api/chat", pl))
        try:
            data = send(payload, on_token)
        except LLMError as e:
            msg = str(e).lower()
            if "think" in payload and "think" in msg:
                payload.pop("think")  # model doesn't support thinking
                data = send(payload, on_token)
            elif tools and "does not support tools" in msg:
                payload.pop("tools")  # fall back to text tool calls
                self.supports_tools = False
                data = send(payload, on_token)
            else:
                raise
        msg = data.get("message", {})
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"input": args}
            calls.append(ToolCall(id=_new_id(), name=fn.get("name", ""), arguments=args or {}))
        return LLMResponse(
            content=msg.get("content", "") or "",
            tool_calls=calls,
            thinking=msg.get("thinking", "") or "",
            usage={"prompt": data.get("prompt_eval_count"), "completion": data.get("eval_count")},
        )

    def _stream(self, payload: dict, on_token) -> dict:
        """Stream tokens to on_token as they're generated; return the same shape as a non-streamed reply."""
        content, thinking, calls, last = [], [], [], {}
        try:
            with self.client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"{self.label} HTTP {r.status_code}: {r.text[:500]}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("error"):
                        raise LLMError(f"{self.label}: {ev['error']}")
                    m = ev.get("message") or {}
                    if m.get("content"):
                        content.append(m["content"])
                        try:
                            on_token(m["content"])
                        except Exception:
                            pass
                    if m.get("thinking"):
                        thinking.append(m["thinking"])
                    calls.extend(m.get("tool_calls") or [])
                    if ev.get("done"):
                        last = ev
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise LLMError(f"{self.label} unreachable: {e}")
        out = dict(last)
        out["message"] = {"content": "".join(content), "thinking": "".join(thinking), "tool_calls": calls}
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(f"{self.base_url}/api/embed", {"model": self.model, "input": texts})
        return data.get("embeddings", [])


class OpenAICompatProvider(Provider):
    """OpenAI, OpenRouter, LM Studio, llama.cpp server, vLLM, ..."""
    name = "openai"

    def _convert(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{
                        "id": c["id"], "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    } for c in m["tool_calls"]]
                out.append(msg)
            elif m.get("images"):
                parts: list[dict] = [{"type": "text", "text": m.get("content") or ""}]
                for p in m["images"]:
                    b, mime = _b64(p)
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b}"}})
                out.append({"role": role, "content": parts})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def chat(self, messages, tools=None, *, temperature=0.3, max_tokens=4096, think=None, num_ctx=None, on_token=None):
        payload: dict[str, Any] = {
            "model": self.model, "messages": self._convert(messages),
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(f"{self.base_url}/chat/completions", payload, headers)
        choice = (data.get("choices") or [{}])[0].get("message", {})
        calls = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"input": fn.get("arguments")}
            calls.append(ToolCall(id=tc.get("id") or _new_id(), name=fn.get("name", ""), arguments=args))
        return LLMResponse(content=choice.get("content") or "", tool_calls=calls,
                           thinking=choice.get("reasoning") or "", usage=data.get("usage", {}))


class AnthropicProvider(Provider):
    name = "anthropic"

    def _convert(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts, out = [], []

        def push(role: str, blocks: list[dict]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append(m["content"])
            elif role == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"] or "(empty)"}])
            elif role == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
                push("assistant", blocks or [{"type": "text", "text": "(no content)"}])
            else:
                blocks = []
                for p in m.get("images") or []:
                    b, mime = _b64(p)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b}})
                blocks.append({"type": "text", "text": m.get("content") or "(empty)"})
                push("user", blocks)
        return "\n\n".join(system_parts), out

    def chat(self, messages, tools=None, *, temperature=0.3, max_tokens=4096, think=None, num_ctx=None, on_token=None):
        system, msgs = self._convert(messages)
        payload: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": msgs,
                                   "temperature": temperature}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{
                "name": t["function"]["name"], "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            } for t in tools]
        headers = {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}
        data = self._post(f"{self.base_url}/v1/messages", payload, headers)
        text, calls = [], []
        for b in data.get("content", []):
            if b.get("type") == "text":
                text.append(b["text"])
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {}))
        return LLMResponse(content="\n".join(text), tool_calls=calls, usage=data.get("usage", {}))


PROVIDER_CLASSES = {
    "ollama": OllamaProvider,
    "openai": OpenAICompatProvider,
    "openrouter": OpenAICompatProvider,
    "lmstudio": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}


def make_provider(spec: dict, providers_cfg: dict) -> Provider | None:
    """Build a provider from a models.<role> config entry. Returns None for a
    cloud entry with no API key (i.e. the user chose to stay local)."""
    kind = spec.get("provider", "ollama")
    from .bridges import BRIDGES
    if kind in BRIDGES:
        return BRIDGES[kind](model=spec.get("model", ""), timeout=float(spec.get("timeout", 600)),
                             reasoning=spec.get("reasoning", ""))
    cls = PROVIDER_CLASSES.get(kind)
    if not cls:
        raise LLMError(f"unknown provider '{kind}'")
    base_url = spec.get("base_url") or providers_cfg.get(kind, {}).get("base_url", "")
    api_key = None
    if spec.get("api_key_env"):
        api_key = os.environ.get(spec["api_key_env"])
        if not api_key:
            return None
    elif spec.get("api_key"):
        api_key = spec["api_key"]
    p = cls(spec["model"], base_url, api_key, options=spec.get("options"))
    p.name = kind
    return p


# What each subscription is best at. Used to send a job straight to the right
# brain instead of making you pick an app. Editable under `routing:` in config.
DEFAULT_ROUTES: dict[str, dict] = {
    "chatgpt": {"label": "code and building things",
                "words": ["code", "coding", "script", "program", "python", "javascript", "bug", "debug", "error", "fix",
                          "build", "app", "website", "api", "function", "sql", "excel formula", "spreadsheet", "math",
                          "calculate", "automate", "regex", "deploy", "github", "repo", "test"]},
    "gemini": {"label": "research and long documents",
               "words": ["research", "search", "find out", "look up", "latest", "news", "compare", "summarize",
                         "summary", "pdf", "document", "paper", "article", "youtube", "video", "transcript", "sources",
                         "review of", "best ", "price", "prices", "deals", "which is better"]},
    "claude": {"label": "writing and careful reasoning",
               "words": ["write", "rewrite", "draft", "email", "essay", "letter", "cover letter", "resume", "edit",
                         "proofread", "tone", "story", "explain", "analyze", "analysis", "plan", "strategy", "advice",
                         "review my", "feedback", "outline", "argument"]},
}


def brain_key(p: "Provider | None") -> str:
    if p is None:
        return ""
    k = getattr(p, "key", "") or getattr(p, "name", "")
    return {"anthropic": "claude", "openai": "openai-api", "openrouter": "openrouter", "ollama": "local"}.get(k, k)


QUOTA_WORDS = ("usage limit", "rate limit", "quota", "429", "too many requests", "credit", "exceeded", "limit reached")


class Router:
    """Hands out providers by role (main, vision, embed) plus the "brains":
    subscription bridges (ChatGPT, Gemini) and API-key models (Claude, ...).
    Routes each job to the brain best suited to it, and rests a brain whose
    subscription limit was hit."""

    def __init__(self, models_cfg: dict, providers_cfg: dict, brains_cfg: list | None = None,
                 routes: dict | None = None, cooldown_minutes: float = 60):
        self.models_cfg = models_cfg
        self.providers_cfg = providers_cfg
        self.brains_cfg = list(brains_cfg or [])
        if models_cfg.get("cloud") and models_cfg["cloud"] not in self.brains_cfg:
            self.brains_cfg.append(models_cfg["cloud"])
        self.routes = {k: dict(v) for k, v in DEFAULT_ROUTES.items()}
        for k, v in (routes or {}).items():
            if isinstance(v, dict):
                self.routes.setdefault(k, {}).update(v)
            elif isinstance(v, list):
                self.routes.setdefault(k, {"label": k})["words"] = v
        self.cooldown_minutes = cooldown_minutes
        self.resting: dict[str, float] = {}   # brain key -> time it's available again
        self._cache: dict[str, Provider | None] = {}
        self._chain: list[Provider] | None = None
        self._lock = threading.Lock()

    def get(self, role: str) -> Provider | None:
        if role == "cloud":
            return self.cloud
        if role not in self._cache:
            spec = self.models_cfg.get(role)
            self._cache[role] = make_provider(spec, self.providers_cfg) if spec else None
        return self._cache[role]

    @property
    def main(self) -> Provider:
        p = self.get("main")
        if p is None:
            raise LLMError("no main model configured")
        return p

    def all_brains(self, refresh: bool = False) -> list[Provider]:
        """Every configured, signed-in brain, including ones resting after hitting a limit."""
        with self._lock:
            if self._chain is None or refresh:
                chain = []
                for spec in self.brains_cfg:
                    if not spec or spec.get("enabled") is False:
                        continue
                    try:
                        p = make_provider(spec, self.providers_cfg)
                    except LLMError:
                        continue
                    if p is None:
                        continue
                    if hasattr(p, "available") and not p.available(refresh=refresh):
                        continue
                    chain.append(p)
                self._chain = chain
            return list(self._chain)

    def brain_chain(self, refresh: bool = False, prefer: str = "") -> list[Provider]:
        """Brains ready to use now, the preferred one first."""
        now = time.time()
        ready = [p for p in self.all_brains(refresh) if self.resting.get(brain_key(p), 0) <= now]
        if prefer:
            ready.sort(key=lambda p: brain_key(p) != prefer)
        return ready

    def by_key(self, key: str) -> Provider | None:
        if key in ("local", "main"):
            return self.main
        return next((p for p in self.brain_chain() if brain_key(p) == key), None)

    def rest(self, p: Provider, minutes: float | None = None) -> None:
        """Subscription limit hit: skip this brain for a while."""
        self.resting[brain_key(p)] = time.time() + 60 * (self.cooldown_minutes if minutes is None else minutes)

    @staticmethod
    def is_quota_error(err: Exception | str) -> bool:
        low = str(err).lower()
        return any(w in low for w in QUOTA_WORDS)

    def route(self, task: str) -> tuple[str, str]:
        """(brain key, plain-language reason) for the best available brain, or ("", "") if nothing fits."""
        text = " " + task.lower() + " "
        ready = {brain_key(p) for p in self.brain_chain()}
        best, score = "", 0
        for key, r in self.routes.items():
            if key not in ready:
                continue
            n = sum(1 for w in r.get("words", []) if w in text)
            if n > score:
                best, score = key, n
        if not best:
            return "", ""
        return best, self.routes[best].get("label", best)

    @property
    def cloud(self) -> Provider | None:
        chain = self.brain_chain()
        return chain[0] if chain else None

    def deep(self, prefer: str = "") -> Provider:
        """Best available model for hard work: the preferred/first brain, else main."""
        chain = self.brain_chain(prefer=prefer)
        return chain[0] if chain else self.main

    def status(self) -> list[dict]:
        now = time.time()
        return [{"key": brain_key(p), "label": p.label,
                 "resting_until": self.resting.get(brain_key(p)) if self.resting.get(brain_key(p), 0) > now else None,
                 "good_at": self.routes.get(brain_key(p), {}).get("label", "")}
                for p in self.all_brains()]
