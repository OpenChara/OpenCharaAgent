"""Context management: interrupt-safe commits, uniform self-work turns, tool-aware trim."""
import pytest

from lunamoth.core.context import ContextBuffer
from lunamoth.session.settings import Settings


def test_render_sanitizes_and_withholds_reasoning():
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "did it", "reasoning_content": "secret thinking", "kind": "x"})
    out = c.render()
    assert out == [{"role": "assistant", "content": "did it"}]


def test_self_work_cycles_all_survive_as_plain_assistant_messages():
    # hermes-faithful: a chara's self-work turns are first-class assistant
    # messages, NOT a class pruned to a window. Many consecutive self-work
    # cycles all stay in the buffer (aged only by trim/compaction), and none
    # carry a classification tag.
    c = ContextBuffer()
    c.add("user", "please do X")
    n = 20
    for i in range(n):
        c.add("assistant", f"musing {i}")
    assistants = [m for m in c.messages if m.get("role") == "assistant"]
    assert len(assistants) == n  # nothing pruned to a window
    assert all("kind" not in m for m in assistants)  # plain assistant messages
    assert c.render()[0] == {"role": "user", "content": "please do X"}  # still visible
    assert c.render()[-1] == {"role": "assistant", "content": f"musing {n - 1}"}


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


def test_trim_protects_the_summary_head():
    # After compaction, messages[0] is the kind="summary" row holding the
    # entire compressed past — the backstop trim must eat chatter, never it.
    c = ContextBuffer(max_tokens=100, trim_buffer_tokens=0)
    summary = {"role": "system", "content": "s" * 200, "kind": "summary"}
    c.messages = [summary] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"chatter {i} " + "y" * 80}
        for i in range(10)
    ]
    c.trim()
    assert c.messages[0] is summary  # the past survives
    assert len(c.messages) < 11      # chatter was trimmed instead


def test_oversized_summary_alone_survives_trim():
    # A summary fatter than the whole budget is kept anyway — it IS the past;
    # the tail shrinks to nothing around it rather than the summary vanishing.
    c = ContextBuffer(max_tokens=50, trim_buffer_tokens=0)
    summary = {"role": "system", "content": "s" * 2000, "kind": "summary"}
    c.messages = [summary, {"role": "user", "content": "hi"}]
    c.trim()
    assert c.messages == [summary]


def test_trim_after_summary_never_strands_tool_results():
    # The orphan-drop rule must keep working when trim starts past the summary.
    c = ContextBuffer(max_tokens=10, trim_buffer_tokens=0)
    c.messages = [
        {"role": "system", "content": "old facts", "kind": "summary"},
        {"role": "assistant", "content": "x" * 100, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "assistant", "content": "z" * 100},
    ]
    c.trim()
    assert c.messages[0].get("kind") == "summary"
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
    # Self-work output is committed as a plain assistant message (no kind tag).
    assistants = [m for m in s.context.messages if m.get("role") == "assistant"]
    assert assistants and assistants[-1]["content"].strip()  # partial idle output committed
    assert all("kind" not in m for m in assistants)


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


def test_render_pads_missing_reasoning_to_single_space_on_echo():
    """Echo provider + assistant tool-call turn with NO reasoning_content → the
    replay MUST carry a non-empty string (a single space), never ""/absent —
    DeepSeek V4 Pro / Kimi thinking mode 400s otherwise (tier 4)."""
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "terminal", "arguments": "{}"}}]})
    assert "reasoning_content" not in c.render()[0]              # withheld by default
    echoed = c.render(include_reasoning=True)[0]
    assert echoed["reasoning_content"] == " "                    # padded, never absent


def test_render_upgrades_empty_string_reasoning_on_echo():
    """tier 1: a pinned empty-string reasoning_content upgrades to ' ' on echo."""
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "x", "reasoning_content": ""})
    assert c.render(include_reasoning=True)[0]["reasoning_content"] == " "


def test_render_non_string_reasoning_dropped_or_padded():
    """tier 5: None reasoning_content (e.g. after compaction) → padded on echo,
    absent when not echoing."""
    c = ContextBuffer()
    c.add_message({"role": "assistant", "content": "x", "reasoning_content": None})
    assert "reasoning_content" not in c.render()[0]
    assert c.render(include_reasoning=True)[0]["reasoning_content"] == " "


def test_render_never_injects_reasoning_on_non_assistant():
    c = ContextBuffer()
    c.add_message({"role": "user", "content": "hi"})
    c.add_message({"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "terminal", "arguments": "{}"}}]})
    c.add_message({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    out = c.render(include_reasoning=True)
    assert "reasoning_content" not in out[0]   # user
    assert "reasoning_content" not in out[2]   # tool result


def test_render_passes_reasoning_details_unmodified_both_modes():
    """reasoning_details (signature/encrypted continuity blocks) rides EVERY
    replay unmodified — echo or not."""
    c = ContextBuffer()
    rd = [{"type": "reasoning.encrypted", "signature": "sig-abc", "data": "..."}]
    c.add_message({"role": "assistant", "content": "done", "reasoning_details": rd})
    assert c.render()[0]["reasoning_details"] == rd
    assert c.render(include_reasoning=True)[0]["reasoning_details"] == rd


def test_reasoning_echoback_host_match_not_substring():
    """Host-matched, so a lookalike path can't false-trigger the echo gate."""
    from lunamoth.config import LLMConfig
    from lunamoth.core.llm import LLMClient

    def client(base_url, model, provider="openai_compatible"):
        return LLMClient(LLMConfig(provider=provider, base_url=base_url, model=model))

    # substring "api.deepseek.com" sits in the PATH, not the host → must be False
    assert not client("https://evil.com/api.deepseek.com/v1", "llama-3").reasoning_echoback_required()
    # real Kimi/Moonshot host → True
    assert client("https://api.moonshot.ai/v1", "kimi-k2").reasoning_echoback_required()
    # provider tag alone → True
    assert client("https://x.test/v1", "some-model", provider="kimi-coding").reasoning_echoback_required()
    # MiMo by model name → True
    assert client("https://x.test/v1", "xiaomi/mimo-7b").reasoning_echoback_required()


def test_model_keeps_thought_signature_gate():
    from lunamoth.core.llm import _model_keeps_thought_signature
    assert _model_keeps_thought_signature("google/gemini-3-pro")
    assert _model_keeps_thought_signature("gemma-3-27b")
    assert not _model_keeps_thought_signature("deepseek/deepseek-v4")
    assert not _model_keeps_thought_signature(None)
