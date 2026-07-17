"""Anthropic prompt-cache breakpoints (system_and_3 layout).

Places exactly 4 ``cache_control`` markers on the API message copy — the
system prefix plus the last 3 non-system messages — at a single TTL (5m or
1h). On Anthropic-family models this reuses the cached prefix across turns
within a session, cutting input-token cost on multi-turn conversations.

Pure functions, no state, no agent dependency. The markers go on a DEEP
COPY of the messages the caller hands in: the stored context and the
byte-stable stable-prefix cache are never touched. Only routes that honour
Anthropic ``cache_control`` get markers — every other route is left exactly
as assembled (an unknown extra field would otherwise risk a provider 400).

We mainly speak the OpenAI wire. Two layouts:
  * native layout (markers on the inner content blocks, and on the tool
    message envelope) — for endpoints speaking the native Anthropic
    protocol against an Anthropic-family model;
  * envelope/OpenAI-wire layout (markers on content parts only, never on a
    top-level tool message) — for OpenRouter, which forwards Anthropic
    ``cache_control`` upstream for ``anthropic/*`` models, and OpenAI-wire
    proxies that accept the looser form.

Ported apple-to-apple from the upstream reference's prompt_caching.py
(``system_and_3`` strategy); the policy gate is trimmed to the routes this
runtime actually speaks.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlparse


def _host(base_url: str) -> str:
    """Lower-cased hostname of a base URL ('' when unparseable)."""
    try:
        raw = (base_url or "").strip()
        if raw and "://" not in raw:
            raw = "//" + raw
        return (urlparse(raw).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def cache_policy(base_url: str, model: str) -> tuple[bool, bool]:
    """Decide ``(should_cache, native_layout)`` for this route + model.

    Returns ``(False, False)`` for any route we don't know honours Anthropic
    ``cache_control`` — caching is best-effort and never sends an unknown
    field to a provider that might reject it.

      * native Anthropic endpoint -> ``(True, True)`` (inner-block layout);
      * OpenRouter + an ``anthropic/*`` (claude) model -> ``(True, False)``
        (OpenRouter forwards the marker upstream on the OpenAI wire);
      * everything else -> ``(False, False)``.
    """
    host = _host(base_url)
    model_l = (model or "").lower()
    is_claude = "claude" in model_l
    is_anthropic_host = host == "api.anthropic.com" or host.endswith(".anthropic.com")
    is_openrouter = host == "openrouter.ai" or host.endswith(".openrouter.ai")

    if is_anthropic_host:
        return True, True
    if is_openrouter and is_claude:
        return True, False
    return False, False


def _build_marker(ttl: str) -> dict[str, str]:
    """A ``cache_control`` marker for the given TTL ('5m' default, or '1h')."""
    marker: dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_marker(msg: dict[str, Any], marker: dict[str, str], native: bool) -> None:
    """Place one ``cache_control`` marker on a single message in place.

    Handles every content shape: tool-role envelope (native only), empty
    content, plain string (promoted to a one-part text list), and a content
    list (marker on the last part).
    """
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        if native:
            msg["cache_control"] = marker
        return

    if content is None or content == "":
        msg["cache_control"] = marker
        return

    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content, "cache_control": marker}]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_cache_control(
    api_messages: list[dict[str, Any]],
    ttl: str = "5m",
    native: bool = False,
) -> list[dict[str, Any]]:
    """Return a DEEP COPY of ``api_messages`` with up to 4 cache breakpoints:
    the system prefix + the last 3 non-system messages, all at one TTL.

    The input list (and so the stored context / stable-prefix cache) is never
    mutated — only the returned copy carries markers. Trailing volatile-tail
    ``system`` messages are skipped by the non-system filter, so the 3
    conversational breakpoints land on real user/assistant/tool turns.

    Copy-on-write: we shallow-copy the LIST, then deep-copy ONLY the ≤4 messages
    we actually annotate. A tool result can be 100KB+, so deep-copying the whole
    list every tool step (the old ``copy.deepcopy``) was wasteful — and needless,
    since the other messages are returned by reference unchanged. The output is
    byte-identical to before; the caller's messages (and any nested object they
    contain) are still never mutated, because every message _apply_marker touches
    is a fresh deep copy.
    """
    if not api_messages:
        return []

    marker = _build_marker(ttl)

    # Indices we will mark: the leading system prefix + the last (4 - used)
    # non-system messages. Compute first so we deep-copy ONLY those.
    to_mark: list[int] = []
    used = 0
    if api_messages[0].get("role") == "system":
        to_mark.append(0)
        used += 1
    remaining = 4 - used
    non_sys = [i for i in range(len(api_messages)) if api_messages[i].get("role") != "system"]
    to_mark.extend(non_sys[-remaining:])

    messages = list(api_messages)  # shallow: unmarked messages stay by reference
    for idx in to_mark:
        msg = copy.deepcopy(messages[idx])  # deep-copy only what we mutate
        _apply_marker(msg, marker, native)
        messages[idx] = msg

    return messages
