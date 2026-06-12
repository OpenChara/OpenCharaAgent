"""Context management: interrupt-safe commits, think windowing, tool-aware trim."""
import pytest

from lunamoth.core.context import THINK_WINDOW, ContextBuffer
from lunamoth.session.settings import Settings


def test_render_sanitizes_and_withholds_reasoning():
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "did it", "reasoning_content": "secret thinking", "kind": "x"})
    out = c.render()
    assert out == [{"role": "assistant", "content": "did it"}]


def test_old_think_cycles_pruned_from_buffer():
    c = ContextBuffer()
    c.add("user", "please do X")
    for i in range(THINK_WINDOW + 5):
        c.add("assistant", f"musing {i}", kind="think")
    # Old monologues are pruned from the BUFFER itself (they stay in the
    # transcript), so they neither reach the API nor occupy trim budget —
    # a chatty daemon can't crowd out the operator's real instruction.
    thinks = [m for m in c.messages if m.get("kind") == "think"]
    assert len(thinks) == THINK_WINDOW
    assert thinks[-1]["content"].endswith(f"musing {THINK_WINDOW + 4}")  # newest kept
    assert c.render()[0] == {"role": "user", "content": "please do X"}  # still visible


def test_render_drops_orphaned_tool_results():
    c = ContextBuffer()
    # A restored window that BEGINS with a tool result (its assistant got
    # trimmed away in an earlier life) must not leak the orphan to the API.
    c.restore([
        {"role": "tool", "tool_call_id": "lost", "content": "stale result"},
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "kept"}]},
        {"role": "tool", "tool_call_id": "kept", "content": "fresh result"},
    ])
    rendered = c.render()
    assert all(m.get("tool_call_id") != "lost" for m in rendered)
    assert any(m.get("tool_call_id") == "kept" for m in rendered)  # valid pair survives


def test_trim_never_strands_tool_results():
    c = ContextBuffer(max_tokens=10, trim_buffer_tokens=0)
    c.messages = [
        {"role": "assistant", "content": "x" * 100, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "assistant", "content": "summary"},
    ]
    c.trim()
    # Dropping the tool_calls message must drop its orphaned results too.
    assert all(not m.get("tool_call_id") for m in c.messages)


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return LunaMothAgent(Settings(character_path="", **kw))

    return make


def test_interrupted_reply_keeps_instruction_and_partial(agent):
    a = agent()
    a.transcript.reset()
    s = a.make_session()
    gen = a.stream_handle("帮我写一首诗", s)
    next(gen)  # a few chars only...
    next(gen)
    gen.close()  # ...then the operator interrupts (the UI abandons the generator)
    pairs = s.context.pairs()
    assert ("user", "帮我写一首诗") in pairs  # the instruction is NEVER lost
    partial = [c for r, c in pairs if r == "assistant"]
    assert partial and "[cut off" in partial[-1]  # the partial is kept and marked


def test_interrupted_think_cycle_is_committed(agent):
    a = agent()
    a.transcript.reset()
    s = a.make_session()
    gen = a.stream_think(s)
    next(gen)
    gen.close()
    thinks = [m for m in s.context.messages if m.get("kind") == "think"]
    assert thinks and thinks[-1]["content"].strip()  # partial idle output committed


def test_commit_keeps_speech_and_drops_machinery_events(agent, monkeypatch):
    # Typed events replace the old in-band markers: only TextDelta is speech;
    # thinking and tool chatter must never leak into the committed context.
    from lunamoth.protocol import Notice, TextDelta, ThinkDelta, ToolEnd

    a = agent()
    a.transcript.reset()
    s = a.make_session()

    def mixed_stream(*_args, **_kw):
        def gen():
            yield ThinkDelta("pondering…")
            yield TextDelta("你好。")
            yield ToolEnd("terminal", summary="⚙ terminal ✓")
            yield Notice("retry", "⚠ retry 1/5")
            yield TextDelta("再见。")

        return gen()

    monkeypatch.setattr(a, "_reply_stream", mixed_stream)
    list(a.stream_handle("打个招呼", s))
    reply = [c for r, c in s.context.pairs() if r == "assistant"][-1]
    assert reply == "你好。再见。"
    assert "pondering" not in reply and "terminal" not in reply and "retry" not in reply


def test_reasoning_policy_openrouter_and_deepseek():
    from lunamoth.config import LLMConfig
    from lunamoth.core.llm import LLMClient

    def client(base_url, model):
        return LLMClient(LLMConfig(provider="openai_compatible", base_url=base_url, model=model))

    v4 = client("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash")
    assert v4.reasoning_supported() and v4.reasoning_echoback_required()
    llama = client("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct")
    assert not llama.reasoning_supported() and not llama.reasoning_echoback_required()
    # Direct DeepSeek speaks its own dialect: thinking is chosen by MODEL NAME,
    # the unified `reasoning` object must NOT be sent — but echo-back of
    # reasoning_content on replayed tool calls is still required.
    direct = client("https://api.deepseek.com/v1", "deepseek-chat")
    assert not direct.reasoning_supported() and direct.reasoning_echoback_required()
    ollama = client("http://localhost:11434/v1", "qwen2.5:3b-instruct")
    assert not ollama.reasoning_supported()


def test_render_echoes_reasoning_only_on_request():
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "done", "reasoning_content": "thought hard"})
    assert "reasoning_content" not in c.render()[0]
    assert c.render(include_reasoning=True)[0]["reasoning_content"] == "thought hard"
