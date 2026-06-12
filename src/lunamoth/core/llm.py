from __future__ import annotations

import json
import queue
import random
import re
import threading
import time
from typing import Any, Iterator, NoReturn

from ..config import LLMConfig
from ..obs import get_logger
from ..content.persona import fallback_persona
from ..protocol import Notice, TextDelta, ThinkDelta, ToolEnd, ToolStart

_log = get_logger("llm")

LIVE_PROVIDERS = {"openai_compatible", "openai", "ollama", "openrouter"}

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


# ---- lone-surrogate sanitization (audit #7, hermes message_sanitization) ---------------
# Lone surrogate code points (U+D800–DFFF) are invalid outside UTF-16 pairs.
# Byte-level models (Ollama Kimi/GLM/Qwen — the hermes scar) emit them as
# unpaired \udXXX JSON escapes; json.loads happily materializes them, and any
# later ensure_ascii=False json.dumps (the protocol codec, messaging adapters)
# raises UnicodeEncodeError at .encode("utf-8"). Scrub to U+FFFD before model
# text/reasoning/args enter the context or the live event stream.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_SURROGATE_PAIR_RE = re.compile(r"[\ud800-\udbff][\udc00-\udfff]")


def _combine_pair(m: "re.Match[str]") -> str:
    s = m.group(0)
    return chr(0x10000 + ((ord(s[0]) - 0xD800) << 10) + (ord(s[1]) - 0xDC00))


def _scrub_surrogates(text: str) -> str:
    """Replace lone surrogates with U+FFFD; fast no-op when there are none.

    Adjacent high+low surrogates are first recombined into the real astral
    char: json.loads combines a \\uD83D\\uDE00 escape pair inside ONE delta,
    but halves rejoined across delta boundaries (_SurrogateJoiner) arrive as
    two raw code points that Python never auto-combines."""
    if not _SURROGATE_RE.search(text):
        return text
    text = _SURROGATE_PAIR_RE.sub(_combine_pair, text)
    return _SURROGATE_RE.sub("�", text)


class _SurrogateJoiner:
    """Per-delta surrogate scrubbing that does NOT destroy real astral chars.

    Providers can split one emoji's \\ud83d\\ude00 escape pair across two SSE
    deltas; scrubbing each delta alone would turn that legal pair into two
    U+FFFD. So a trailing HIGH surrogate is held back and rejoined with the
    next chunk — everything emitted is surrogate-free (safe for the
    ensure_ascii=False codec on the live event path) while split pairs come
    out whole. flush() releases a dangling held char at stream end.
    """

    def __init__(self) -> None:
        self._held = ""

    def feed(self, text: str) -> str:
        text = self._held + text
        self._held = ""
        if text and "\ud800" <= text[-1] <= "\udbff":
            self._held = text[-1]
            text = text[:-1]
        return _scrub_surrogates(text)

    def flush(self) -> str:
        held, self._held = self._held, ""
        return _scrub_surrogates(held)


# ---- tool-call argument repair (audit #2, hermes message_sanitization) -----------------
# Local/aggregated models (GLM via Ollama, hermes #12068) emit truncated JSON,
# trailing commas, Python None, literal control chars. Before this, a parse
# failure became `{}` → the gateway's missing-required-args error — a fair
# model-visible error, but REPAIRABLE calls were needlessly failed, and broken
# args persisted into history were replayed broken on every later request.


def _escape_control_chars_in_strings(raw: str) -> str:
    """Escape unescaped control chars (0x00-0x1F) inside JSON string values;
    pass-through outside strings (hermes _escape_invalid_chars_in_json_strings,
    #12093 — catches what strict=False alone can't when other malformations
    are present too)."""
    out: list[str] = []
    in_string = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def _repair_tool_args(raw_args: str, tool_name: str = "?") -> str:
    """Repair malformed tool_call argument JSON (hermes
    message_sanitization._repair_tool_call_arguments, four passes).

    Already-valid JSON returns BYTE-IDENTICAL (no re-serialization) so replayed
    history stays byte-stable for prompt caching. Then: (0) strict=False
    reparse + re-serialize (literal control chars — the most common local-model
    case); (1) strip trailing commas, close unclosed braces/brackets; (2) pop
    excess closers, bounded; (3) escape raw control chars inside strings. Last
    resort "{}": the gateway then reports the real missing-args failure to the
    model — an honest error, far better than a crashed turn (the GLM-via-Ollama
    scar). Every repair is logged at WARNING.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""
    if not raw_stripped:
        return "{}"
    try:
        json.loads(raw_stripped)
        return raw_stripped  # valid — keep the model's exact bytes
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    if raw_stripped == "None":  # Python-literal None
        _log.warning("repaired Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Pass 0: strict=False accepts literal control chars inside strings and
    # lets us re-serialize into wire-valid JSON without string surgery.
    try:
        parsed = json.loads(raw_stripped, strict=False)
        fixed = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        _log.warning("repaired unescaped control chars in tool_call arguments for %s", tool_name)
        return fixed
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fixed = raw_stripped
    # Pass 1: strip trailing commas (before a closer, or dangling at the cut
    # point of a truncated stream), then close unclosed structures. Hermes
    # counts braces and appends `}` before `]`, which mis-orders the closers
    # for `{"a": [1, 2`; a string-aware stack walk closes in nesting order
    # and also terminates an unclosed string.
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = re.sub(r",\s*$", "", fixed)
    stack: list[str] = []
    in_string = False
    i = 0
    while i < len(fixed):
        ch = fixed[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack and stack[-1] == ("{" if ch == "}" else "["):
            stack.pop()
        i += 1
    if in_string:
        fixed += '"'
    for opener in reversed(stack):
        fixed += "}" if opener == "{" else "]"
    # Pass 2: pop excess closing braces/brackets (bounded).
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
                fixed = fixed[:-1]
            elif fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
            else:
                break
    try:
        json.loads(fixed)
        _log.warning("repaired malformed tool_call arguments for %s: %s -> %s",
                     tool_name, raw_stripped[:80], fixed[:80])
        return fixed
    except json.JSONDecodeError:
        pass

    # Pass 3: escape raw control chars inside strings, then retry — catches
    # control chars combined with the structural damage passes 1-2 fixed.
    try:
        escaped = _escape_control_chars_in_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            _log.warning("repaired control-char-laced tool_call arguments for %s: %s -> %s",
                         tool_name, raw_stripped[:80], escaped[:80])
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    _log.warning("unrepairable tool_call arguments for %s — replaced with {} (was: %s)",
                 tool_name, raw_stripped[:80])
    return "{}"


def _preflight_history_tool_calls(msg: dict) -> dict:
    """Repair unparseable tool_call arguments on a REPLAYED history message
    (hermes conversation_loop pre-flight): one bad turn persisted to the
    transcript must not be replayed broken on every later request forever.
    Valid arguments pass through byte-identical; the durable history is never
    mutated (copy-on-repair, this is a per-request view only)."""
    tcs = msg.get("tool_calls")
    if not tcs:
        return msg
    fixed: "list[Any] | None" = None
    for i, tc in enumerate(tcs):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            json.loads(raw)
            continue
        except (json.JSONDecodeError, ValueError):
            pass
        if fixed is None:
            fixed = list(tcs)
        fixed[i] = {**tc, "function": {**fn, "arguments": _repair_tool_args(raw, str(fn.get("name") or "?"))}}
    if fixed is None:
        return msg
    return {**msg, "tool_calls": fixed}


# ---- jittered retry backoff (audit #5, hermes retry_utils) ------------------------------
# A flat 5s×5 burned the whole retry budget inside a single 60s provider rate
# window. Exponential + jitter spreads the five attempts over ~2.5 minutes and
# decorrelates concurrent charas retrying against the same provider. The retry
# COUNT and the no-fallback policy are untouched: after the budget, the error
# is VISIBLE — never papered over.
_RETRY_BASE_DELAY = 5.0
_RETRY_MAX_DELAY = 120.0
_RETRY_JITTER_RATIO = 0.5


def _retry_delay(attempt: int, retry_after: "float | None" = None) -> float:
    """min(base·2^(n−1), 120) + U(0, 0.5·delay), hermes jittered_backoff.

    A provider-sent Retry-After wins outright — it is the provider's own
    schedule, no jitter needed — but is capped at the same 120s so a hostile
    header can't wedge an unattended cycle."""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, _RETRY_MAX_DELAY)
    delay = min(_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1)), _RETRY_MAX_DELAY)
    return delay + random.uniform(0.0, _RETRY_JITTER_RATIO * delay)


def _parse_retry_after(headers) -> "float | None":
    """Retry-After from a 429: delta-seconds or an HTTP-date. None when absent
    or unparseable — the jittered backoff is then the schedule."""
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if not value:
        return None
    s = str(value).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(s).timestamp() - time.time())
    except Exception:
        return None


# ---- stream stall watchdog (audit #1, hermes chat_completion_helpers) ------------------
# SSE keep-alives defeat socket read timeouts: the socket sees traffic while no
# real chunk arrives. So the watchdog is a PAYLOAD-level wall clock — only
# content/reasoning/tool-call deltas (and finish_reason) reset it. The stall
# budget scales with the prompt: reasoning models legitimately pause for
# minutes mid-stream while thinking over large contexts (hermes scales 180 →
# 240 s above 50k tokens → 300 s above 100k). Plus a first-byte deadline for
# endpoints that accept the connection and never emit one event.
_FIRST_BYTE_TIMEOUT = 60.0
_STALL_TIMEOUT = 180.0
_STALL_TIMEOUT_50K = 240.0
_STALL_TIMEOUT_100K = 300.0


def _stall_timeout_for(body_bytes: int) -> float:
    est_tokens = body_bytes // 4  # rough chars→tokens; only a threshold pick
    if est_tokens > 100_000:
        return _STALL_TIMEOUT_100K
    if est_tokens > 50_000:
        return _STALL_TIMEOUT_50K
    return _STALL_TIMEOUT


class StreamStall(RuntimeError):
    """The provider stopped sending payload mid-stream; the request was aborted."""


class _StallGuard:
    """Wall-clock watchdog around a urllib streaming response.

    urllib has no per-read timeout we can trust here (keep-alive bytes reset
    it), so a daemon reader thread feeds raw lines into a queue and `lines()`
    waits on the queue with a deadline anchored at the last PAYLOAD chunk —
    the consumer calls `mark_payload()` when it sees one. On expiry the
    response is closed and StreamStall raised: a visible error, never a hang.
    """

    _EOF = object()

    def __init__(self, resp, first_byte_timeout: "float | None" = None, stall_timeout: "float | None" = None):
        self._resp = resp
        self._first_byte = _FIRST_BYTE_TIMEOUT if first_byte_timeout is None else first_byte_timeout
        self._stall = _STALL_TIMEOUT if stall_timeout is None else stall_timeout
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._mark = time.monotonic()
        self._got_first = False
        threading.Thread(target=self._read, name="lunamoth-stream-reader", daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self._resp:
                self._q.put(raw)
        except Exception as e:  # socket death, close() during a stall abort
            self._q.put(e)
            return
        self._q.put(self._EOF)

    def mark_payload(self) -> None:
        self._mark = time.monotonic()

    def lines(self) -> "Iterator[bytes]":
        while True:
            limit = self._stall if self._got_first else self._first_byte
            remaining = self._mark + limit - time.monotonic()
            if remaining <= 0:
                self._abort(limit)
            try:
                item = self._q.get(timeout=remaining)
            except queue.Empty:
                self._abort(limit)
            if item is self._EOF:
                return
            if isinstance(item, Exception):
                raise RuntimeError(f"stream read failed: {item}") from item
            if not self._got_first:
                self._got_first = True
                self._mark = time.monotonic()  # first byte arrived; the stall clock starts here
            yield item

    def _abort(self, limit: float) -> NoReturn:
        try:
            self._resp.close()
        except Exception:
            pass
        what = "no stream data at all" if not self._got_first else "no payload chunk"
        raise StreamStall(f"stream stalled — {what} for {limit:.0f}s; connection aborted")


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

    def reasoning_echoback_required(self) -> bool:
        """Some thinking modes reject replayed assistant tool-call messages that
        omit reasoning_content — DeepSeek (hermes #15250), Xiaomi MiMo, and
        Kimi/Moonshot when called on their own endpoints (aggregators like
        OpenRouter speak their own protocol for Kimi, hence the host gate)."""
        model = (self.cfg.model or "").lower()
        base = (self.cfg.base_url or "").lower()
        return (
            "deepseek" in model
            or "api.deepseek.com" in base
            or "mimo" in model
            or any(h in base for h in ("api.kimi.com", "moonshot.ai", "moonshot.cn"))
        )

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
        (copied from hermes-agent's _max_tokens_param)."""
        base = (self.cfg.base_url or "").lower()
        if "api.openai.com" in base or "openai.azure.com" in base:
            return {"max_completion_tokens": self.cfg.max_tokens}
        return {"max_tokens": self.cfg.max_tokens}

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
        while True:
            retry_after = None
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                if e.code not in self._RETRYABLE_HTTP:
                    _log.info("permanent HTTP error from %s: %s %s", url, e.code, detail[:200])
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from e
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

    def raw_complete(self, messages: list[dict[str, Any]], max_tokens: int = 1024, timeout: float = 60.0) -> str:
        """One-off NON-streaming completion for engine-internal use (context
        compaction summaries). Returns the assistant text, or "" on ANY failure —
        compaction must degrade to a no-op, never crash or block the turn."""
        if not self.is_live():
            return ""
        import urllib.request

        body = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": 0.3,           # factual, low-variance summaries
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        body = self._reasoning_body(body, override="off")  # summaries don't need thinking
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.base_url}/chat/completions", data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception as e:
            _log.warning("raw_complete failed (degrading to no-op): %s", e)
            return ""

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
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        # OpenRouter recommends these; harmless elsewhere.
        if "openrouter.ai" in self.cfg.base_url:
            headers["HTTP-Referer"] = "https://github.com/Lunamos/LunaMoth"
            headers["X-Title"] = "LunaMoth"
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
            return False, f"HTTP {e.code}: {detail}"
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
                tool_calls, thinking_text, finish = yield from self._stream_turn(
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
                    record(a_msg)
                    messages.append(a_msg if echo else {k: v for k, v in a_msg.items() if k != "reasoning_content"})
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
                record(a_msg)
                messages.append(a_msg if echo else {k: v for k, v in a_msg.items() if k != "reasoning_content"})

                if tool_calls:
                    for i, tc in enumerate(tool_calls):
                        fn = tc.get("function", {})
                        name = fn.get("name", "?")
                        yield ToolStart(name, preview=str(fn.get("arguments") or "")[:80], index=i)
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
                            yield TextDelta(str(res["say"]) + "\n", "say")
                        t_msg = {"role": "tool", "tool_call_id": tc.get("id") or "", "content": res.get("content", "")}
                        record(t_msg)
                        messages.append(t_msg)
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
        read the partial). Returns (tool_calls, reasoning, finish_reason).
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
        data = json.dumps(body).encode("utf-8")
        _log.debug("request: model=%s messages=%d tools=%d body=%d bytes",
                   self.cfg.model, len(messages), len(tools or []), len(data))
        acc: dict[int, dict[str, str]] = {}
        reasoning_parts: list[str] = []
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
                        slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tcd.get("id"):
                            slot["id"] = tcd["id"]
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
        for idx in sorted(acc):
            s = acc[idx]
            if s["name"]:
                tool_calls.append({
                    "id": s["id"] or f"call_{idx}",
                    "type": "function",
                    # Scrub surrogates (audit #7 — args are replayed into later
                    # requests and json.dumps'd by adapters), then repair at
                    # stream end (audit #2): trailing commas, unclosed braces,
                    # raw control chars — fixable args must not become a
                    # missing-args error, and only clean args enter history.
                    "function": {"name": s["name"],
                                 "arguments": _repair_tool_args(_scrub_surrogates(s["args"]), s["name"])},
                })
        return tool_calls, "".join(reasoning_parts), finish_reason

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
