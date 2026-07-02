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
    return compaction.compact(ctx, llm)


def _fill(ctx, n, chars=500):
    for i in range(n):
        ctx.add("user" if i % 2 == 0 else "assistant", "x" * chars)


@pytest.fixture(autouse=True)
def _low_floor(monkeypatch):
    """hermes' 64K MINIMUM_CONTEXT_LENGTH floor would make every tiny-window
    unit test need 64K+ tokens to trigger. Drop it to 0 here so these tests
    exercise the 0.50 ratio at small windows; the floor itself is covered by
    test_threshold_floor_and_ratio / test_subfloor_window_never_triggers."""
    monkeypatch.setattr(compaction, "MINIMUM_CONTEXT_LENGTH", 0)


def test_should_compact_threshold():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 20, 500)  # ~2540 tokens
    assert _sc(ctx, 1000, FakeLLM())          # 2540 >= 0.50*1000 = 500
    assert not _sc(ctx, 1_000_000, FakeLLM())  # well under 50%
    assert not _sc(ctx, 1000, FakeLLM(live=False))  # offline


def test_threshold_floor_and_ratio(monkeypatch):
    """Apple-to-apple with hermes: threshold = max(0.50*window, 64K)."""
    monkeypatch.setattr(compaction, "MINIMUM_CONTEXT_LENGTH", 64_000)

    def thr(window):
        return compaction._threshold_tokens(ContextBuffer(max_tokens=window))

    assert thr(1_000_000) == 500_000   # 50% of a 1M window
    assert thr(200_000) == 100_000     # 50%
    assert thr(128_000) == 64_000      # 50% (64K) == floor
    assert thr(96_000) == 64_000       # 50% (48K) floored up to 64K
    assert thr(32_000) == 64_000       # floor exceeds the window → unreachable


def test_subfloor_window_never_triggers(monkeypatch):
    """A window below the 64K floor never reaches the threshold → compaction
    never fires; trim() is the backstop (hermes refuses such models, we don't)."""
    monkeypatch.setattr(compaction, "MINIMUM_CONTEXT_LENGTH", 64_000)
    ctx = ContextBuffer(max_tokens=32_000, trim_buffer_tokens=2000)
    _fill(ctx, 40, 800)  # plenty of tokens, but the 32K window < 64K floor
    assert compaction.should_compact(ctx, FakeLLM()) is False


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
    # A prior summary (kind='summary') in the head drives the ITERATIVE-update
    # framing: it's pulled out and passed as PREVIOUS SUMMARY (prefix stripped),
    # while only the new turns are serialized. Iterative update for free.
    head = [
        {"role": "system", "content": compaction.SUMMARY_PREFIX + "\nolder facts here", "kind": "summary"},
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "done"},
    ]
    llm = FakeLLM(summary="UPDATED")
    seen = {}
    orig = llm.raw_complete

    def capture(messages, max_tokens=1024, timeout=60.0):
        seen["prompt"] = messages[0]["content"]
        return orig(messages, max_tokens)

    llm.raw_complete = capture
    out = compaction._summarize(head, 4000, llm)
    assert out == "UPDATED"
    p = seen["prompt"]
    assert "PREVIOUS SUMMARY:" in p                 # iterative framing chosen
    assert "older facts here" in p                  # prior body folded in
    assert "do the thing" in p and "done" in p      # new turns included
    # the prior summary's REFERENCE-ONLY prefix is stripped so it doesn't nest
    assert p.count("[CONTEXT COMPACTION — REFERENCE ONLY]") == 0


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
    assert compaction.compact(ctx, llm, force=True) is False  # but it TRIED
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
        # One filter-safe user message now carries the whole structured prompt.
        seen["convo"] = messages[0]["content"]
        return orig(messages, max_tokens)

    llm.raw_complete = capture
    assert _cp(ctx, 4000, llm) is True
    # the specific ask must not be in the summarized turns (the template
    # boilerplate itself contains the word "IMPORTANT", so match the phrase)
    assert "refactor the parser next" not in seen["convo"]
    assert any(m.get("role") == "user" and "refactor the parser next" in str(m.get("content"))
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


# ---- the structured summary template + REFERENCE-ONLY handoff (apple-to-apple) ----------


def _capture_prompt(ctx, window, summary="S"):
    llm = FakeLLM(summary=summary)
    seen = {}
    orig = llm.raw_complete

    def capture(messages, max_tokens=1024, timeout=60.0):
        seen["prompt"] = messages[0]["content"]
        return orig(messages, max_tokens)

    llm.raw_complete = capture
    _cp(ctx, window, llm)
    return seen.get("prompt", "")


def test_summary_carries_reference_only_prefix():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    assert _cp(ctx, 4000, FakeLLM(summary="BODY")) is True
    head = ctx.messages[0]
    assert head["kind"] == "summary"
    assert head["content"].startswith(compaction.SUMMARY_PREFIX)
    assert "REFERENCE ONLY" in head["content"] and "latest message WINS" in head["content"]


def test_first_compaction_uses_structured_template():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    prompt = _capture_prompt(ctx, 4000)
    for section in ("## Active Task", "## Completed Actions", "## Pending Asks",
                    "## Critical Context"):
        assert section in prompt
    assert "summarization agent creating a context checkpoint" in prompt
    assert "PREVIOUS SUMMARY:" not in prompt   # first compaction, not iterative


def test_temporal_anchoring_present_and_dated(monkeypatch):
    monkeypatch.setattr(compaction, "_today_str", lambda: "2026-06-18")
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    prompt = _capture_prompt(ctx, 4000)
    assert "TEMPORAL ANCHORING: The current date is 2026-06-18" in prompt
    # absent when the clock is unavailable
    monkeypatch.setattr(compaction, "_today_str", lambda: "")
    ctx2 = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx2, 40, 500)
    assert "TEMPORAL ANCHORING" not in _capture_prompt(ctx2, 4000)


def test_strip_summary_prefix_handles_current_and_legacy():
    assert compaction._strip_summary_prefix(compaction.SUMMARY_PREFIX + "\nbody") == "body"
    assert compaction._strip_summary_prefix(compaction._LEGACY_HEADER + "\nold body") == "old body"
    assert compaction._strip_summary_prefix("no prefix here") == "no prefix here"


def test_no_brand_strings_in_model_facing_summary_text():
    banned = ("hermes", "Hermes", "the VM", "MEMORY.md", "USER.md")
    surfaces = [
        compaction.SUMMARY_PREFIX,
        compaction._SUMMARIZER_PREAMBLE,
        compaction._template_sections(1024, "2026-06-18"),
        compaction._first_compaction_prompt("X", 1024, "2026-06-18"),
        compaction._iterative_update_prompt("P", "X", 1024, "2026-06-18"),
    ]
    for s in surfaces:
        for b in banned:
            assert b not in s, f"{b!r} leaked into model-facing summary text"


# ---- prune old tool outputs in the LIVE window (audit #13) ------------------------------


def _tool_pair(ctx, call_id, name, content):
    ctx.add_message({"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]})
    ctx.add_message({"role": "tool", "tool_call_id": call_id, "content": content})


def test_live_prune_one_lines_old_tool_results():
    # A fat old tool result in the live window is collapsed to a factual one-liner
    # — its full content stays on disk, not in the in-memory API view.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _tool_pair(ctx, "c1", "terminal", "OUTPUT\n" * 4000)  # ~28 KB, old
    _fill(ctx, 10, 800)  # push it well past the protected tail
    assert compaction.prune_live_tool_outputs(ctx) is True
    tool_msg = next(m for m in ctx.messages if m.get("tool_call_id") == "c1")
    assert tool_msg["content"].startswith("[terminal output pruned")
    assert len(tool_msg["content"]) < 300  # the 28 KB payload is gone from the live window


def test_live_prune_keeps_recent_tail_results_verbatim():
    # A tool result inside the protected tail must NOT be pruned (it is recent work).
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 4, 200)
    _tool_pair(ctx, "fresh", "read_file", "RECENT\n" * 400)  # newest, in the tail
    before = ctx.messages[-1]["content"]
    compaction.prune_live_tool_outputs(ctx)
    assert ctx.messages[-1]["content"] == before  # untouched


def test_live_prune_dedups_identical_old_results():
    # The same file read twice: the OLDER copy collapses to a back-reference,
    # the newer one is one-lined (both outside the tail) — no 6 KB duplicate.
    ctx = ContextBuffer(max_tokens=10_000_000)
    body = "SAME CONTENT\n" * 1000
    _tool_pair(ctx, "a", "read_file", body)
    _tool_pair(ctx, "b", "read_file", body)  # identical
    _fill(ctx, 12, 800)  # both pushed past the tail
    assert compaction.prune_live_tool_outputs(ctx) is True
    older = next(m for m in ctx.messages if m.get("tool_call_id") == "a")
    assert "duplicate tool output" in older["content"]


def test_live_prune_is_idempotent():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _tool_pair(ctx, "c1", "terminal", "OUT\n" * 4000)
    _fill(ctx, 10, 800)
    assert compaction.prune_live_tool_outputs(ctx) is True
    assert compaction.prune_live_tool_outputs(ctx) is False  # nothing left to prune
    assert len([m for m in ctx.messages if str(m.get("content", "")).startswith("[terminal output pruned")]) == 1


def test_live_prune_noop_on_short_or_no_tool_outputs():
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 20, 500)  # plain chatter, no tool results
    assert compaction.prune_live_tool_outputs(ctx) is False
    ctx2 = ContextBuffer(max_tokens=10_000_000)
    _tool_pair(ctx2, "c1", "read_file", "tiny")  # under the 200-char floor
    _fill(ctx2, 10, 500)
    assert compaction.prune_live_tool_outputs(ctx2) is False


def test_compact_skips_llm_when_live_prune_alone_suffices():
    # If one-lining old tool outputs drops the window back under threshold,
    # compaction returns True WITHOUT spending a summarizer call.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _tool_pair(ctx, "c1", "terminal", "X" * 60_000)  # one giant old result dominates
    _fill(ctx, 6, 300)  # a small recent tail
    llm = FakeLLM(summary="UNUSED")
    assert _cp(ctx, 4000, llm) is True
    assert llm.calls == 0  # no LLM summary needed — pruning alone did it
    assert any(str(m.get("content", "")).startswith("[terminal output pruned") for m in ctx.messages)
    assert not any(m.get("kind") == "summary" for m in ctx.messages)  # no summary row written


def test_live_prune_does_not_persist():
    # Pruning narrows the in-memory view only; like trim(), it must not re-persist.
    persisted = []
    ctx = ContextBuffer(max_tokens=10_000_000, persist=persisted.append)
    _tool_pair(ctx, "c1", "terminal", "OUT\n" * 4000)
    _fill(ctx, 10, 800)
    n = len(persisted)
    compaction.prune_live_tool_outputs(ctx)
    assert len(persisted) == n  # zero new persist calls


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


def test_thrash_guard_holds_when_real_usage_exceeds_heuristic(clock):
    # CJK-class scenario: the provider's real prompt_tokens runs far above the
    # char-heuristic window size. The anti-thrash guard records resume_above on
    # the SAME (real) scale that allows() reads — else real > heuristic*1.10
    # clears the backoff every turn and the guard is silently defeated (the
    # burned-a-real-key class of incident). Each _cp/_sc re-freshens usage to
    # mimic the per-turn stream that refreshes the provider count.
    ctx = ContextBuffer(max_tokens=10_000_000)
    _fill(ctx, 40, 500)
    llm = FakeUsageLLM(prompt_tokens=50_000, summary="y" * 12000)  # fat summary → ineffective
    assert _cp(ctx, 4000, llm) is True    # ineffective #1
    llm.usage_fresh = True
    assert _cp(ctx, 4000, llm) is False   # nothing foldable left → ineffective #2
    llm.usage_fresh = True
    # Two ineffective passes: the guard MUST disengage despite real >> heuristic.
    assert _sc(ctx, 4000, llm) is False
    assert _cp(ctx, 4000, llm) is False
    assert llm.calls == 1                 # no further wasted summary calls


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


# ---- todo list survives compaction (hermes parity) --------------------------------------


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.session.settings import Settings
    from lunamoth.core.agent import LunaMothAgent

    return LunaMothAgent(Settings(character_path="", toolpack="sandbox"))


def test_todo_injection_renders_active_items(agent):
    # No todo used yet → nothing to inject.
    assert agent.tools.todo_injection() is None
    agent.tools.call("todo", todos=[
        {"id": "1", "content": "draft the nocturne", "status": "in_progress"},
        {"id": "2", "content": "tune the strings", "status": "pending"},
        {"id": "3", "content": "warm up", "status": "completed"},
    ])
    block = agent.tools.todo_injection()
    assert block is not None
    assert "draft the nocturne" in block and "tune the strings" in block
    assert "warm up" not in block  # completed items are dropped (no re-doing finished work)


def test_compaction_reinjects_active_todo(agent):
    from lunamoth.core.agent import Session
    agent.tools.call("todo", todos=[
        {"id": "1", "content": "draft the nocturne", "status": "in_progress"},
    ])
    session = Session()
    session.context.add("user", "hi")
    agent._reinject_todo(session)
    last = session.context.messages[-1]
    assert last["kind"] == "todo"
    assert "draft the nocturne" in last["content"]


def test_compaction_without_todo_injects_nothing(agent):
    from lunamoth.core.agent import Session
    session = Session()
    session.context.add("user", "hi")
    before = len(session.context.messages)
    agent._reinject_todo(session)  # no todo used → no-op
    assert len(session.context.messages) == before


def test_compaction_tail_reappend_does_not_duplicate_display_or_export(tmp_path):
    # The persisted tail re-append (kind='replay') exists ONLY so load() can
    # rebuild "latest summary + tail". Display and export read the full epoch —
    # they must skip replay rows, or the tail shows twice after every compaction.
    from lunamoth.core.transcript import TranscriptStore

    store = TranscriptStore(tmp_path / "transcript.db")
    ctx = ContextBuffer(max_tokens=10_000_000)
    ctx.persist = store.append_message
    llm = FakeLLM(summary="CHECKPOINT")
    _fill(ctx, 40, 500)  # add_message persists through ctx.persist, like the live agent
    n_display_before = len(store.load_display())
    assert _cp(ctx, 4000, llm) is True
    tail = [m for m in ctx.messages if m.get("kind") != "summary"]

    # Restore path: latest summary + the tail, exactly the live window.
    restored = store.load()
    assert restored[0].get("kind") == "summary"
    assert [m["content"] for m in restored[1:]] == [m["content"] for m in tail]

    # Display: one summary row added, NOTHING duplicated.
    display = store.load_display()
    assert len(display) == n_display_before + 1
    assert sum(1 for m in display if m.get("kind") == "summary") == 1

    # Export: no replay rows leak.
    out = tmp_path / "export.jsonl"
    lines = store.export_jsonl(out)
    assert lines == n_display_before + 1
    assert "replay" not in {__import__("json").loads(l)["kind"] for l in out.read_text().splitlines()}


def test_replay_rows_survive_a_second_compaction(tmp_path):
    # Re-compacting a restored window must keep working: the replay rows load
    # as ordinary dicts and fold/re-append again without growing the display.
    from lunamoth.core.transcript import TranscriptStore

    store = TranscriptStore(tmp_path / "transcript.db")
    ctx = ContextBuffer(max_tokens=10_000_000)
    ctx.persist = store.append_message
    llm = FakeLLM(summary="CHECKPOINT")
    _fill(ctx, 40, 500)
    assert _cp(ctx, 4000, llm) is True
    display_after_first = len(store.load_display())

    # Simulate restart: rebuild the live window from disk, keep talking, compact again.
    ctx2 = ContextBuffer(max_tokens=10_000_000)
    ctx2.restore(store.load())
    ctx2.persist = store.append_message
    for i in range(20):
        ctx2.add("user" if i % 2 == 0 else "assistant", "y" * 500)
    assert _cp(ctx2, 4000, llm) is True
    display = store.load_display()
    # first compaction display + 20 new rows + 1 new summary row, no tail dupes
    assert len(display) == display_after_first + 21
    assert sum(1 for m in display if m.get("kind") == "summary") == 2
