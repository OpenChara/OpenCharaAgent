from __future__ import annotations

import json
import random
import re
from typing import Any, Callable, Iterator

from .config import LLMConfig
from .persona import fallback_persona


LIVE_PROVIDERS = {"openai_compatible", "openai", "ollama", "openrouter"}

# In-band style markers for "machinery" output (reasoning, tool activity):
# UIs render the wrapped span dimmed, the way hermes / Claude Code grey out
# everything that is not character speech. Each yielded chunk carries balanced
# markers, and strip_dim() removes the spans before anything is committed to
# the conversation context.
DIM_ON = "\x01"
DIM_OFF = "\x02"
_DIM_SPAN = re.compile("\x01.*?\x02", re.S)


def dim(text: str) -> str:
    return f"{DIM_ON}{text}{DIM_OFF}"


def strip_dim(text: str) -> str:
    """Remove dim spans (reasoning/tool chatter) — what remains is speech."""
    return _DIM_SPAN.sub("", text).replace(DIM_ON, "").replace(DIM_OFF, "")


# Model families that accept OpenRouter's unified `reasoning` request param
# (hermes gates this the same way: unknown extra_body fields get forwarded
# upstream and some providers 400 on them).
_REASONING_PREFIXES = (
    "deepseek/", "anthropic/", "openai/", "x-ai/", "google/gemini", "qwen/qwen3", "nousresearch/",
)


class LLMClient:
    def __init__(self, cfg: LLMConfig, system_provider: "Callable[[str], list[str]] | None" = None):
        self.cfg = cfg
        # When set, builds the system messages (persona + tools + status/memory + world info).
        # Lets the agent drive persona from a SillyTavern card instead of the legacy files.
        self.system_provider = system_provider

    def is_live(self) -> bool:
        return self.cfg.provider in LIVE_PROVIDERS and bool(self.cfg.base_url)

    # ---- reasoning-model policy (hermes-style, OpenRouter + DeepSeek focus) --------

    def reasoning_supported(self) -> bool:
        """Safe to send the `reasoning` request param on this route/model."""
        base = (self.cfg.base_url or "").lower()
        model = (self.cfg.model or "").lower()
        if "openrouter" in base:
            return model.startswith(_REASONING_PREFIXES)
        return "deepseek" in model or "api.deepseek.com" in base

    def reasoning_echoback_required(self) -> bool:
        """DeepSeek thinking mode rejects replayed assistant tool-call messages
        that omit reasoning_content (hermes #15250) — for those models we echo
        the thinking back; for everyone else it is withheld."""
        return "deepseek" in (self.cfg.model or "").lower() or "api.deepseek.com" in (self.cfg.base_url or "").lower()

    def _reasoning_body(self, body: dict) -> dict:
        if self.reasoning_supported():
            body["reasoning"] = {"enabled": True, "effort": "medium"}
        return body

    def stream_complete(
        self, user_text: str, memory: str, status: dict[str, Any], context: list[dict],
        in_context: bool = True,
    ) -> Iterator[str]:
        if self.is_live():
            yield from self._openai_compatible_stream(user_text, memory, status, context, in_context)
            return
        # Fake streaming for mock mode.
        text = self._mock(user_text, memory, status)
        for ch in text:
            yield ch

    def _messages(
        self, user_text: str, memory: str, status: dict[str, Any], context: list[dict],
        in_context: bool = True,
    ) -> list[dict[str, Any]]:
        """Build the chat-completions message list.

        `context` is ContextBuffer.render() output: full message dicts including
        assistant tool_calls and tool results, already sanitized for the API.
        When `in_context` is True the caller has ALREADY committed user_text to
        the context (interrupt-safety: commit before streaming), so it is not
        appended again; ephemeral prompts (idle think cycles) pass False.
        """
        if self.system_provider is not None:
            scan_text = "\n".join(str(m.get("content") or "") for m in context) + "\n" + user_text
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": m} for m in self.system_provider(scan_text) if m and m.strip()
            ]
        else:
            # Only hit when no system_provider is wired (bare client). Keep it neutral.
            messages = [{"role": "system", "content": fallback_persona()}]
            if memory.strip():
                messages.append({"role": "system", "content": f"Your saved memory:\n{memory}"})
        for msg in context:
            role = msg.get("role")
            if role not in {"user", "assistant", "system", "tool"}:
                msg = {**msg, "role": "system"}
            messages.append(msg)
        if not in_context:
            messages.append({"role": "user", "content": user_text})
        return messages

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
            "max_tokens": 1,
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
        self, user_text: str, memory: str, status: dict[str, Any], context: list[dict],
        in_context: bool = True,
    ) -> Iterator[str]:
        headers = self._headers()
        url = f"{self.cfg.base_url}/chat/completions"
        body = self._reasoning_body({
            "model": self.cfg.model,
            "messages": self._messages(user_text, memory, status, context, in_context),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        })
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
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
                        # Visible but dimmed — machinery, not character speech.
                        yield dim(thinking)
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
        except urllib.error.HTTPError as e:
            raise RuntimeError(e.read().decode("utf-8", errors="replace")) from e

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
        self, user_text, memory, status, context, tools, execute,
        record=None, max_steps: int = 8, in_context: bool = True,
    ):
        """Stream a reply that may call tools (modern OpenAI-style function calling).

        Yields text chunks for the UI. `execute(tc)` runs one tool call and returns
        {"display": ..., "content": ...}; results are fed back until the model
        produces a final answer.

        `record(msg)` commits each message (assistant incl. tool_calls and
        reasoning_content, tool results, system continuation notes) to the DURABLE
        context as soon as it exists — following hermes-agent's conversation
        history. If the UI abandons this generator mid-stream (operator interrupt),
        the partial turn is still committed, marked as cut off, so the model
        remembers what it was doing.
        """
        if not self.is_live():
            for ch in self._mock(user_text, memory, status):
                yield ch
            return
        record = record or (lambda _msg: None)
        messages = self._messages(user_text, memory, status, context, in_context=in_context)
        acc: list[str] = []  # text of the in-flight turn, readable by `finally`
        finished = False
        try:
            for _ in range(max_steps):
                acc.clear()
                tool_calls, reasoning, finish = yield from self._stream_turn(messages, tools, acc)
                text = "".join(acc).strip()
                acc.clear()  # committed below — must not re-commit as "interrupted"
                truncated = finish == "length"

                # DeepSeek thinking mode requires reasoning_content echoed on
                # replayed assistant tool-call messages; everyone else gets it
                # withheld (most providers reject echoed thinking).
                echo = self.reasoning_echoback_required()

                if truncated and tool_calls:
                    # Cut mid-arguments: the JSON is unusable. Drop the calls and
                    # tell the model to split the work (hermes pattern).
                    a_msg: dict[str, Any] = {"role": "assistant", "content": text or "(oversized tool call dropped)"}
                    if reasoning:
                        a_msg["reasoning_content"] = reasoning
                    record(a_msg)
                    messages.append(a_msg if echo else {k: v for k, v in a_msg.items() if k != "reasoning_content"})
                    note = {"role": "system", "content": self._SPLIT_TOOLS_NOTE}
                    record(note)
                    messages.append(note)
                    yield "\n" + dim("⚠ tool call truncated by the output limit — asking for smaller pieces") + "\n"
                    continue

                a_msg = {"role": "assistant", "content": text or None}
                if tool_calls:
                    a_msg["tool_calls"] = tool_calls
                if reasoning:
                    # Always kept for the record/transcript; replayed to the API
                    # only when the provider demands it (echo above).
                    a_msg["reasoning_content"] = reasoning
                record(a_msg)
                messages.append(a_msg if echo else {k: v for k, v in a_msg.items() if k != "reasoning_content"})

                if tool_calls:
                    for tc in tool_calls:
                        res = execute(tc)
                        display = res.get("display")
                        if display:
                            yield "\n" + dim(display) + "\n"
                        t_msg = {"role": "tool", "tool_call_id": tc.get("id") or "", "content": res.get("content", "")}
                        record(t_msg)
                        messages.append(t_msg)
                    continue

                if truncated:
                    note = {"role": "system", "content": self._CONTINUE_NOTE}
                    record(note)
                    messages.append(note)
                    yield "\n"
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
                    record({"role": "assistant", "content": partial + self.INTERRUPT_MARK})

    def _stream_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, text_out: list[str]):
        """Stream one assistant turn. Yields content chunks; accumulates visible
        text into `text_out` (caller-owned, so an abandoned generator can still
        read the partial). Returns (tool_calls, reasoning, finish_reason).
        """
        import urllib.error
        import urllib.request

        body: dict[str, Any] = self._reasoning_body({
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        })
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.cfg.base_url}/chat/completions", data=data, headers=self._headers(), method="POST")
        acc: dict[int, dict[str, str]] = {}
        reasoning_parts: list[str] = []
        finish_reason = ""
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
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
                        # Stream it dimmed: the operator sees the chara think
                        # (hermes/Claude-Code style) without it reading as speech.
                        yield dim(thinking)
                    chunk = delta.get("content")
                    if chunk:
                        text_out.append(chunk)
                        yield chunk
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
        except urllib.error.HTTPError as e:
            raise RuntimeError(e.read().decode("utf-8", errors="replace")) from e
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
        if "internal cycle" in lower or "内部循环" in user_text:
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
