"""A /provider or /model swap must strip stale cross-provider reasoning continuity.

reasoning_details (Anthropic/Gemini signed thinking blocks) and per-tool-call
extra_content are opaque to any route that didn't emit them; render() replays
reasoning_details unconditionally, so without a strip on swap a cross-provider
switch poisons every later turn (a strict endpoint can 400 on the foreign field).
"""
from __future__ import annotations

import pytest

from chara.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    from chara.core import agent as agent_mod

    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", tmp_path / "sandbox")
    from chara.core.agent import CharaAgent

    a = CharaAgent(Settings(provider="mock", character_path="", toolpack=""))
    a.transcript.reset()
    return a


def _seed_continuity(session) -> None:
    session.context.messages[:] = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "hello",
            "reasoning_details": [{"type": "reasoning.encrypted", "data": "xx"}],
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
                "extra_content": {"thought_signature": "sig"},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]


def _assert_stripped(session) -> None:
    asst = session.context.messages[1]
    assert "reasoning_details" not in asst
    assert "extra_content" not in asst["tool_calls"][0]
    # The plain content / tool-call structure is otherwise untouched.
    assert asst["content"] == "hello"
    assert asst["tool_calls"][0]["id"] == "c1"


def test_swap_provider_strips_reasoning_continuity(agent):
    session = agent.make_session()
    _seed_continuity(session)
    agent.swap_provider(provider="openai_compatible",
                        base_url="https://api.example/v1", api_key="sk-x",
                        model="some-model", session=session)
    _assert_stripped(session)


def test_swap_model_strips_reasoning_continuity(agent):
    session = agent.make_session()
    _seed_continuity(session)
    agent.swap_model("another-model", session=session)
    _assert_stripped(session)


# ---- window resync on swap (2026-07-02 audit P2) ---------------------------------
# Swapping a wide-window model for a narrow one must resize the LIVE session:
# max_tokens AND the trim buffer together. A stale 1M window overflows a 128K
# endpoint with 400s; a stale 100k trim buffer against a 64k window yields a
# trim target of max(0, 64000-100000)=0 — the next add_message pops the ENTIRE
# live context, silently.


def _pin_windows(monkeypatch, windows: dict[str, int]) -> None:
    from chara.core import providers

    def fake_resolved(provider, base_url, model, api_key, override=0):
        return windows.get(model, 200_000), True

    monkeypatch.setattr(providers, "context_window_resolved", fake_resolved)


def test_swap_model_resyncs_context_window(agent, monkeypatch):
    _pin_windows(monkeypatch, {"big-model": 1_000_000, "small-model": 64_000})
    agent.settings.model = "big-model"
    session = agent.make_session()
    assert session.context.max_tokens == 1_000_000
    assert session.context.trim_buffer_tokens == 100_000
    agent.swap_model("small-model", session=session)
    assert session.context.max_tokens == 64_000
    assert session.context.trim_buffer_tokens == 8_000
    assert session.context.max_tokens - session.context.trim_buffer_tokens > 0


def test_swap_provider_resyncs_context_window(agent, monkeypatch):
    _pin_windows(monkeypatch, {"big-model": 1_000_000, "small-model": 64_000})
    agent.settings.model = "big-model"
    session = agent.make_session()
    assert session.context.max_tokens == 1_000_000
    agent.swap_provider(provider="openai_compatible",
                        base_url="https://api.example/v1", api_key="sk-x",
                        model="small-model", session=session)
    assert session.context.max_tokens == 64_000
    assert session.context.trim_buffer_tokens == 8_000


def test_swap_to_narrow_window_trims_the_live_context(agent, monkeypatch):
    # Resizing alone is not enough: idle/react/event paths build the next request
    # WITHOUT an add_message (whose trim would be the first to notice), so the
    # swap itself must bring an over-window history back under the new window.
    _pin_windows(monkeypatch, {"big-model": 1_000_000, "small-model": 64_000})
    agent.settings.model = "big-model"
    session = agent.make_session()
    for i in range(30):
        session.context.messages.append({"role": "user", "content": f"{i} " + "x" * 40_000})
    assert session.context.token_count() > 64_000  # over the narrow window
    agent.swap_model("small-model", session=session)
    assert session.context.token_count() <= session.context.max_tokens - session.context.trim_buffer_tokens
    assert session.context.messages  # trimmed, not wiped


def test_swap_with_no_session_is_a_noop(agent, monkeypatch):
    _pin_windows(monkeypatch, {"small-model": 64_000})
    agent.swap_model("small-model", session=None)  # must not raise
