from __future__ import annotations

import json
import random
import time
from typing import Any, Iterator

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


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

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
    _RETRY_DELAY = 5.0

    def _connect_with_retry(self, url: str, data: bytes, timeout: float):
        """Open the streaming request, Claude-Code style on failure: a flat 5s
        pause, up to 5 retries, then stop and surface the error. Yields dim
        retry notices to the UI; returns the open response. Only the connection
        phase retries — a stream that dies mid-flight surfaces immediately (the
        interrupt-commit machinery already preserves partials)."""
        import urllib.error
        import urllib.request

        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                if e.code not in self._RETRYABLE_HTTP:
                    _log.info("permanent HTTP error from %s: %s %s", url, e.code, detail[:200])
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from e
                err = f"HTTP {e.code}: {detail[:120]}"
            except urllib.error.URLError as e:
                err = f"connection failed: {e.reason}"
            except TimeoutError:
                err = "connection timed out"
            attempt += 1
            if attempt > self._RETRY_LIMIT:
                _log.error("gave up after %d retries: %s", self._RETRY_LIMIT, err)
                raise RuntimeError(f"{err} — gave up after {self._RETRY_LIMIT} retries")
            _log.warning("connect retry %d/%d: %s", attempt, self._RETRY_LIMIT, err)
            yield Notice("retry", f"⚠ {err} — retry {attempt}/{self._RETRY_LIMIT} in {int(self._RETRY_DELAY)}s")
            time.sleep(self._RETRY_DELAY)

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
        resp = yield from self._connect_with_retry(url, data, timeout=90)
        with resp:
                for raw in resp:
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
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
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
                        if flow == "think":
                            yield ThinkDelta("\n")
                        flow = "speech"
                        yield TextDelta(chunk, channel)

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
        try:
            for step in range(max_steps):
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
            finished = True  # step budget exhausted; everything so far is recorded
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
        resp = yield from self._connect_with_retry(f"{self.cfg.base_url}/chat/completions", data, timeout=120)
        with resp:
                for raw in resp:
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
                    choice = payload.get("choices", [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta", {})
                    # Reasoning-model thinking (DeepSeek-style `reasoning_content`,
                    # OpenRouter's `reasoning`): captured for the record, not shown
                    # as character speech and never replayed to the API.
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
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
                        if flow == "think":
                            yield ThinkDelta("\n")
                        flow = "speech"
                        text_out.append(chunk)
                        yield TextDelta(chunk, channel)
                    for tcd in delta.get("tool_calls") or []:
                        idx = tcd.get("index", 0)
                        slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tcd.get("id"):
                            slot["id"] = tcd["id"]
                        fn = tcd.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(acc):
            s = acc[idx]
            if s["name"]:
                tool_calls.append({
                    "id": s["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": s["name"], "arguments": s["args"] or "{}"},
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
