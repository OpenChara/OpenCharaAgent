"""Sliding conversation window over full OpenAI-style message dicts.

Messages are dicts (`{"role", "content", ...}`) rather than (role, content)
tuples so that assistant tool calls, tool results and reasoning survive in the
durable history — the model must remember what it ran last turn (the design
follows hermes-agent's conversation history; see also transcript.py).

Extra keys we use:
    tool_calls      assistant message that invoked tools (OpenAI shape)
    tool_call_id    a tool-result message answering one call
    reasoning_content  model thinking, stored for the record but NOT replayed
                    to the API (most providers reject or double-bill it)
    kind            'think' marks idle self-talk monologues; render() replays
                    only the most recent few so monologue floods can't bury
                    the operator's actual instructions
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

# How many idle self-talk monologues stay visible to the model. Older ones
# remain in memory/transcript but are dropped from the API view.
THINK_WINDOW = 4

# Keys allowed through to the chat-completions API.
_API_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def estimate_tokens(text: str) -> int:
    # Conservative-ish mixed Chinese/English approximation.
    # For context-budget bookkeeping, an exact tokenizer is unnecessary.
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    other = max(0, len(text) - cjk)
    return cjk + other // 4


def _msg_text(msg: dict) -> str:
    parts = [str(msg.get("content") or "")]
    if msg.get("tool_calls"):
        try:
            parts.append(json.dumps(msg["tool_calls"], ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return "\n".join(p for p in parts if p)


@dataclass
class ContextBuffer:
    max_tokens: int = 65536
    trim_buffer_tokens: int = 4096
    messages: list[dict] = field(default_factory=list)
    # Called for every NEW message (not for restored history); the transcript
    # store hooks in here. Trimming only narrows the in-memory window.
    persist: "Callable[[dict], None] | None" = None

    def add(self, role: str, content: str, kind: str = "") -> None:
        msg: dict[str, Any] = {"role": role, "content": content}
        if kind:
            msg["kind"] = kind
        self.add_message(msg)

    def add_message(self, msg: dict) -> None:
        self.messages.append(msg)
        if self.persist is not None:
            self.persist(msg)
        self.trim()

    def restore(self, rows: list[dict]) -> None:
        """Load previously persisted history WITHOUT re-persisting it."""
        self.messages = [dict(m) for m in rows]
        self.trim()

    def pairs(self) -> list[tuple[str, str]]:
        """(role, content) view for UIs/tests — structured fields flattened away."""
        return [(str(m.get("role", "")), str(m.get("content") or "")) for m in self.messages]

    def render(self, include_reasoning: bool = False) -> list[dict]:
        """API-ready view: sanitized keys, old think cycles dropped so self-talk
        can't bury the operator's instructions. Reasoning is withheld unless the
        provider demands the echo-back (DeepSeek thinking mode — see llm.py)."""
        keys = _API_KEYS + ("reasoning_content",) if include_reasoning else _API_KEYS
        think_seen = 0
        out: list[dict] = []
        for msg in reversed(self.messages):
            if msg.get("kind") == "think":
                think_seen += 1
                if think_seen > THINK_WINDOW:
                    continue
            out.append({k: msg[k] for k in keys if k in msg})
        out.reverse()
        return out

    def token_count(self) -> int:
        return sum(estimate_tokens(_msg_text(m)) + 2 for m in self.messages)

    def trim(self) -> None:
        target = max(0, self.max_tokens - self.trim_buffer_tokens)
        while self.messages and self.token_count() > target:
            dropped = self.messages.pop(0)
            # Never strand tool results without the assistant call that made
            # them — the API rejects orphaned role:"tool" messages.
            if dropped.get("tool_calls"):
                while self.messages and self.messages[0].get("tool_call_id"):
                    self.messages.pop(0)
