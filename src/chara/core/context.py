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
import threading
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


# Context-window defaults — the ONE source. agent.py uses these as the env-var fallbacks
# too, so a ContextBuffer built without the env path (a direct construct / a test) computes
# the SAME compaction threshold (target = max_tokens - trim_buffer_tokens) as the live agent.
DEFAULT_CONTEXT_TOKENS = 65536
DEFAULT_TRIM_BUFFER_TOKENS = 4096


@dataclass
class ContextBuffer:
    max_tokens: int = DEFAULT_CONTEXT_TOKENS
    trim_buffer_tokens: int = DEFAULT_TRIM_BUFFER_TOKENS
    messages: list[dict] = field(default_factory=list)
    # Called for every NEW message (not for restored history); the transcript
    # store hooks in here. Trimming only narrows the in-memory window.
    persist: "Callable[[dict], None] | None" = None
    # Per-message token-estimate memo, keyed by id(msg). token_count() flattening
    # + json.dumps(tool_calls) of every message on every call was O(n) work
    # repeated inside trim()'s pop loop (→ O(K·n)) and again by compaction every
    # turn. We cache the per-message estimate and rebuild the memo to EXACTLY the
    # current messages on each token_count(), so:
    #   * a message kept across the call reuses its cached estimate (the win);
    #   * any externally-mutated/new message (compaction's `ctx.messages = …` and
    #     `msgs[i] = {…}`, agent.py's `.messages.append`, commands.py's `.clear`)
    #     is an id() miss and gets recomputed — no path can leave a stale value;
    #   * an id() freed by a pop and reused by a fresh dict cannot return the old
    #     estimate, because the memo is pruned to the current id set every call.
    _tok_memo: "dict[int, int]" = field(default_factory=dict, repr=False, compare=False)
    # Guards _tok_memo: token_count() runs on the TRANSPORT thread (snapshot →
    # protocol/api.py) while the worker thread's trim() iterates/rebuilds the
    # same dict — an unguarded write mid-comprehension is a RuntimeError. The
    # lock covers only the dict touches (never the token estimation), so the
    # memo's O(1) amortized behavior is unchanged and contention is negligible.
    _tok_lock: "threading.Lock" = field(default_factory=threading.Lock, repr=False, compare=False)

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

    def _msg_tokens(self, msg: dict) -> int:
        """Cached per-message estimate (the +2 envelope cost is included).

        Keyed by id(msg): a hit means this exact dict object was counted before
        and hasn't been replaced (compaction swaps in NEW dicts, which miss).
        The estimate FORMULA is unchanged — only its recomputation is avoided.
        """
        key = id(msg)
        with self._tok_lock:
            val = self._tok_memo.get(key)
        if val is None:
            val = estimate_tokens(_msg_text(msg)) + 2
            with self._tok_lock:
                self._tok_memo[key] = val
        return val

    def token_count(self) -> int:
        total = 0
        live: dict[int, int] = {}
        # Iterate a snapshot of the list: the transport thread counts while the
        # worker thread's trim() pops (a slightly stale total is fine; reading a
        # mutating list mid-iteration is not).
        for m in list(self.messages):
            key = id(m)
            v = live.get(key)
            if v is None:
                v = self._msg_tokens(m)
                live[key] = v
            total += v
        # Prune the memo down to exactly the messages present right now so a
        # later dict reusing a freed id() can never read a stale estimate.
        with self._tok_lock:
            self._tok_memo = live
        return total

    def trim(self) -> None:
        target = max(0, self.max_tokens - self.trim_buffer_tokens)
        total = self.token_count()  # also resyncs the memo to current messages
        while self.messages and total > target:
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
            total -= self._msg_tokens(dropped)  # subtract instead of full recount
            # Never strand tool results without the assistant call that made
            # them — the API rejects orphaned role:"tool" messages.
            if dropped.get("tool_calls"):
                while start < len(self.messages) and self.messages[start].get("tool_call_id"):
                    total -= self._msg_tokens(self.messages.pop(start))
        # Drop freed ids accumulated by the pop loop so the memo can't grow with
        # stale keys between full counts.
        if self.messages:
            live_ids = {id(m) for m in self.messages}
            with self._tok_lock:
                self._tok_memo = {k: v for k, v in self._tok_memo.items() if k in live_ids}
        else:
            with self._tok_lock:
                self._tok_memo = {}
