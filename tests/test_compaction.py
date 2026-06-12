import pytest

from lunamoth.core import compaction
from lunamoth.core.context import ContextBuffer


class FakeLLM:
    def __init__(self, live=True, summary="OLD SUMMARY"):
        self._live, self._summary, self.calls = live, summary, 0

    def is_live(self):
        return self._live

    def raw_complete(self, messages, max_tokens=1024, timeout=60.0):
        self.calls += 1
        return self._summary



def _sc(ctx, window, llm):
    ctx.max_tokens, ctx.trim_buffer_tokens = window, 0
    return compaction.should_compact(ctx, llm)


def _cp(ctx, window, llm):
    ctx.max_tokens, ctx.trim_buffer_tokens = window, 0
    return compaction.compact(ctx, "en", llm)


def _fill(ctx, n, chars=500):
    for i in range(n):
        ctx.add("user" if i % 2 == 0 else "assistant", "x" * chars)


def test_should_compact_threshold():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 20, 500)  # ~2540 tokens
    assert _sc(ctx, 1000, FakeLLM())          # 2540 >= 750
    assert not _sc(ctx, 1_000_000, FakeLLM())  # well under 75%
    assert not _sc(ctx, 1000, FakeLLM(live=False))  # offline


def test_compact_replaces_head_keeps_tail():
    ctx = ContextBuffer(max_tokens=10_000_000)  # disable trim so we test compaction alone
    llm = FakeLLM(summary="OLD SUMMARY TEXT")
    _fill(ctx, 40, 500)
    n_before = len(ctx.messages)
    assert _cp(ctx, 4000, llm) is True
    assert llm.calls == 1
    assert ctx.messages[0]["kind"] == "summary"
    assert "OLD SUMMARY TEXT" in ctx.messages[0]["content"]
    assert len(ctx.messages) < n_before                  # head collapsed
    assert ctx.messages[-1]["content"] == "x" * 500      # tail kept verbatim


def test_iterative_summary_folds_previous():
    # A summary message (kind='summary') sits at messages[0] after compaction; the
    # next compaction includes it in the head, so _serialize labels it as the prior
    # summary and the model folds it into the new one — iterative update for free.
    head = [
        {"role": "system", "content": "older facts here", "kind": "summary"},
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "done"},
    ]
    serialized = compaction._serialize(head)
    assert "earlier summary" in serialized and "older facts here" in serialized


def test_serialize_prunes_tool_outputs_without_mutating_head():
    head = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "line\n" * 200},
    ]
    serialized = compaction._serialize(head)
    assert "terminal output pruned" in serialized
    assert len(serialized) < len(head[1]["content"])
    assert head[1]["content"] == "line\n" * 200  # pruning is only for the summary prompt copy


def test_offline_and_empty_summary_are_noops():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    n = len(ctx.messages)
    assert _cp(ctx, 4000, FakeLLM(live=False)) is False
    assert _cp(ctx, 4000, FakeLLM(summary='')) is False
    assert len(ctx.messages) == n  # unchanged


# ---- anti-thrash guard + failure cooldown (audit #10) -----------------------------------


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(compaction, "_now", lambda: t["now"])
    return t


def test_summarizer_failure_enters_cooldown_then_recovers(clock):
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    llm = FakeLLM(summary="")  # the summary call fails (raw_complete degrades to "")
    assert _cp(ctx, 4000, llm) is False
    assert llm.calls == 1
    # Cooldown: over threshold, but no retry — not one wasted LLM call per turn.
    assert _sc(ctx, 4000, llm) is False
    assert _cp(ctx, 4000, llm) is False
    assert llm.calls == 1
    clock["now"] += 301  # cooldown expires
    assert _sc(ctx, 4000, llm) is True
    assert _cp(ctx, 4000, llm) is False  # tries again (and fails again)
    assert llm.calls == 2


def test_force_bypasses_the_failure_cooldown(clock):
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    llm = FakeLLM(summary="")
    assert _cp(ctx, 4000, llm) is False  # enters cooldown
    ctx.max_tokens, ctx.trim_buffer_tokens = 4000, 0
    assert compaction.compact(ctx, "en", llm, force=True) is False  # but it TRIED
    assert llm.calls == 2  # the operator's explicit /compact is never silently ignored


def test_ineffective_compactions_back_off_until_window_regrows(clock):
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    # A summary nearly as fat as the head it replaces: shrink < 10%.
    llm = FakeLLM(summary="y" * 12000)
    assert _cp(ctx, 4000, llm) is True   # ineffective pass #1 (fat summary)
    assert _cp(ctx, 4000, llm) is False  # nothing foldable left — ineffective #2
    assert llm.calls == 1
    # Two consecutive ineffective passes — the guard disengages compaction.
    assert _sc(ctx, 4000, llm) is False
    assert _cp(ctx, 4000, llm) is False
    assert llm.calls == 1  # no more wasted summary calls
    # The window genuinely regrows past the step — the guard re-arms.
    ctx.max_tokens = 10_000_000  # let it grow (trim would cap it at the window)
    _fill(ctx, 20, 2000)
    assert _sc(ctx, 4000, llm) is True


def test_nothing_to_fold_counts_as_ineffective(clock):
    # Over threshold but the whole transcript fits in the protected tail
    # (hermes scar #40803): without counting it, every turn re-fires a no-op.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 6, 500)  # ~750 tokens: over a 1000-token budget's threshold, under the 2000 tail
    llm = FakeLLM()
    assert _cp(ctx, 1000, llm) is False
    assert _cp(ctx, 1000, llm) is False
    assert llm.calls == 0
    assert _sc(ctx, 1000, llm) is False  # guard engaged after two no-ops


# ---- tail boundary respects tool pairs + the last user message (audit #11) --------------


def test_tail_never_starts_with_orphaned_tool_results():
    # The blind token walk lands the cut ON a tool result; render() would then
    # silently drop it (orphan). The cut must pull back to the parent assistant.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 20, 500)
    ctx.add_message({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c9", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
    ctx.add_message({"role": "tool", "tool_call_id": "c9", "content": "y" * 8000})
    ctx.add("user", "and then?")
    assert _cp(ctx, 4000, FakeLLM(summary="S")) is True
    msgs = ctx.messages
    assert msgs[0]["kind"] == "summary"
    first_tool = next(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    assert msgs[first_tool - 1].get("tool_calls")  # parent kept with its result
    rendered = ctx.render()
    assert any(m.get("tool_call_id") == "c9" for m in rendered)  # nothing dropped


def test_parentless_tool_results_fold_into_the_summary():
    from lunamoth.core.compaction import _align_tail_cut

    msgs = [
        {"role": "user", "content": "a"},
        {"role": "tool", "tool_call_id": "x", "content": "r1"},
        {"role": "tool", "tool_call_id": "y", "content": "r2"},
        {"role": "user", "content": "b"},
    ]
    # No declaring assistant in the window: push forward past the orphans
    # instead of stranding them at the tail head.
    assert _align_tail_cut(msgs, 1) == 3
    # With a parent, pull back to it.
    msgs[0] = {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}
    assert _align_tail_cut(msgs, 1) == 0
    # Non-tool boundary: untouched.
    assert _align_tail_cut(msgs, 3) == 3


def test_last_user_message_stays_out_of_the_summary():
    # Scar #10896: the operator's active request must never be summarized away.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 20, 500)
    ctx.add("user", "IMPORTANT: refactor the parser next")
    ctx.add("assistant", "z" * 8200)  # fat reply puts the token-walk cut AFTER the user msg
    llm = FakeLLM(summary="S")
    seen = {}
    orig = llm.raw_complete

    def capture(messages, max_tokens=1024, timeout=60.0):
        seen["convo"] = messages[1]["content"]
        return orig(messages, max_tokens)

    llm.raw_complete = capture
    assert _cp(ctx, 4000, llm) is True
    assert "IMPORTANT" not in seen["convo"]  # not summarized
    assert any(m.get("role") == "user" and "IMPORTANT" in str(m.get("content"))
               for m in ctx.messages)        # kept verbatim in the tail


def test_anchor_refuses_when_only_the_head_remains():
    # Last user message at index 0/1: anchoring would leave nothing to fold —
    # compact() bails (counted ineffective) instead of summarizing the request.
    ctx = ContextBuffer(max_tokens=10_000_000)
    ctx.add("user", "the one and only ask")
    ctx.add("assistant", "w" * 9000)
    ctx.add("assistant", "v" * 9000)
    ctx.add("assistant", "almost done")
    llm = FakeLLM(summary="S")
    assert _cp(ctx, 4000, llm) is False
    assert llm.calls == 0  # bailed before any summarizer spend


# ---- real usage preferred over the char heuristic (audit #8) ----------------------------


class FakeUsageLLM(FakeLLM):
    def __init__(self, prompt_tokens=0, fresh=True, **kw):
        super().__init__(**kw)
        self.last_prompt_tokens = prompt_tokens
        self.usage_fresh = fresh

    def mark_usage_stale(self):
        self.usage_fresh = False


def test_real_usage_triggers_when_heuristic_undercounts():
    # History heuristic ~1000 tokens (under a 5000-budget threshold of 3750),
    # but the provider measured the WHOLE request at 3900: compaction fires.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 8, 500)
    assert not _sc(ctx, 5000, FakeUsageLLM(prompt_tokens=0))            # heuristic alone: under
    assert _sc(ctx, 5000, FakeUsageLLM(prompt_tokens=3900))             # real usage: over
    assert not _sc(ctx, 5000, FakeUsageLLM(prompt_tokens=3900, fresh=False))  # stale → heuristic


def test_implausible_real_usage_is_distrusted_after_reset():
    # The same client survives /reset: usage captured against the OLD window
    # must not compact a near-empty buffer (heur·4 < real fails the gate).
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 2, 100)  # ~54 tokens of live history
    assert not _sc(ctx, 5000, FakeUsageLLM(prompt_tokens=3900))


def test_compact_marks_usage_stale():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    llm = FakeUsageLLM(prompt_tokens=100_000, summary="TIGHT")
    assert _cp(ctx, 4000, llm) is True
    # The captured usage described the pre-compaction window; after the
    # rewrite the trigger must fall back to the heuristic until a new stream
    # carries fresh usage.
    assert llm.usage_fresh is False


def test_effective_compaction_resets_the_guard(clock):
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    llm = FakeLLM(summary="")
    assert _cp(ctx, 4000, llm) is False  # failure -> cooldown
    clock["now"] += 301
    good = FakeLLM(summary="tight summary")
    assert _cp(ctx, 4000, good) is True  # effective: big shrink
    guard = compaction._guard(ctx)
    assert guard.ineffective == 0 and guard.cooldown_until == 0.0  # both reset on success
