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

A chara's self-work turns are ordinary assistant messages here — no per-message
classification, aged only by the normal trim/compaction path (hermes-faithful:
context is bounded by length, never by deleting a class of messages).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

# Keys allowed through to the chat-completions API.
_API_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def _flatten_content(content: Any) -> str:
    """Text view of a message's content — a plain string passes through; a
    multimodal list collapses to its joined ``text`` parts (images dropped)."""
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return str(content or "")


def estimate_tokens(text: str) -> int:
    # Conservative-ish mixed Chinese/English approximation.
    # For context-budget bookkeeping, an exact tokenizer is unnecessary.
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    other = max(0, len(text) - cjk)
    return cjk + other // 4


def _msg_text(msg: dict) -> str:
    # Flatten multimodal list content to its text parts — never str(list), which
    # would count a ~2MB image_url base64 blob as "text" in token_count / the
    # compaction tail-walk (image bytes aren't text tokens; they're handled by
    # strip_old_images).
    parts = [_flatten_content(msg.get("content"))]
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
        """(role, content) view for UIs/tests — structured fields flattened away.
        Multimodal content (a list of parts) collapses to its text parts."""
        return [(str(m.get("role", "")), _flatten_content(m.get("content"))) for m in self.messages]

    def render(self, include_reasoning: bool = False) -> list[dict]:
        """API-ready view: sanitized keys, orphaned tool results dropped.

        Reasoning is withheld unless the provider demands the echo-back
        (`include_reasoning=True`, set from llm.reasoning_echoback_required()).
        When echoing, every assistant message MUST carry a non-empty STRING
        reasoning_content or DeepSeek V4 Pro / Kimi / MiMo thinking mode 400s
        the replay ("the reasoning content in thinking mode must be passed back
        to the API"); a single space satisfies the non-empty check without
        fabricating chain-of-thought. Mirrors the upstream reference's replay
        tiers, collapsed to the three reachable here (this codebase has ONE
        reasoning key and no mid-session provider fallback, so the cross-
        provider promote/poison tiers are structurally moot):
          1. explicit string reasoning_content kept verbatim ("" upgraded to
             " " when echoing — pre-tightening history pinned empty strings);
          4. echo provider + no usable reasoning_content -> " ";
          5. non-string reasoning_content (e.g. None after compaction) dropped
             when not echoing, padded to " " when echoing.
        reasoning_details (opaque signature / encrypted_content continuity
        blocks) rides EVERY replay unmodified, echo or not."""
        out: list[dict] = []
        declared_call_ids: set[str] = set()
        for msg in self.messages:
            # A role:"tool" message is only valid right after the assistant
            # message that declared its tool_call_id; if that assistant got
            # trimmed/restored away, the orphan would 400 the API (hermes
            # sanitizes tool pairs the same way).
            if msg.get("tool_call_id") and msg["tool_call_id"] not in declared_call_ids:
                continue
            for tc in msg.get("tool_calls") or []:
                if tc.get("id"):
                    declared_call_ids.add(tc["id"])
            api_msg = {k: msg[k] for k in _API_KEYS if k in msg}
            details = msg.get("reasoning_details")
            if isinstance(details, list) and details:
                api_msg["reasoning_details"] = details
            if include_reasoning and msg.get("role") == "assistant":
                rc = msg.get("reasoning_content")
                if isinstance(rc, str):
                    api_msg["reasoning_content"] = rc if rc != "" else " "   # tier 1 (+ "" upgrade)
                else:
                    api_msg["reasoning_content"] = " "                       # tier 4 (None/missing -> pad)
            out.append(api_msg)
        return out

    def token_count(self) -> int:
        return sum(estimate_tokens(_msg_text(m)) + 2 for m in self.messages)

    def trim(self) -> None:
        target = max(0, self.max_tokens - self.trim_buffer_tokens)
        while self.messages and self.token_count() > target:
            # After compaction messages[0] IS the kind="summary" row holding the
            # entire compressed past — trimming it would delete everything old
            # in one pop. Start at the first non-summary message; if only the
            # summary remains it stays even over budget (it is the past, and
            # the tail must shrink around it).
            start = 0
            while start < len(self.messages) and self.messages[start].get("kind") == "summary":
                start += 1
            if start >= len(self.messages):
                break
            dropped = self.messages.pop(start)
            # Never strand tool results without the assistant call that made
            # them — the API rejects orphaned role:"tool" messages.
            if dropped.get("tool_calls"):
                while start < len(self.messages) and self.messages[start].get("tool_call_id"):
                    self.messages.pop(start)
