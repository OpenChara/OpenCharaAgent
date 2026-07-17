"""Context management: interrupt-safe commits, uniform self-work turns, tool-aware trim."""
import pytest

from chara.core.context import ContextBuffer
from chara.session.settings import Settings


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


# ---- token_count memoization (perf optimization, must stay byte-exact) --------

from chara.core.context import _msg_text, estimate_tokens  # noqa: E402


def _fresh_count(messages):
    """The original from-scratch formula — the memoized count must always match."""
    return sum(estimate_tokens(_msg_text(m)) + 2 for m in messages)


def test_token_count_matches_fresh_recompute_across_mutations():
    c = ContextBuffer(max_tokens=10**9, trim_buffer_tokens=0)  # never trims
    c.add("user", "hello world")
    assert c.token_count() == _fresh_count(c.messages)

    # add more
    for i in range(5):
        c.add("assistant", f"reply {i} " + "z" * 50)
    assert c.token_count() == _fresh_count(c.messages)

    # tool_calls message (json.dumps path)
    c.add_message({"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "terminal", "arguments": "{}"}}]})
    c.add_message({"role": "tool", "tool_call_id": "c1", "content": "x" * 200})
    assert c.token_count() == _fresh_count(c.messages)


def test_token_count_consistent_after_trim_pops():
    c = ContextBuffer(max_tokens=60, trim_buffer_tokens=0)
    for i in range(30):
        c.add("user" if i % 2 == 0 else "assistant", f"msg {i} " + "w" * 40)
    # after add-driven trims, the memoized count equals a fresh recompute
    assert c.token_count() == _fresh_count(c.messages)
    # an explicit extra trim is a no-op on the count's correctness
    c.trim()
    assert c.token_count() == _fresh_count(c.messages)


def test_token_count_consistent_after_external_message_swap():
    # compaction does `ctx.messages = [...]` and `msgs[i] = {...}` outside the
    # class — a NEW dict at a possibly-reused id() must be recounted, never read
    # stale from the memo.
    c = ContextBuffer(max_tokens=10**9, trim_buffer_tokens=0)
    for i in range(4):
        c.add("assistant", f"long {i} " + "q" * 100)
    c.token_count()  # warm the memo
    # swap one message for a much shorter one (mimics prune_live_tool_outputs)
    c.messages[1] = {"role": "assistant", "content": "tiny"}
    assert c.token_count() == _fresh_count(c.messages)
    # full reassignment (mimics compaction's summary + tail rewrite)
    c.messages = [{"role": "system", "content": "summary", "kind": "summary"},
                  {"role": "user", "content": "fresh tail"}]
    assert c.token_count() == _fresh_count(c.messages)


def test_token_count_consistent_after_clear_and_append():
    # commands.py clears in place; agent.py appends in place.
    c = ContextBuffer(max_tokens=10**9, trim_buffer_tokens=0)
    c.add("user", "one")
    c.token_count()
    c.messages.clear()
    assert c.token_count() == 0 == _fresh_count(c.messages)
    c.messages.append({"role": "system", "content": "volatile env", "kind": "todo"})
    assert c.token_count() == _fresh_count(c.messages)


def test_token_count_restore_resets_memo():
    c = ContextBuffer(max_tokens=10**9, trim_buffer_tokens=0)
    c.add("user", "throwaway " + "v" * 300)
    c.token_count()
    c.restore([{"role": "user", "content": "restored"},
               {"role": "assistant", "content": "ok " + "p" * 80}])
    assert c.token_count() == _fresh_count(c.messages)


def test_token_count_is_thread_safe_against_trim():
    """snapshot() calls token_count() from the TRANSPORT thread while the worker
    thread's add_message→trim() iterates/rebuilds the same _tok_memo dict — the
    unguarded interleave raised 'dictionary changed size during iteration'."""
    import threading

    c = ContextBuffer(max_tokens=400, trim_buffer_tokens=0)
    errors: list[BaseException] = []
    stop = threading.Event()

    def counter():
        try:
            while not stop.is_set():
                c.token_count()
        except BaseException as exc:  # noqa: BLE001 - the assertion target
            errors.append(exc)

    t = threading.Thread(target=counter)
    t.start()
    try:
        # Every add trims (window ~400 tokens), so the memo is constantly pruned
        # while the counter thread constantly rebuilds it.
        for i in range(3000):
            c.add("user", f"message number {i} with some padding text attached")
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert c.token_count() == _fresh_count(c.messages)


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
    from chara.protocol import Notice, TextDelta, ThinkDelta, ToolEnd

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
    from chara.config import LLMConfig
    from chara.core.llm import LLMClient

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
    from chara.config import LLMConfig
    from chara.core.llm import LLMClient

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
    from chara.core.llm import _model_keeps_thought_signature
    assert _model_keeps_thought_signature("google/gemini-3-pro")
    assert _model_keeps_thought_signature("gemma-3-27b")
    assert not _model_keeps_thought_signature("deepseek/deepseek-v4")
    assert not _model_keeps_thought_signature(None)


# ---- refuse a KNOWN-too-small live model (apple-to-apple with hermes) -----------

def test_refuses_known_small_context_live_model(agent, monkeypatch):
    """A live model whose REAL window is known to be < 64K is refused (hermes
    raises at init below MINIMUM_CONTEXT_LENGTH). Only when DETERMINED — an
    unmeasured/offline model that fell back to DEFAULT_WINDOW is allowed."""
    from chara.core import providers

    a = agent(toolpack="")
    monkeypatch.setattr(a.llm, "is_live", lambda: True)

    # determined + below floor → refuse
    monkeypatch.setattr(providers, "context_window_resolved", lambda *x, **k: (32_000, True))
    a._ctx_window_key = None  # force recompute
    with pytest.raises(ValueError, match="below the"):
        a.context_limit()

    # determined + at/above floor → fine
    monkeypatch.setattr(providers, "context_window_resolved", lambda *x, **k: (128_000, True))
    a._ctx_window_key = None
    assert a.context_limit() == 128_000

    # UNDETERMINED small window (offline/unknown) → allowed, never refused
    monkeypatch.setattr(providers, "context_window_resolved", lambda *x, **k: (32_000, False))
    a._ctx_window_key = None
    assert a.context_limit() == 32_000


def test_mock_provider_never_refused_for_small_window(agent, monkeypatch):
    """A non-live (mock/offline) agent is never refused, even at a small window —
    the guard is live-only."""
    from chara.core import providers
    a = agent(toolpack="")  # mock provider → is_live() False
    monkeypatch.setattr(providers, "context_window_resolved", lambda *x, **k: (8_000, True))
    a._ctx_window_key = None
    assert a.context_limit() == 8_000  # no raise
