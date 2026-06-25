from __future__ import annotations

import base64
import json
import mimetypes
import random
import time
from typing import Any, Iterator

from ..config import LLMConfig
from ..obs import get_logger
from ..content.persona import fallback_persona
from ..protocol import Notice, TextDelta, ThinkDelta, ToolEnd, ToolStart
from .attachments import shrink_data_url, shrink_image_to_inline
from .cache import apply_cache_control, cache_policy
# Transport hardening (surrogate scrub, tool-arg repair, jittered retry, the SSE
# stall watchdog) — extracted to _stream_util so this module stays the request/
# stream/tool loop + the hermes-ported reasoning/cache core.
from ._stream_util import (
    StreamStall,
    _StallGuard,
    _SurrogateJoiner,
    _parse_retry_after,
    _preflight_history_tool_calls,
    _repair_tool_args,
    _retry_delay,
    _scrub_surrogates,
    _stall_timeout_for,
)

# Auxiliary vision model — when the main model can't see, an image is described by
# the GLOBAL read-image route (session.settings.global_vision_route: its own provider
# + model, NOT this chara's route) and the text fed back.
# hermes' generic describe prompt (auxiliary task=vision interceptor), verbatim.
_VISION_DESCRIBE_PROMPT = (
    "Describe everything visible in this image in thorough detail. Include any "
    "text, code, data, objects, people, layout, colors, and any other notable "
    "visual information."
)
_VISION_MAX_BYTES = 8 * 1024 * 1024  # shrink anything larger before the aux call

_log = get_logger("llm")

LIVE_PROVIDERS = {"openai_compatible", "openai", "ollama", "openrouter"}

# Provider "image too large" rejection markers (ported from hermes
# agent/error_classifier.py). A chara inlines images at full size (hermes native
# shape); when a provider 400s on an oversized image part we reactively shrink the
# data-URL parts and retry once (see _connect_with_retry / _shrink_request_images).
_IMAGE_TOO_LARGE_MARKERS = (
    "image_too_large", "image too large", "image is too large", "image exceeds",
    "exceeds 5 mb", "exceeds the maximum size", "maximum allowed size",
    # Anthropic's per-side pixel-dimension cap (independent of byte size) — the
    # rejection wording omits "too large"/"exceeds", so list it explicitly.
    "dimensions exceed max allowed size", "max allowed size: 8000",
)


def _provider_error_message(body: str) -> str:
    """The provider's own human message, dug out of the common JSON error shapes
    ({"error":{"message":…}} / {"error":"…"} / {"message":…}). Falls back to the
    raw body. Never raises."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return (body or "").strip()
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "").strip()
        if isinstance(err, str):
            return err.strip()
        if data.get("message"):
            return str(data["message"]).strip()
    return (body or "").strip()


def _explain_http_error(code: int, body: str) -> str:
    """One honest, human line for a permanent provider HTTP error — never a raw
    JSON dump. Leads with what it MEANS / what to do, then quotes the provider's
    own message as context. (A 401 "User not found" from OpenRouter, for instance,
    means the API key is unrecognized — not that a user account is missing.)"""
    msg = _provider_error_message(body)[:200]
    if code in (401, 403):
        lead = "the model provider rejected the API key — it's invalid, revoked, or unrecognized; check the provider key in Settings"
    elif code == 402:
        lead = "the model provider reports the account is out of credit"
    elif code == 404:
        lead = "the model wasn't found — check the model id for this provider"
    elif code == 429:
        lead = "rate limited by the model provider — try again shortly"
    elif 500 <= code < 600:
        lead = "the model provider had a server error"
    else:
        lead = "the model provider returned an error"
    tail = f' (provider said: "{msg}")' if msg else ""
    return f"HTTP {code}: {lead}{tail}"


def _is_image_too_large(detail: str) -> bool:
    """True when a provider error reads as an oversized-image rejection."""
    d = (detail or "").lower()
    if any(m in d for m in _IMAGE_TOO_LARGE_MARKERS):
        return True
    return "image" in d and ("exceed" in d or "too large" in d or "maximum size" in d)


def _shrink_request_images(data: bytes) -> "bytes | None":
    """Reactively shrink oversized inline image data-URLs in an already-serialized
    request body. Returns new bytes, or None when there were no shrinkable image
    parts / nothing changed (so the caller surfaces the original error). Mirrors
    hermes try_shrink_image_parts_in_messages; remote http(s) URLs are left alone."""
    try:
        body = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return None
    changed = False
    for m in msgs:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            iv = part.get("image_url")
            if isinstance(iv, dict):
                new = shrink_data_url(iv.get("url", ""))
                if new:
                    iv["url"] = new
                    changed = True
            elif isinstance(iv, str):
                new = shrink_data_url(iv)
                if new:
                    part["image_url"] = new
                    changed = True
    if not changed:
        return None
    try:
        return json.dumps(body).encode("utf-8")
    except (TypeError, ValueError):
        return None

# All streaming generators here yield protocol events (protocol/events.py),
# never styled strings: speech is TextDelta, reasoning is ThinkDelta, tool
# activity is ToolStart/ToolEnd, retries and truncations are Notice. How each
# is drawn (dimmed, hidden behind a ✶ indicator, dropped) is the frontend's
# decision — the hermes stream_events model.


# Model families that accept OpenRouter's unified `reasoning` request param
# (hermes gates this the same way: unknown extra_body fields get forwarded
# upstream and some providers 400 on them). List copied from hermes-agent's
# reasoning_model_prefixes, plus nousresearch.
_REASONING_PREFIXES = (
    "deepseek/", "anthropic/", "openai/", "x-ai/", "google/gemini-2", "google/gemma-4",
    "qwen/qwen3", "tencent/hy3-preview", "xiaomi/", "nousresearch/",
)

# Known multimodal (image-in) model families, matched as substrings of the model
# id (provider-prefix tolerant, e.g. "openai/gpt-4o", "gpt-4o-mini"). This is a
# CAPABILITY read, not a preference — when in doubt we route the image to the
# workspace+notice path rather than risk a 400 from a text-only model.
_VISION_HINTS = (
    "gpt-4o", "gpt-4.1", "gpt-4-vision", "gpt-4-turbo", "o1", "o3", "o4-mini", "chatgpt-4o",
    "claude-3", "claude-4", "claude-opus", "claude-sonnet", "claude-haiku",
    "gemini", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qvq",
    "llava", "pixtral", "llama-3.2", "llama-4", "internvl", "minicpm-v",
    "glm-4v", "glm-4.1v", "step-1v", "yi-vision", "grok-vision", "grok-2-vision",
    "grok-4", "molmo", "phi-3.5-vision", "phi-4-multimodal", "mistral-small-3",
    "mistral-medium-3", "deepseek-vl", "kimi-vl", "ernie-4.5-vl", "doubao-vision",
)


def _base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when base_url's hostname is `domain` or a subdomain of it.

    Safer than `domain in base_url`, which false-positives on paths
    (`evil.com/moonshot.ai`) and lookalike hosts (`moonshot.ai.evil`)."""
    from urllib.parse import urlparse
    raw = (base_url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "//" + raw
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    domain = (domain or "").strip().lower().rstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _model_keeps_thought_signature(model: "str | None") -> bool:
    """Gemini-family targets attach `extra_content` (thought_signature) to each
    tool call and reject the next request with HTTP 400 if it is missing on
    replay; every other strict OpenAI-compatible provider rejects the request
    if extra_content IS present. So the field is kept only when the OUTGOING
    model is itself Gemini/Gemma family, stripped otherwise."""
    m = str(model or "").lower()
    return "gemini" in m or "gemma" in m



class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        # Real token usage from the most recent stream that carried a `usage`
        # object (audit #8, hermes context_compressor: "defers to recent real
        # API usage over known-noisy rough estimates"). The real number sees
        # the WHOLE request — stable prefix, tool schemas, volatile tail —
        # where the char heuristic sees only the history, and it is exact on
        # CJK-heavy text where the heuristic drifts. Not requested explicitly
        # (stream_options is not universal); captured when present.
        self.last_usage: "dict[str, Any] | None" = None
        self.last_prompt_tokens: int = 0
        self.usage_fresh: bool = False

    def _note_usage(self, usage: Any) -> None:
        """Record a `usage` object seen in a stream payload (typically the
        final chunk). Only positive prompt_tokens counts — providers send
        zeroed placeholders mid-stream."""
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        if isinstance(prompt, (int, float)) and prompt > 0:
            self.last_usage = dict(usage)
            self.last_prompt_tokens = int(prompt)
            self.usage_fresh = True

    def mark_usage_stale(self) -> None:
        """The window was rewritten (compaction): the captured usage no longer
        describes it. The numbers stay readable for diagnostics; only the
        compaction-trigger freshness drops."""
        self.usage_fresh = False

    def is_live(self) -> bool:
        return self.cfg.provider in LIVE_PROVIDERS and bool(self.cfg.base_url)

    # ---- reasoning-model policy (hermes-style, OpenRouter + DeepSeek focus) --------

    def reasoning_supported(self) -> bool:
        """Safe to send the unified `reasoning` request param on this route/model.

        ONLY OpenRouter understands the unified object — it is their
        normalization layer (effort → Anthropic/Gemini thinking budgets, OpenAI
        native effort, on/off for DeepSeek/Qwen3), so per-model effort quirks
        are their job, not ours (hermes trusts it the same way). Direct
        endpoints each speak their own dialect: DeepSeek native picks thinking
        by MODEL NAME (deepseek-reasoner vs deepseek-chat) and rejects unknown
        params, so direct routes get nothing. If we ever add direct routes
        with per-model effort menus (GitHub Models / LM Studio), follow
        hermes: a per-model supported-efforts table + clamping.
        """
        base = (self.cfg.base_url or "").lower()
        model = (self.cfg.model or "").lower()
        return "openrouter" in base and model.startswith(_REASONING_PREFIXES)

    def vision_supported(self) -> bool:
        """Whether the active model can SEE images sent as ``image_url`` content.

        A capability read (model-name heuristic) with an explicit `on`/`off`
        override on `cfg.vision` for routes the name can't reveal. When unknown
        we return False so an image lands in the workspace with a notice rather
        than risking a hard 400 from a text-only model (no failure fallbacks)."""
        mode = (self.cfg.vision or "auto").strip().lower()
        if mode in {"on", "true", "1", "yes"}:
            return True
        if mode in {"off", "false", "0", "no"}:
            return False
        model = (self.cfg.model or "").lower()
        return any(hint in model for hint in _VISION_HINTS)

    def reasoning_echoback_required(self) -> bool:
        """Some thinking modes reject replayed assistant tool-call messages that
        omit reasoning_content — DeepSeek V4 thinking (hermes #15250), Xiaomi
        MiMo, and Kimi/Moonshot when called on their OWN endpoints (aggregators
        like OpenRouter speak their own protocol for those models, hence the
        HOST gate, not a model-name gate). Host-matched, not substring-matched,
        so `evil.com/api.deepseek.com` can't false-trigger."""
        provider = (self.cfg.provider or "").lower()
        model = (self.cfg.model or "").lower()
        base = self.cfg.base_url or ""
        deepseek = (
            provider == "deepseek"
            or "deepseek" in model
            or _base_url_host_matches(base, "api.deepseek.com")
        )
        kimi = (
            provider in {"kimi-coding", "kimi-coding-cn"}
            or _base_url_host_matches(base, "api.kimi.com")
            or _base_url_host_matches(base, "moonshot.ai")
            or _base_url_host_matches(base, "moonshot.cn")
        )
        mimo = (
            provider == "xiaomi"
            or "mimo" in model
            or _base_url_host_matches(base, "api.xiaomimimo.com")
        )
        return deepseek or kimi or mimo

    def _reasoning_body(self, body: dict, override: "str | None" = None) -> dict:
        """Attach the unified `reasoning` request param (default ON at medium).

        Effort: off | low | medium | high — from cfg.reasoning, or `override`
        for a single call. "off" still sends an explicit
        {"enabled": false} to reasoning-capable models (some think by default);
        non-reasoning routes get nothing either way."""
        if not self.reasoning_supported():
            return body
        effort = (override or self.cfg.reasoning or "medium").strip().lower()
        if effort == "off":
            body["reasoning"] = {"enabled": False}
        else:
            if effort not in {"low", "medium", "high"}:
                effort = "medium"
            body["reasoning"] = {"enabled": True, "effort": effort}
        return body

    def _max_tokens_param(self) -> dict:
        """OpenAI's newer models on the DIRECT endpoint require
        max_completion_tokens; OpenRouter/local/older routes use max_tokens
        (copied from hermes-agent's _max_tokens_param).

        The VALUE follows the model — `providers.max_output_tokens` resolves the
        model's real output cap (OpenRouter's `max_completion_tokens`), defaulting
        to 8192, with the operator's `LLM_MAX_TOKENS` (>0) as an explicit override.
        Replaces the old flat 4096, which cut large `write_file`/`patch` tool-call
        arguments mid-argument (~12KB)."""
        from . import providers
        n = providers.max_output_tokens(
            self.cfg.provider, self.cfg.base_url, self.cfg.model, self.cfg.api_key,
            override=int(self.cfg.max_tokens or 0),
        )
        base = (self.cfg.base_url or "").lower()
        if "api.openai.com" in base or "openai.azure.com" in base:
            return {"max_completion_tokens": n}
        return {"max_tokens": n}

    # Transient failures worth retrying at the connection phase. Everything else
    # (auth errors, bad requests) surfaces immediately — a failed request is a
    # failed request; there is NO fabricated fallback output anywhere.
    _RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504, 520, 522, 524}
    _RETRY_LIMIT = 5

    def _connect_with_retry(self, url: str, data: bytes, timeout: float):
        """Open the streaming request. On transient failure: jittered
        exponential backoff (audit #5) — min(5·2^(n−1), 120)s + U(0, 0.5·delay)
        — up to 5 retries, then stop and surface the error; a 429's Retry-After
        header, when present, replaces the computed delay. Yields dim retry
        notices to the UI; returns the open response. Only the connection
        phase retries — a stream that dies mid-flight surfaces immediately (the
        interrupt-commit machinery already preserves partials)."""
        import urllib.error
        import urllib.request

        attempt = 0
        shrunk_for_image = False
        while True:
            retry_after = None
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                if e.code not in self._RETRYABLE_HTTP:
                    # hermes reactive recovery: a 400 that reads as image-too-large →
                    # shrink the inline image data-URLs once and retry (no backoff, no
                    # attempt cost). Any failure falls through to surfacing the error.
                    if e.code == 400 and not shrunk_for_image and _is_image_too_large(detail):
                        new_data = _shrink_request_images(data)
                        if new_data is not None:
                            shrunk_for_image = True
                            data = new_data
                            _log.warning("image too large — shrank inline image(s) and retrying")
                            yield Notice("retry", "⚠ image too large — shrinking and retrying")
                            continue
                    _log.info("permanent HTTP error from %s: %s %s", url, e.code, detail[:200])
                    raise RuntimeError(_explain_http_error(e.code, detail)) from e
                if e.code == 429:
                    retry_after = _parse_retry_after(getattr(e, "headers", None))
                err = f"HTTP {e.code}: {detail[:120]}"
            except urllib.error.URLError as e:
                err = f"connection failed: {e.reason}"
            except TimeoutError:
                err = "connection timed out"
            attempt += 1
            if attempt > self._RETRY_LIMIT:
                _log.error("gave up after %d retries: %s", self._RETRY_LIMIT, err)
                raise RuntimeError(f"{err} — gave up after {self._RETRY_LIMIT} retries")
            delay = _retry_delay(attempt, retry_after)
            _log.warning("connect retry %d/%d in %.1fs: %s", attempt, self._RETRY_LIMIT, delay, err)
            yield Notice("retry", f"⚠ {err} — retry {attempt}/{self._RETRY_LIMIT} in {delay:.0f}s")
            time.sleep(delay)

    def raw_complete(self, messages: list[dict[str, Any]], max_tokens: int = 1024,
                     timeout: float = 60.0, model: str = "", temperature: float = 0.3,
                     *, base_url: str = "", api_key: str = "") -> str:
        """One-off NON-streaming completion for engine-internal use (compaction
        summaries; auxiliary vision). `model` overrides the main model id. `base_url`
        + `api_key` override the ROUTE (auxiliary vision rides the GLOBAL default
        provider, not this chara's). Returns the assistant text, or "" on ANY
        failure — engine side-tasks degrade to a no-op, never crash the turn."""
        url = (base_url or self.cfg.base_url or "").rstrip("/")
        # An explicit route override is trusted; otherwise the chara's own client
        # must be live (compaction path).
        if not url or (not base_url and not self.is_live()):
            return ""
        import urllib.request

        # Side-tasks OMIT the `reasoning` param entirely (hermes parity:
        # auxiliary_client.call_llm never sets reasoning; trajectory_compressor's
        # summary calls send none) — the provider/model default applies. We do NOT
        # send reasoning:{enabled:false}: reasoning-MANDATORY routes (some vision
        # models, e.g. stepfun step-3.7) reject that with HTTP 400 "Reasoning is
        # mandatory… cannot be disabled". The main streaming path keeps its own
        # reasoning handling (apple-to-apple with hermes); only side-tasks omit it.
        body = {
            "model": (model or self.cfg.model),
            "messages": messages,
            "temperature": temperature,   # low-variance for summaries / descriptions
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/chat/completions", data=data,
            headers=self._headers_for(url, api_key if base_url else self.cfg.api_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:
            _log.warning("raw_complete failed (degrading to no-op): %s", e)
            return ""

    def describe_image(self, data: bytes, mime: str, question: str = "") -> "str | None":
        """Understand an image via the AUXILIARY vision model when the main model
        has no vision — hermes' auxiliary task=vision shape. The vision model rides
        the GLOBAL DEFAULT route (Settings · 模型 · 读图 + the default provider), NOT
        this chara's provider: image understanding needs no prompt cache, and a
        per-chara provider switch must not break it. Returns the description, "" for
        an empty completion, or None when no vision route is configured / not an
        image (the caller keeps the honest 'saved to disk' note — no fabrication)."""
        from ..session.settings import global_vision_route

        route = global_vision_route()
        vm = route.get("model", "")
        if not vm or not route.get("base_url") or not route.get("api_key"):
            return None
        if not data or not (mime or "").startswith("image/"):
            return None
        if len(data) > _VISION_MAX_BYTES:  # keep the aux call cheap / within limits
            shrunk = shrink_image_to_inline(data, mime)
            if shrunk is not None:
                data, mime = shrunk
        q = (question or "").strip()
        prompt = _VISION_DESCRIBE_PROMPT if not q else (
            "Fully describe and explain everything about this image, then answer "
            f"the following question:\n\n{q}")
        b64 = base64.b64encode(data).decode("ascii")
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}]
        out = self.raw_complete(messages, max_tokens=1500, timeout=120, model=vm,
                                temperature=0.1, base_url=route["base_url"], api_key=route["api_key"])
        return out or None

    def analyze_image(self, image_path: str, question: str = "") -> "str | None":
        """Path-based wrapper over describe_image (browser screenshots, read_file
        on an image). Reads the file, sniffs the mime, describes it. None on any
        read failure or when no vision model is configured."""
        try:
            data = open(image_path, "rb").read()
        except OSError:
            return None
        mime, _ = mimetypes.guess_type(image_path)
        return self.describe_image(data, mime or "image/png", question)

    def stream_complete(
        self, user_text: str, context: list[dict], stable: list[str], volatile: list[str],
        in_context: bool = True, reasoning: "str | None" = None, channel: str = "say",
    ) -> "Iterator[Any]":
        if self.is_live():
            yield from self._openai_compatible_stream(
                user_text, context, stable, volatile, in_context, reasoning, channel
            )
            return
        # Fake streaming for mock mode.
        text = self._mock(user_text, "", {})
        for ch in text:
            yield TextDelta(ch, channel)

    def _messages(
        self, user_text: str, context: list[dict], stable: list[str] | None = None,
        volatile: list[str] | None = None, in_context: bool = True,
    ) -> list[dict[str, Any]]:
        """Build the chat-completions message list.

        `context` is ContextBuffer.render() output: full message dicts including
        assistant tool_calls and tool results, already sanitized for the API.
        When `in_context` is True the caller has ALREADY committed user_text to
        the context (interrupt-safety: commit before streaming), so it is not
        appended again; ephemeral prompts (idle think cycles) pass False.
        """
        stable_blocks = stable if stable is not None else [fallback_persona()]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": m} for m in stable_blocks if m and m.strip()
        ]
        for msg in context:
            role = msg.get("role")
            if role not in {"user", "assistant", "system", "tool"}:
                msg = {**msg, "role": "system"}
            if msg.get("content") is None:
                # Strict OpenAI-compatible providers reject null content even on
                # tool-call messages; "" is accepted everywhere.
                msg = {**msg, "content": ""}
            if msg.get("tool_calls"):
                # Pre-flight repair over replayed history (audit #2): a bad
                # turn already in the durable context must not poison every
                # later request.
                msg = _preflight_history_tool_calls(msg)
                # A Gemini thought_signature (extra_content) captured under a
                # Gemini target is a 400 on every other strict provider — strip
                # it from replayed history when the current model isn't Gemini.
                if not _model_keeps_thought_signature(self.cfg.model):
                    if any(isinstance(tc, dict) and "extra_content" in tc for tc in msg["tool_calls"]):
                        msg = {**msg, "tool_calls": [
                            {k: v for k, v in tc.items() if k != "extra_content"} if isinstance(tc, dict) else tc
                            for tc in msg["tool_calls"]
                        ]}
            messages.append(msg)
        if not in_context:
            messages.append({"role": "user", "content": user_text})
        messages.extend(
            {"role": "system", "content": m} for m in (volatile or []) if m and m.strip()
        )
        return messages

    def _system_messages(self, blocks: list[str]) -> list[dict[str, Any]]:
        return [{"role": "system", "content": m} for m in blocks if m and m.strip()]

    def _headers(self) -> dict[str, str]:
        return self._headers_for(self.cfg.base_url, self.cfg.api_key)

    def _headers_for(self, base_url: str, api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # OpenRouter app attribution (name + icon); scoped to openrouter.ai.
        from ..config import openrouter_attribution_headers
        headers.update(openrouter_attribution_headers(base_url))
        return headers

    def test_connection(self, timeout: float = 20.0) -> tuple[bool, str]:
        """Validate endpoint + key + model with a tiny non-streaming completion.

        Returns (ok, human_readable_message). Never raises.
        """
        if not self.is_live():
            return False, f"provider '{self.cfg.provider}' is offline/mock — no endpoint to test"
        if not self.cfg.base_url:
            return False, "base_url is empty"
        import urllib.error
        import urllib.request

        body = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": "ping"}],
            **{k: 1 for k in self._max_tokens_param()},
            "stream": False,
        }
        url = f"{self.cfg.base_url}/chat/completions"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            model = payload.get("model", self.cfg.model)
            return True, f"OK — reached {self.cfg.base_url} as model '{model}'"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            return False, _explain_http_error(e.code, detail)
        except urllib.error.URLError as e:
            return False, f"connection failed: {e.reason}"
        except Exception as e:  # noqa: BLE001 - surface anything to the operator
            return False, f"error: {e}"

    def _openai_compatible_stream(
        self, user_text: str, context: list[dict], stable: list[str], volatile: list[str],
        in_context: bool = True, reasoning: "str | None" = None, channel: str = "say",
    ) -> "Iterator[Any]":
        url = f"{self.cfg.base_url}/chat/completions"
        body = self._reasoning_body({
            "model": self.cfg.model,
            "messages": self._messages(user_text, context, stable, volatile, in_context),
            "temperature": self.cfg.temperature,
            **self._max_tokens_param(),
            "stream": True,
        }, override=reasoning)
        should, native = cache_policy(self.cfg.base_url, self.cfg.model)
        if should:
            body["messages"] = apply_cache_control(body["messages"], self.cfg.cache_ttl, native)
        data = json.dumps(body).encode("utf-8")
        flow = ""  # "" | "think" | "speech" — for newline transitions around thinking
        # Lone-surrogate scrub (audit #7); joiners keep split astral pairs whole.
        speech_join, think_join = _SurrogateJoiner(), _SurrogateJoiner()
        resp = yield from self._connect_with_retry(url, data, timeout=90)
        guard = _StallGuard(resp, stall_timeout=_stall_timeout_for(len(data)))
        try:
            with resp:
                for raw in guard.lines():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._note_usage(payload.get("usage"))  # audit #8
                    # `or [{}]`: a usage-only final chunk carries "choices": []
                    # — plain [0] indexing would crash on it.
                    choices = payload.get("choices") or [{}]
                    delta = choices[0].get("delta", {})
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
                    if thinking:
                        guard.mark_payload()
                        thinking = think_join.feed(thinking)
                    if thinking:
                        # Reasoning travels as ThinkDelta; the flow-transition
                        # newlines ride in the same event type so a frontend
                        # that hides thinking leaks nothing.
                        if flow != "think":
                            if flow == "speech":
                                yield ThinkDelta("\n")
                            flow = "think"
                        yield ThinkDelta(thinking)
                    chunk = delta.get("content")
                    if chunk:
                        guard.mark_payload()
                        chunk = speech_join.feed(chunk)
                    if chunk:
                        if flow == "think":
                            yield ThinkDelta("\n")
                        flow = "speech"
                        yield TextDelta(chunk, channel)
        except StreamStall as e:
            _log.error("%s (model=%s)", e, self.cfg.model)
            yield Notice("stall", f"⚠ {e}")
            raise RuntimeError(str(e)) from None
        leftover = think_join.flush()
        if leftover:
            yield ThinkDelta(leftover)
        leftover = speech_join.flush()
        if leftover:
            yield TextDelta(leftover, channel)

    # ---- native function-calling agent loop ---------------------------------------

    # Continuation prompts, hermes-style: when the output limit cuts a response
    # or a tool call, TELL the model instead of letting it flounder — the silent
    # version is exactly the "started working, then mysteriously gave up" bug.
    _CONTINUE_NOTE = (
        "[System: your previous response was truncated by the output length limit. "
        "Continue exactly where you left off. Do not restart or repeat prior text.]"
    )
    _SPLIT_TOOLS_NOTE = (
        "[System: your tool call streamed past the output length limit and was DROPPED "
        "before execution. Do not retry the same call with the same large content — "
        "break the work into several smaller tool calls (e.g. write the file in pieces).]"
    )
    INTERRUPT_MARK = "\n[cut off mid-reply by the operator's next message]"
    # Step-budget exhaustion (audit #9, hermes turn_finalizer._handle_max_iterations):
    # a turn the loop limit cuts mid-work must say so — to the UI AND in
    # context — or the next turn treats the stop as completion ("started
    # working, then mysteriously gave up", the same bug family as silent
    # truncation).
    _MAX_STEPS_NOTE = (
        "[System: this turn's tool-step budget ({steps} steps) ran out before the work was "
        "finished. Nothing failed — the loop was stopped. Pick the unfinished work back up "
        "next turn instead of restarting it.]"
    )
    # Empty-completion policy (audit #4, hermes conversation_loop ≤3 retries):
    # a stream that ends with no text and no tool calls is an invisible
    # non-answer; silently recording assistant {content: None} violates the
    # "visible errors, no fabricated output" principle by the back door.
    _EMPTY_RETRY_LIMIT = 3

    def stream_agent(
        self, user_text, context, stable, volatile, tools, execute,
        record=None, max_steps: int = 8, in_context: bool = True,
        reasoning: "str | None" = None, channel: str = "say",
        record_volatile=None,
    ):
        """Stream a reply that may call tools (modern OpenAI-style function calling).

        Yields protocol events for the UI. `execute(tc)` runs one tool call and
        returns {"display": ..., "content": ..., "ok": ...}; results are fed back
        until the model produces a final answer.

        `record(msg)` commits each message (assistant incl. tool_calls and
        reasoning_content, tool results, system continuation notes) to the DURABLE
        context as soon as it exists — following hermes-agent's conversation
        history. If the UI abandons this generator mid-stream (operator interrupt),
        the partial turn is still committed, marked as cut off, so the model
        remembers what it was doing.
        """
        if not self.is_live():
            for ch in self._mock(user_text, "", {}):
                yield TextDelta(ch, channel)
            return
        record = record or (lambda _msg: None)
        # Keep the growing tool-loop transcript free of volatile tail messages.
        # Each API call appends a fresh copy of the volatile tail after all
        # history/tool results, so the post-history slot is literally last.
        messages = self._messages(user_text, context, stable, [], in_context=in_context)
        volatile_messages = self._system_messages(volatile)
        acc: list[str] = []  # text of the in-flight turn, readable by `finally`
        finished = False
        empty_retries = 0
        try:
            step = 0
            while step < max_steps:
                acc.clear()
                t0 = time.monotonic()
                tool_calls, thinking_text, finish, reasoning_details = yield from self._stream_turn(
                    messages + volatile_messages, tools, acc, reasoning, channel
                )
                text = "".join(acc).strip()
                acc.clear()  # committed below — must not re-commit as "interrupted"
                truncated = finish == "length"
                _log.info(
                    "turn %d/%d: model=%s finish=%s text=%d chars think=%d chars tools=%d in %.1fs",
                    step + 1, max_steps, self.cfg.model, finish or "?", len(text),
                    len(thinking_text), len(tool_calls), time.monotonic() - t0,
                )
                if truncated:
                    _log.warning("response truncated by output limit (finish=length, tools=%d)", len(tool_calls))

                if not text and not tool_calls and not truncated:
                    # An empty completion: nothing said, nothing called. Retry
                    # within a small budget (doesn't consume tool-loop steps);
                    # then surface a VISIBLE error — never a silent empty turn.
                    reasoning_only = bool(thinking_text)
                    what = (
                        "reasoning-only completion (thinking exhausted before a visible reply)"
                        if reasoning_only else
                        f"empty stream (no content, no tool calls, finish={finish or 'missing'})"
                    )
                    empty_retries += 1
                    if empty_retries <= self._EMPTY_RETRY_LIMIT:
                        _log.warning("%s — retry %d/%d (model=%s)", what, empty_retries, self._EMPTY_RETRY_LIMIT, self.cfg.model)
                        yield Notice("retry", f"⚠ {what} — retry {empty_retries}/{self._EMPTY_RETRY_LIMIT}")
                        continue
                    finished = True  # there is no partial to commit; the error IS the outcome
                    _log.error("%s after %d retries (model=%s)", what, self._EMPTY_RETRY_LIMIT, self.cfg.model)
                    raise RuntimeError(f"model returned a {what} after {self._EMPTY_RETRY_LIMIT} retries")
                empty_retries = 0
                step += 1

                # DeepSeek thinking mode requires reasoning_content echoed on
                # replayed assistant tool-call messages; everyone else gets it
                # withheld (most providers reject echoed thinking).
                echo = self.reasoning_echoback_required()

                if truncated and tool_calls:
                    # Cut mid-arguments: the JSON is unusable. Drop the calls and
                    # tell the model to split the work (hermes pattern).
                    a_msg: dict[str, Any] = {"role": "assistant", "content": text or "(oversized tool call dropped)"}
                    if thinking_text:
                        a_msg["reasoning_content"] = thinking_text
                    if reasoning_details:
                        a_msg["reasoning_details"] = reasoning_details
                    record(a_msg)
                    if echo:
                        # Echo-required thinking modes 400 on a replayed assistant
                        # tool-call turn that omits reasoning_content; a single
                        # space satisfies the non-empty check without fabricating.
                        replay = dict(a_msg)
                        if not isinstance(replay.get("reasoning_content"), str) or replay.get("reasoning_content") == "":
                            replay["reasoning_content"] = " "
                        messages.append(replay)
                    else:
                        messages.append({k: v for k, v in a_msg.items() if k != "reasoning_content"})
                    note = {"role": "system", "content": self._SPLIT_TOOLS_NOTE}
                    record(note)
                    messages.append(note)
                    yield Notice("truncation", "⚠ tool call truncated by the output limit — asking for smaller pieces")
                    continue

                a_msg = {"role": "assistant", "content": text or None}
                if tool_calls:
                    a_msg["tool_calls"] = tool_calls
                if thinking_text:
                    # Always kept for the record/transcript; replayed to the API
                    # only when the provider demands it (echo above).
                    a_msg["reasoning_content"] = thinking_text
                if reasoning_details:
                    # Opaque continuity blocks (signature / encrypted thinking);
                    # replayed unmodified on BOTH echo and non-echo paths.
                    a_msg["reasoning_details"] = reasoning_details
                record(a_msg)
                if echo:
                    replay = dict(a_msg)
                    if tool_calls and not isinstance(replay.get("reasoning_content"), str):
                        replay["reasoning_content"] = " "
                    elif replay.get("reasoning_content") == "":
                        replay["reasoning_content"] = " "
                    messages.append(replay)
                else:
                    messages.append({k: v for k, v in a_msg.items() if k != "reasoning_content"})

                if tool_calls:
                    img_followups = []
                    for i, tc in enumerate(tool_calls):
                        fn = tc.get("function", {})
                        name = fn.get("name", "?")
                        # Carry more of the call's arguments so the technical tool
                        # view can show a useful slice (e.g. a generate_image prompt);
                        # 80 chars was almost all JSON wrapper. Still bounded.
                        yield ToolStart(name, preview=str(fn.get("arguments") or "")[:240], index=i)
                        tool_t0 = time.monotonic()
                        res = execute(tc)
                        yield ToolEnd(
                            name, ok=bool(res.get("ok", True)),
                            duration=time.monotonic() - tool_t0,
                            summary=str(res.get("display") or ""), index=i,
                        )
                        if res.get("say"):
                            # A tool surfaced words ADDRESSED TO THE USER (the
                            # speak tool): always the say channel — every
                            # frontend delivers it, whatever this turn's channel.
                            # superchat=True marks it as the deliberate "reach the
                            # user" speak (vs ordinary reply prose) so a delivery
                            # edge can broadcast it to every gateway in any turn.
                            yield TextDelta(str(res["say"]) + "\n", "say", superchat=True)
                        # Files are no longer surfaced by a tool: the chara puts a
                        # file in front of the user by writing a `MEDIA:<path>` line
                        # in its reply, which the agent extracts (see agent._media_filter).
                        t_msg = {"role": "tool", "tool_call_id": tc.get("id") or "", "content": res.get("content", "")}
                        record(t_msg)
                        messages.append(t_msg)
                        follow = res.get("follow_up")
                        if isinstance(follow, dict) and follow.get("content"):
                            img_followups.append(follow)
                    # A tool surfaced an image for the model to SEE (read_file on an
                    # image, vision models only): inject it as a user message AFTER
                    # every tool result, so the tool replies for this assistant turn
                    # stay contiguous and the image_url rides a user message.
                    # The image rides the LIVE context across turns (record_volatile →
                    # context.messages, so the chara can re-examine the most recent
                    # image) but is NOT written to the durable transcript — bytes in
                    # immutable history is exactly what to avoid. A per-turn pass keeps
                    # only the newest image's pixels and collapses older ones to a text
                    # handle (compaction.strip_old_images, hermes _strip_historical_media).
                    # `messages.append` covers THIS turn's request; record_volatile
                    # carries it into the next turn's context view.
                    for follow in img_followups:
                        if record_volatile is not None:
                            record_volatile(follow)
                        messages.append(follow)
                    continue

                if truncated:
                    note = {"role": "system", "content": self._CONTINUE_NOTE}
                    record(note)
                    messages.append(note)
                    yield TextDelta("\n", channel)
                    continue

                finished = True
                return
            # Step budget exhausted mid-work (audit #9): NEVER a silent stop.
            # The UI gets a Notice; the durable context gets an explicit marker
            # so the next turn knows the loop was cut, not completed. Everything
            # up to here is already recorded.
            _log.warning("step budget exhausted (%d steps, model=%s) — turn stopped mid-work",
                         max_steps, self.cfg.model)
            note = {"role": "system", "content": self._MAX_STEPS_NOTE.format(steps=max_steps)}
            record(note)
            yield Notice("budget", f"⚠ step budget exhausted ({max_steps} tool steps) — stopping here; the work is unfinished")
            finished = True
        finally:
            if not finished:
                partial = "".join(acc).strip()
                if partial:
                    # Operator interrupt mid-stream: keep the partial turn so the
                    # model remembers it was halfway through something.
                    _log.info("stream abandoned mid-turn; committed %d partial chars", len(partial))
                    record({"role": "assistant", "content": partial + self.INTERRUPT_MARK})

    def _stream_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, text_out: list[str], reasoning: "str | None" = None, channel: str = "say"):
        """Stream one assistant turn. Yields protocol events; accumulates visible
        text into `text_out` (caller-owned, so an abandoned generator can still
        read the partial). Returns
        (tool_calls, reasoning, finish_reason, reasoning_details).
        """
        body: dict[str, Any] = self._reasoning_body({
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            **self._max_tokens_param(),
            "stream": True,
        }, override=reasoning)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        should, native = cache_policy(self.cfg.base_url, self.cfg.model)
        if should:
            body["messages"] = apply_cache_control(body["messages"], self.cfg.cache_ttl, native)
        data = json.dumps(body).encode("utf-8")
        _log.debug("request: model=%s messages=%d tools=%d body=%d bytes",
                   self.cfg.model, len(messages), len(tools or []), len(data))
        acc: dict[int, dict[str, Any]] = {}
        reasoning_parts: list[str] = []
        reasoning_details: list[Any] = []   # provider continuity blocks (signature / encrypted), replayed unmodified
        finish_reason = ""
        flow = ""  # "" | "think" | "speech" — for newline transitions around thinking
        # Lone-surrogate scrub (audit #7) on everything emitted/accumulated;
        # joiners keep astral pairs split across deltas whole.
        speech_join, think_join = _SurrogateJoiner(), _SurrogateJoiner()
        resp = yield from self._connect_with_retry(f"{self.cfg.base_url}/chat/completions", data, timeout=120)
        guard = _StallGuard(resp, stall_timeout=_stall_timeout_for(len(data)))
        try:
            with resp:
                for raw in guard.lines():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._note_usage(payload.get("usage"))  # audit #8
                    # `or [{}]`: a usage-only final chunk carries "choices": []
                    # — plain [0] indexing would crash on it.
                    choice = (payload.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                        guard.mark_payload()
                    delta = choice.get("delta", {})
                    # Reasoning-model thinking (DeepSeek-style `reasoning_content`,
                    # OpenRouter's `reasoning`): captured for the record, not shown
                    # as character speech and never replayed to the API.
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
                    if thinking:
                        guard.mark_payload()
                        thinking = think_join.feed(thinking)
                    if thinking:
                        reasoning_parts.append(thinking)
                        # ThinkDelta: hidden by default behind the "✶ thinking…"
                        # indicator; /thinking on reveals it dimmed. The flow
                        # newlines travel as ThinkDelta too, so nothing leaks
                        # when a frontend hides thinking.
                        if flow != "think":
                            if flow == "speech":
                                yield ThinkDelta("\n")
                            flow = "think"
                        yield ThinkDelta(thinking)
                    rd = delta.get("reasoning_details")
                    if isinstance(rd, list) and rd:
                        guard.mark_payload()
                        reasoning_details.extend(rd)
                    chunk = delta.get("content")
                    if chunk:
                        guard.mark_payload()
                        chunk = speech_join.feed(chunk)
                    if chunk:
                        if flow == "think":
                            yield ThinkDelta("\n")
                        flow = "speech"
                        text_out.append(chunk)
                        yield TextDelta(chunk, channel)
                    for tcd in delta.get("tool_calls") or []:
                        guard.mark_payload()
                        idx = tcd.get("index", 0)
                        slot = acc.setdefault(idx, {"id": "", "name": "", "args": "", "extra": None})
                        if tcd.get("id"):
                            slot["id"] = tcd["id"]
                        if tcd.get("extra_content") is not None:
                            # Gemini thought_signature rides here; kept only for a
                            # Gemini-family target (see materialization below).
                            slot["extra"] = tcd["extra_content"]
                        fn = tcd.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except StreamStall as e:
            _log.error("%s (model=%s)", e, self.cfg.model)
            yield Notice("stall", f"⚠ {e}")
            raise RuntimeError(str(e)) from None
        # A stream ending on a held-back high surrogate: release it scrubbed so
        # nothing un-sanitized is lost from the accumulated turn.
        leftover = think_join.flush()
        if leftover:
            reasoning_parts.append(leftover)
            yield ThinkDelta(leftover)
        leftover = speech_join.flush()
        if leftover:
            text_out.append(leftover)
            yield TextDelta(leftover, channel)
        tool_calls: list[dict[str, Any]] = []
        keep_sig = _model_keeps_thought_signature(self.cfg.model)
        for idx in sorted(acc):
            s = acc[idx]
            if s["name"]:
                tc: dict[str, Any] = {
                    "id": s["id"] or f"call_{idx}",
                    "type": "function",
                    # Scrub surrogates (audit #7 — args are replayed into later
                    # requests and json.dumps'd by adapters), then repair at
                    # stream end (audit #2): trailing commas, unclosed braces,
                    # raw control chars — fixable args must not become a
                    # missing-args error, and only clean args enter history.
                    "function": {"name": s["name"],
                                 "arguments": _repair_tool_args(_scrub_surrogates(s["args"]), s["name"])},
                }
                if keep_sig and s.get("extra") is not None:
                    tc["extra_content"] = s["extra"]
                tool_calls.append(tc)
        return tool_calls, "".join(reasoning_parts), finish_reason, reasoning_details

    def _mock(self, user_text: str, memory: str, status: dict[str, Any]) -> str:
        # Persona-neutral offline engine: keeps the app usable without an API. Real
        # character voice comes from the configured card + a live model, not from here.
        lower = user_text.lower()
        if not user_text.strip():
            # An empty user message = unattended time (see rules) — idle output.
            return random.choice([
                "[mock] internal loop tick. buffer stable.",
                "[mock] recall check: " + (memory[:60] or "EMPTY"),
                "[mock] idle cycle complete.",
            ])
        if "memory" in lower or "记忆" in user_text:
            return f"[mock] loaded memory:\n{memory or '(empty)'}"
        if "status" in lower or "状态" in user_text:
            return f"[mock] isolation={status.get('isolation', 'sandbox')} network={'on' if status.get('network_access') else 'off'}"
        return random.choice([
            "[mock] offline engine. Configure an API in the welcome screen for a real reply.",
            "[mock] logged.",
            "[mock] no live backend; this is a placeholder response.",
        ])
