"""Streaming-transport hardening for the OpenAI-compatible client (core/llm.py).

Self-contained helpers extracted from llm.py to keep that module focused on the
request/stream/tool loop. NONE of this is the hermes-ported reasoning/cache core
(that stays in llm.py) — these are the value-neutral transport scars:

  * lone-surrogate sanitization (_scrub_surrogates / _SurrogateJoiner)
  * tool-call argument repair (_repair_tool_args / _preflight_history_tool_calls)
  * jittered retry backoff (_retry_delay / _parse_retry_after)
  * the SSE stall watchdog (_StallGuard / StreamStall / _stall_timeout_for)

llm.py re-imports the names its loop uses; tests import the helpers from here.
"""
from __future__ import annotations

import json
import queue
import random
import re
import threading
import time
from typing import Any, Iterator, NoReturn

from ..obs import get_logger

_log = get_logger("llm")


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
        threading.Thread(target=self._read, name="chara-stream-reader", daemon=True).start()

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

