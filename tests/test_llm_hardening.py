"""Hermes-parity hardening of the streaming client (audit #2/#7/#8/#9):
tool-args JSON repair, lone-surrogate scrubbing, real usage capture, and the
announced step-budget exhaustion. All offline — fake streams, no sleeps."""
import json

from lunamoth.config import LLMConfig
from lunamoth.core.llm import LLMClient, _repair_tool_args


def _client():
    return LLMClient(LLMConfig(provider="openai_compatible", base_url="https://x.test/v1", model="m"))


def _drive(gen):
    """Drive a generator that returns a value; collect (events, return value)."""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


# ---- audit #2: tool-call argument repair -------------------------------------------------


def test_valid_args_pass_byte_identical():
    raw = '{"a":  1, "路径": "工坊/诗.txt"}'  # odd spacing + CJK must survive untouched
    assert _repair_tool_args(raw, "t") == raw


def test_trailing_comma_stripped():
    fixed = _repair_tool_args('{"a": 1,}', "t")
    assert json.loads(fixed) == {"a": 1}


def test_unclosed_structures_closed():
    fixed = _repair_tool_args('{"a": [1, 2', "t")
    assert json.loads(fixed) == {"a": [1, 2]}


def test_excess_closers_popped():
    fixed = _repair_tool_args('{"a": 1}}}', "t")
    assert json.loads(fixed) == {"a": 1}


def test_literal_control_chars_reserialized():
    # strict=False accepts a literal tab inside a string (hermes #12068, the
    # most common local-model case) and re-serializes to wire-valid JSON.
    fixed = _repair_tool_args('{"a": "x\ty"}', "t")
    assert json.loads(fixed) == {"a": "x\ty"}
    assert "\t" not in fixed


def test_control_chars_plus_structural_damage():
    # Pass 3: control chars combined with an unclosed brace.
    fixed = _repair_tool_args('{"a": "x\ny", "b": 1', "t")
    assert json.loads(fixed) == {"a": "x\ny", "b": 1}


def test_python_none_and_empty_become_empty_object():
    assert _repair_tool_args("None", "t") == "{}"
    assert _repair_tool_args("", "t") == "{}"
    assert _repair_tool_args("   ", "t") == "{}"


def test_unrepairable_garbage_degrades_to_empty_object():
    # "{}" feeds the gateway's honest missing-args error instead of crashing.
    assert _repair_tool_args("<<<not json>>>", "t") == "{}"


def test_cjk_survives_reserialization():
    fixed = _repair_tool_args('{"text": "你好\t世界"}', "t")
    assert json.loads(fixed) == {"text": "你好\t世界"}
    assert "你好" in fixed  # ensure_ascii=False — no \uXXXX token bloat


def test_replayed_history_args_repaired_without_mutating_context():
    broken = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls",'}},
    ]}
    context = [broken, {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    messages = _client()._messages("hi", context, ["sys"], [])
    sent = next(m for m in messages if m.get("tool_calls"))
    assert json.loads(sent["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}
    # The durable history is untouched: copy-on-repair, per-request view only.
    assert broken["tool_calls"][0]["function"]["arguments"] == '{"command": "ls",'


def test_replayed_valid_args_stay_byte_identical():
    raw = '{"a": 1}'
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": raw}},
    ]}
    messages = _client()._messages("hi", [msg], ["sys"], [])
    sent = next(m for m in messages if m.get("tool_calls"))
    assert sent["tool_calls"][0]["function"]["arguments"] is raw  # not even copied


# ---- fake SSE stream harness -------------------------------------------------------------


class FakeResp:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_stream(monkeypatch, chunks):
    lines = [b"data: " + json.dumps(c).encode("utf-8") for c in chunks] + [b"data: [DONE]"]

    def fake_connect(self, url, data, timeout):
        return FakeResp(lines)
        yield  # pragma: no cover — generator for `yield from`

    monkeypatch.setattr(LLMClient, "_connect_with_retry", fake_connect)


# ---- audit #7: lone-surrogate sanitization ----------------------------------------------


def test_scrub_surrogates():
    from lunamoth.core.llm import _scrub_surrogates

    assert _scrub_surrogates("a\ud800b\udfffc") == "a�b�c"
    clean = "嗨 😀 plain"
    assert _scrub_surrogates(clean) is clean  # fast no-op path


def test_joiner_preserves_split_astral_pairs():
    from lunamoth.core.llm import _SurrogateJoiner

    j = _SurrogateJoiner()
    assert j.feed("ok\ud83d") == "ok"          # high surrogate held back
    assert j.feed("\ude00!") == "😀!"          # rejoined into the real emoji
    assert j.flush() == ""


def test_joiner_scrubs_true_lone_surrogates():
    from lunamoth.core.llm import _SurrogateJoiner

    j = _SurrogateJoiner()
    assert j.feed("bad\ud800") == "bad"
    assert j.feed("x") == "�x"            # high not followed by a low → scrubbed
    assert j.feed("\ude00y") == "�y"      # naked low surrogate → scrubbed
    assert j.feed("end\udbff") == "end"
    assert j.flush() == "�"               # stream ended on a held char


def test_stream_text_and_reasoning_are_surrogate_free(monkeypatch):
    from lunamoth.protocol import TextDelta, ThinkDelta

    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"reasoning_content": "hm\ud800"}}]},
        {"choices": [{"delta": {"reasoning_content": "m"}}]},
        {"choices": [{"delta": {"content": "ok\ud83d"}}]},   # emoji split across deltas
        {"choices": [{"delta": {"content": "\ude00!"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
    ])
    out: list = []
    events, (_tools, think, _finish, _rd) = _drive(_client()._stream_turn([], None, out))
    text = "".join(out)
    assert text == "ok😀!"                      # split pair came out whole
    assert think == "hm�m"                 # lone surrogate scrubbed from reasoning
    for e in events:
        if isinstance(e, (TextDelta, ThinkDelta)):
            # The exact crash the audit names: ensure_ascii=False json.dumps
            # on the live event path must never see a lone surrogate.
            json.dumps(e.text, ensure_ascii=False).encode("utf-8")


def test_stream_ending_on_held_surrogate_flushes_scrubbed(monkeypatch):
    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"content": "end\ud83d"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
    ])
    out: list = []
    _drive(_client()._stream_turn([], None, out))
    assert "".join(out) == "end�"


def test_tool_args_surrogates_scrubbed(monkeypatch):
    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "speak", "arguments": '{"text": "hi\ud800"}'}},
        ]}}]},
        {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
    ])
    _events, (tool_calls, _think, _finish, _rd) = _drive(_client()._stream_turn([], None, []))
    args = tool_calls[0]["function"]["arguments"]
    json.dumps(args, ensure_ascii=False).encode("utf-8")  # must not raise
    assert json.loads(args) == {"text": "hi�"}


def test_plain_stream_path_is_surrogate_free(monkeypatch):
    from lunamoth.protocol import TextDelta

    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"content": "a\ud800"}}]},
        {"choices": [{"delta": {"content": "b"}}]},
    ])
    events = list(_client()._openai_compatible_stream("hi", [], ["sys"], []))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "a�b"
    for e in events:
        json.dumps(e.text, ensure_ascii=False).encode("utf-8")


def test_stream_end_args_repair(monkeypatch):
    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "terminal", "arguments": '{"command": "ls"'}},
        ]}}]},
        {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
    ])
    out: list = []
    _events, (tool_calls, _think, finish, _rd) = _drive(_client()._stream_turn([], None, out))
    assert finish == "tool_calls"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"command": "ls"}


# ---- audit #8: real usage capture --------------------------------------------------------


def test_usage_captured_from_final_chunk_with_empty_choices(monkeypatch):
    # OpenAI-style usage chunk: "choices": [] — must be captured, not crash.
    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
        {"choices": [], "usage": {"prompt_tokens": 1234, "completion_tokens": 5, "total_tokens": 1239}},
    ])
    c = _client()
    assert c.last_prompt_tokens == 0 and not c.usage_fresh
    out: list = []
    _drive(c._stream_turn([], None, out))
    assert "".join(out) == "hi"
    assert c.last_prompt_tokens == 1234
    assert c.usage_fresh
    assert c.last_usage["total_tokens"] == 1239


def test_usage_captured_on_plain_stream_and_zero_placeholders_ignored(monkeypatch):
    _patch_stream(monkeypatch, [
        {"choices": [{"delta": {"content": "a"}}], "usage": {"prompt_tokens": 0}},  # placeholder
        {"choices": [{"delta": {"content": "b"}}], "usage": {"prompt_tokens": 77}},
    ])
    c = _client()
    list(c._openai_compatible_stream("hi", [], ["sys"], []))
    assert c.last_prompt_tokens == 77 and c.usage_fresh


def test_mark_usage_stale():
    c = _client()
    c._note_usage({"prompt_tokens": 50})
    assert c.usage_fresh
    c.mark_usage_stale()
    assert not c.usage_fresh
    assert c.last_prompt_tokens == 50  # numbers stay readable for diagnostics


# ---- audit #9: step-budget exhaustion is announced ---------------------------------------


def _endless_tool_turns(monkeypatch):
    """Every fake turn calls a tool — the loop can only stop on max_steps."""
    def fake_stream_turn(self, messages, tools, text_out, reasoning=None, channel="say"):
        return ([{"id": "c", "type": "function",
                  "function": {"name": "terminal", "arguments": "{}"}}], "", "tool_calls", [])
        yield  # pragma: no cover — generator for `yield from`

    monkeypatch.setattr(LLMClient, "_stream_turn", fake_stream_turn)


def test_step_budget_exhaustion_yields_notice_and_context_marker(monkeypatch):
    from lunamoth.protocol import Notice

    _endless_tool_turns(monkeypatch)
    recorded: list = []
    events = list(_client().stream_agent(
        "go", [], ["sys"], [], tools=[{"type": "function"}],
        execute=lambda tc: {"display": "", "content": "ok", "ok": True},
        record=recorded.append, max_steps=2,
    ))
    budget = [e for e in events if isinstance(e, Notice) and e.kind == "budget"]
    assert len(budget) == 1 and "2 tool steps" in budget[0].text
    # The durable context carries an explicit marker as the last message, so
    # the next turn knows the loop was cut, not completed.
    assert recorded[-1]["role"] == "system"
    assert "tool-step budget (2 steps)" in recorded[-1]["content"]
    # Exactly max_steps tool rounds ran before the stop.
    assert sum(1 for m in recorded if m.get("tool_calls")) == 2


def test_completed_turn_emits_no_budget_notice(monkeypatch):
    from lunamoth.protocol import Notice

    def one_turn(self, messages, tools, text_out, reasoning=None, channel="say"):
        text_out.append("done.")
        return ([], "", "stop", [])
        yield  # pragma: no cover

    monkeypatch.setattr(LLMClient, "_stream_turn", one_turn)
    recorded: list = []
    events = list(_client().stream_agent(
        "go", [], ["sys"], [], tools=None, execute=lambda tc: {},
        record=recorded.append, max_steps=2,
    ))
    assert not any(isinstance(e, Notice) and e.kind == "budget" for e in events)
    assert all("tool-step budget" not in str(m.get("content")) for m in recorded)
