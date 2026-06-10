"""Context management: interrupt-safe commits, think windowing, tool-aware trim."""
import pytest

from lunamoth.context import THINK_WINDOW, ContextBuffer
from lunamoth.settings import Settings


def test_render_sanitizes_and_withholds_reasoning():
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "did it", "reasoning_content": "secret thinking", "kind": "x"})
    out = c.render()
    assert out == [{"role": "assistant", "content": "did it"}]


def test_old_think_cycles_age_out_of_api_view():
    c = ContextBuffer()
    c.add("user", "please do X")
    for i in range(THINK_WINDOW + 5):
        c.add("assistant", f"[internal cycle]\nmusing {i}", kind="think")
    rendered = c.render()
    thinks = [m for m in rendered if "internal cycle" in str(m.get("content"))]
    assert len(thinks) == THINK_WINDOW  # monologue flood can't bury the instruction
    assert rendered[0] == {"role": "user", "content": "please do X"}  # still visible
    # ...but nothing was deleted from memory/transcript.
    assert len(c.messages) == THINK_WINDOW + 6


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
    from lunamoth.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return LunaMothAgent(Settings(character_path="", world_path="", **kw))

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
    assert thinks and "[internal cycle]" in thinks[-1]["content"]


def test_strip_dim_removes_machinery_spans():
    from lunamoth.llm import DIM_OFF, DIM_ON, dim, strip_dim

    mixed = dim("thinking about it…") + "你好。" + "\n" + dim("⚙ terminal ✓") + "\n再见。"
    assert strip_dim(mixed) == "你好。\n\n再见。"
    assert DIM_ON not in strip_dim(mixed) and DIM_OFF not in strip_dim(mixed)


def test_reasoning_policy_openrouter_and_deepseek():
    from lunamoth.config import LLMConfig
    from lunamoth.llm import LLMClient

    def client(base_url, model):
        return LLMClient(LLMConfig(provider="openai_compatible", base_url=base_url, model=model))

    v4 = client("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash")
    assert v4.reasoning_supported() and v4.reasoning_echoback_required()
    llama = client("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct")
    assert not llama.reasoning_supported() and not llama.reasoning_echoback_required()
    direct = client("https://api.deepseek.com/v1", "deepseek-chat")
    assert direct.reasoning_supported() and direct.reasoning_echoback_required()
    ollama = client("http://localhost:11434/v1", "qwen2.5:3b-instruct")
    assert not ollama.reasoning_supported()


def test_render_echoes_reasoning_only_on_request():
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "done", "reasoning_content": "thought hard"})
    assert "reasoning_content" not in c.render()[0]
    assert c.render(include_reasoning=True)[0]["reasoning_content"] == "thought hard"
