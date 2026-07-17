"""Failure policy: 5 retries on jittered exponential backoff (audit #5, hermes
retry_utils), Retry-After honored on 429, then surface the error.
There is NO fabricated fallback output anywhere — a failed request fails."""
import urllib.error
import urllib.request

import pytest

from chara.config import LLMConfig
from chara.core.llm import LLMClient
from chara.session.settings import Settings


def _client():
    return LLMClient(LLMConfig(provider="openai_compatible", base_url="https://x.test/v1", model="m"))


def _drive(gen):
    """Drive the connect generator; return (yielded notices, return value)."""
    notices = []
    try:
        while True:
            notices.append(next(gen))
    except StopIteration as stop:
        return notices, stop.value


@pytest.fixture
def no_sleep(monkeypatch):
    import chara.core.llm as llm_mod

    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)


def test_transient_failure_retries_then_succeeds(monkeypatch, no_sleep):
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection reset")
        return "RESPONSE"

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    notices, resp = _drive(_client()._connect_with_retry("https://x.test/v1/chat/completions", b"{}", 1))
    assert resp == "RESPONSE"
    # Retries surface as typed Notice events (frontends render them dimmed).
    assert len(notices) == 2 and all(n.kind == "retry" and "retry" in n.text for n in notices)


def test_gives_up_after_five_retries(monkeypatch, no_sleep):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    gen = _client()._connect_with_retry("https://x.test/v1/chat/completions", b"{}", 1)
    with pytest.raises(RuntimeError, match="gave up after 5 retries"):
        _drive(gen)


def test_permanent_http_error_surfaces_immediately(monkeypatch, no_sleep):
    class Fake401(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://x.test", 401, "unauthorized", {}, None)

        def read(self):
            # OpenRouter's confusing wording for an unrecognized key.
            return b'{"error": {"message": "User not found.", "code": 401}}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(Fake401()))
    gen = _client()._connect_with_retry("https://x.test/v1/chat/completions", b"{}", 1)
    with pytest.raises(RuntimeError) as exc:
        _drive(gen)  # no retries for auth errors — surface NOW
    msg = str(exc.value)
    assert "HTTP 401" in msg
    # the bug: it used to dump the raw body verbatim. Now it must EXPLAIN it
    # (a key problem), while still quoting the provider's own words as context.
    assert "API key" in msg and "User not found." in msg


def test_explain_http_error_classifies_not_dumps():
    from chara.core.llm import _explain_http_error

    auth = _explain_http_error(401, '{"error":{"message":"User not found.","code":401}}')
    assert "API key" in auth and 'User not found.' in auth
    assert _explain_http_error(401, "x") != "HTTP 401: x"          # never a raw dump
    assert "credit" in _explain_http_error(402, '{"error":{"message":"no funds"}}')
    assert "model" in _explain_http_error(404, '{"error":"nope"}').lower()
    assert "rate limited" in _explain_http_error(429, "").lower()
    # an unparseable body still yields a clean, prefixed line (no crash)
    assert _explain_http_error(500, "<html>oops</html>").startswith("HTTP 500:")


# ---- jittered backoff + Retry-After (audit #5) ------------------------------------------


def test_retry_delay_is_jittered_exponential():
    from chara.core._stream_util import _RETRY_MAX_DELAY, _retry_delay

    for attempt in range(1, 8):
        base = min(5.0 * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
        for _ in range(20):
            d = _retry_delay(attempt)
            assert base <= d <= 1.5 * base  # jitter is U(0, 0.5·delay), additive
    assert _retry_delay(50) <= 1.5 * _RETRY_MAX_DELAY  # capped, no overflow


def test_retry_after_wins_but_is_capped():
    from chara.core.llm import _retry_delay

    assert _retry_delay(3, retry_after=7.0) == 7.0      # the provider's own schedule — no jitter
    assert _retry_delay(1, retry_after=9999.0) == 120.0  # hostile header can't wedge the turn
    d = _retry_delay(2, retry_after=0.0)                 # nonsense value → normal backoff
    assert 10.0 <= d <= 15.0


def test_parse_retry_after_forms():
    import time as _time
    from email.utils import formatdate

    from chara.core.llm import _parse_retry_after

    assert _parse_retry_after({"Retry-After": "30"}) == 30.0
    assert _parse_retry_after({}) is None
    assert _parse_retry_after(None) is None
    assert _parse_retry_after({"Retry-After": "soonish"}) is None
    http_date = formatdate(_time.time() + 60, usegmt=True)
    delta = _parse_retry_after({"Retry-After": http_date})
    assert delta is not None and 50 <= delta <= 61


def test_429_honors_retry_after_header(monkeypatch):
    import chara.core.llm as llm_mod

    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    calls = {"n": 0}

    class Fake429(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://x.test", 429, "rate limited", {"Retry-After": "42"}, None)

        def read(self):
            return b'{"error": "slow down"}'

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Fake429()
        return "RESPONSE"

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    notices, resp = _drive(_client()._connect_with_retry("https://x.test/v1/chat/completions", b"{}", 1))
    assert resp == "RESPONSE"
    assert sleeps == [42.0]  # the provider's schedule, not the computed backoff
    assert "in 42s" in notices[0].text


def test_retry_budget_and_visible_error_unchanged(monkeypatch):
    # The no-fallback policy holds: exactly 5 retries, then a VISIBLE error.
    import chara.core.llm as llm_mod

    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    with pytest.raises(RuntimeError, match="gave up after 5 retries"):
        _drive(_client()._connect_with_retry("https://x.test/v1/chat/completions", b"{}", 1))
    assert len(sleeps) == 5
    # Delays grow exponentially: each within [base, 1.5·base] for base 5,10,20,40,80.
    for n, d in enumerate(sleeps, start=1):
        base = min(5.0 * 2 ** (n - 1), 120.0)
        assert base <= d <= 1.5 * base


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.core.agent import CharaAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return CharaAgent(Settings(character_path="", **kw))

    return make


def test_idle_cycle_failure_surfaces_without_fallback(agent, monkeypatch):
    a = agent()
    a.transcript.reset()
    s = a.make_session()

    def broken_stream(*_args, **_kw):
        def gen():
            raise RuntimeError("HTTP 500: upstream died — gave up after 5 retries")
            yield  # pragma: no cover (makes this a generator)

        return gen()

    monkeypatch.setattr(a, "_reply_stream", broken_stream)
    with pytest.raises(RuntimeError, match="upstream died"):
        list(a.stream_think(s))
    # No fabricated "cycle 0042: buffer stable" output ever enters the context.
    assert not [m for m in s.context.messages if m.get("role") == "assistant"]


# ---- empty-completion detection (audit #4) ---------------------------------------------
# A stream ending with no content, no tool calls and no finish_reason used to
# record assistant {content: None} and end the turn SILENTLY — an invisible
# non-answer. Now: bounded retry, then a visible error.


def _patch_turns(monkeypatch, turns):
    """Each entry: (text, tool_calls, thinking, finish). Pops one per call."""
    from chara.core.llm import LLMClient

    def fake_stream_turn(self, messages, tools, text_out, reasoning=None, channel="say"):
        text, tool_calls, thinking, finish = turns.pop(0)
        if text:
            text_out.append(text)
        return (tool_calls, thinking, finish, [])
        yield  # pragma: no cover — makes this a generator (yield from compatible)

    monkeypatch.setattr(LLMClient, "_stream_turn", fake_stream_turn)


def _agent_events(client, record=None):
    return list(client.stream_agent("hi", [], ["sys"], [], tools=None, execute=lambda tc: {}, record=record))


def test_truly_empty_stream_retries_then_raises_visibly(monkeypatch, no_sleep):
    _patch_turns(monkeypatch, [("", [], "", "")] * 4)  # empty forever
    recorded = []
    with pytest.raises(RuntimeError, match="empty stream"):
        _agent_events(_client(), record=recorded.append)
    # Nothing fabricated, nothing silently recorded: no assistant {content: None}.
    assert all(m.get("content") is not None for m in recorded)


def test_empty_retry_notices_are_visible(monkeypatch, no_sleep):
    from chara.protocol import Notice

    _patch_turns(monkeypatch, [("", [], "", "")] * 4)
    events = []
    try:
        for ev in _client().stream_agent("hi", [], ["sys"], [], tools=None, execute=lambda tc: {}):
            events.append(ev)
    except RuntimeError:
        pass
    retries = [e for e in events if isinstance(e, Notice) and e.kind == "retry"]
    assert len(retries) == 3  # every retry is announced, never silent


def test_reasoning_only_exhaustion_is_distinguished(monkeypatch, no_sleep):
    _patch_turns(monkeypatch, [("", [], "long private thinking", "stop")] * 4)
    with pytest.raises(RuntimeError, match="reasoning-only.*thinking exhausted"):
        _agent_events(_client())


def test_empty_then_real_reply_recovers(monkeypatch, no_sleep):
    _patch_turns(monkeypatch, [("", [], "", ""), ("", [], "", ""), ("here.", [], "", "stop")])
    recorded = []
    _agent_events(_client(), record=recorded.append)  # no exception
    assert recorded and recorded[-1]["content"] == "here."  # the recovered turn is committed
