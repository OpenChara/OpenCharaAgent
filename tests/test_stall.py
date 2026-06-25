"""Stream stall watchdog (audit #1): SSE keep-alives defeat socket timeouts, so
a payload-level wall clock aborts dead streams visibly instead of hanging."""
import threading
import urllib.request

import pytest

from lunamoth.config import LLMConfig
from lunamoth.core.llm import LLMClient, StreamStall, _StallGuard, _stall_timeout_for
from lunamoth.protocol import Notice, TextDelta


class HangingResp:
    """A streaming response that emits its lines, then hangs until close()."""

    def __init__(self, lines=()):
        self._lines = list(lines)
        self._gate = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0)
        self._gate.wait()  # the upstream goes silent (keep-alive only / dead socket)
        raise StopIteration

    def close(self):
        self.closed = True
        self._gate.set()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def test_guard_passes_lines_through_and_ends_on_eof():
    class FiniteResp(HangingResp):
        def __next__(self):
            if self._lines:
                return self._lines.pop(0)
            raise StopIteration

    guard = _StallGuard(FiniteResp([b"a", b"b"]), first_byte_timeout=5, stall_timeout=5)
    assert list(guard.lines()) == [b"a", b"b"]


def test_first_byte_deadline_aborts_a_mute_connection():
    resp = HangingResp([])  # accepted the request, never emits one event
    guard = _StallGuard(resp, first_byte_timeout=0.05, stall_timeout=5)
    with pytest.raises(StreamStall, match="no stream data"):
        list(guard.lines())
    assert resp.closed  # the dead connection is torn down, not leaked


def test_keepalive_lines_do_not_count_as_payload():
    # Lines flow (SSE pings) but the consumer never sees a payload chunk:
    # the stall clock must NOT be reset by mere traffic.
    resp = HangingResp([b": ping"] * 3)
    guard = _StallGuard(resp, first_byte_timeout=5, stall_timeout=0.05)
    with pytest.raises(StreamStall, match="no payload chunk"):
        list(guard.lines())
    assert resp.closed


def test_mark_payload_keeps_a_thinking_stream_alive():
    class FiniteResp(HangingResp):
        def __next__(self):
            if self._lines:
                return self._lines.pop(0)
            raise StopIteration

    guard = _StallGuard(FiniteResp([b"x"] * 5), first_byte_timeout=5, stall_timeout=0.2)
    got = []
    for line in guard.lines():
        got.append(line)
        guard.mark_payload()  # the consumer saw real chunks — no stall
    assert len(got) == 5


def test_stall_timeout_scales_with_prompt_size():
    assert _stall_timeout_for(1000) == 180.0
    assert _stall_timeout_for(51_000 * 4) == 240.0  # >50k estimated tokens
    assert _stall_timeout_for(101_000 * 4) == 300.0  # >100k


def test_stream_turn_stall_surfaces_notice_then_error(monkeypatch):
    # The stall budgets live in _stream_util (where _StallGuard / _stall_timeout_for
    # read them); patch them there, not on the llm module that re-imports the helpers.
    import lunamoth.core._stream_util as su

    monkeypatch.setattr(su, "_FIRST_BYTE_TIMEOUT", 0.5)
    monkeypatch.setattr(su, "_STALL_TIMEOUT", 0.05)
    resp = HangingResp([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'])
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: resp)

    client = LLMClient(LLMConfig(provider="openai_compatible", base_url="https://x.test/v1", model="m"))
    events = []
    text_out = []
    gen = client._stream_turn([{"role": "user", "content": "hi"}], None, text_out)
    with pytest.raises(RuntimeError, match="stream stalled"):
        for ev in gen:
            events.append(ev)
    assert any(isinstance(e, TextDelta) and e.text == "partial" for e in events)  # partial delivered
    assert any(isinstance(e, Notice) and e.kind == "stall" for e in events)  # visible notice
    assert resp.closed  # the request was aborted, not left dangling
