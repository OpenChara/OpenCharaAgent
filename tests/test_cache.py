"""Anthropic prompt-cache breakpoints (core/cache.py) — the system_and_3 layout.

Pure unit tests: cache.py touches no import-time config globals, so no env dance.
"""
import copy

from chara.core.cache import apply_cache_control, cache_policy


def _markers(messages):
    """Count cache_control markers across envelopes + nested content parts."""
    n = 0
    for m in messages:
        if "cache_control" in m:
            n += 1
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for p in c if isinstance(p, dict) and "cache_control" in p)
    return n


# ---- cache_policy (route + model detection) -----------------------------------

def test_policy_native_anthropic():
    assert cache_policy("https://api.anthropic.com", "claude-sonnet-4") == (True, True)


def test_policy_openrouter_claude_is_envelope():
    assert cache_policy("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4") == (True, False)


def test_policy_openrouter_nonclaude_off():
    assert cache_policy("https://openrouter.ai/api/v1", "deepseek/deepseek-v4") == (False, False)


def test_policy_direct_deepseek_off():
    assert cache_policy("https://api.deepseek.com", "deepseek-chat") == (False, False)


def test_policy_empty_host_off():
    assert cache_policy("", "claude-3") == (False, False)


# ---- apply_cache_control (the layout) -----------------------------------------

def test_empty_list():
    assert apply_cache_control([]) == []


def test_system_plus_3_of_5():
    msgs = [{"role": "system", "content": "S"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(5)
    ]
    out = apply_cache_control(msgs)
    # exactly 4 markers: system + last 3 non-system
    assert _markers(out) == 4
    # the 2 oldest non-system messages carry none
    assert "cache_control" not in out[1] and not isinstance(out[1]["content"], list)
    assert "cache_control" not in out[2] and not isinstance(out[2]["content"], list)


def test_no_system_uses_last_4_nonsystem():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    out = apply_cache_control(msgs)
    assert _markers(out) == 4
    # none on a system (there is none); markers on the last 4
    assert not isinstance(out[0]["content"], list) and not isinstance(out[1]["content"], list)


def test_trailing_volatile_system_skipped():
    """OpenCharaAgent puts volatile-tail blocks as TRAILING system messages — they
    must not eat a breakpoint; the 3 land on the last 3 non-system turns."""
    msgs = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "system", "content": "env volatile"},
        {"role": "system", "content": "post-history"},
    ]
    out = apply_cache_control(msgs)
    assert _markers(out) == 4
    # trailing system messages untouched
    assert "cache_control" not in out[4] and not isinstance(out[4]["content"], list)
    assert "cache_control" not in out[5] and not isinstance(out[5]["content"], list)
    # the leading stable system is marked
    assert isinstance(out[0]["content"], list) and "cache_control" in out[0]["content"][-1]


def test_string_content_promoted_to_text_part():
    msgs = [{"role": "user", "content": "hello"}]
    out = apply_cache_control(msgs)
    assert out[0]["content"] == [{"type": "text", "text": "hello",
                                  "cache_control": {"type": "ephemeral"}}]


def test_list_content_marks_last_part():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
    out = apply_cache_control(msgs)
    assert "cache_control" not in out[0]["content"][0]
    assert out[0]["content"][1]["cache_control"] == {"type": "ephemeral"}


def test_tool_role_envelope_native_vs_envelope():
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "result"}]
    # envelope layout (native=False): tool messages get NO marker
    out = apply_cache_control(msgs, native=False)
    assert "cache_control" not in out[0]
    assert out[0]["content"] == "result"  # untouched
    # native layout: envelope marker
    out2 = apply_cache_control(msgs, native=True)
    assert out2[0]["cache_control"] == {"type": "ephemeral"}


def test_none_and_empty_content_on_envelope():
    for content in (None, ""):
        msgs = [{"role": "assistant", "content": content, "tool_calls": [{"id": "x"}]}]
        out = apply_cache_control(msgs)
        assert out[0]["cache_control"] == {"type": "ephemeral"}


def test_ttl_1h_marker():
    msgs = [{"role": "user", "content": "hi"}]
    out = apply_cache_control(msgs, ttl="1h")
    assert out[0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_input_is_not_mutated():
    msgs = [{"role": "system", "content": "S"},
            {"role": "user", "content": "u"}]
    before = copy.deepcopy(msgs)
    out = apply_cache_control(msgs)
    assert msgs == before        # caller's list untouched
    assert out is not msgs       # distinct list


def test_marked_messages_are_copies_unmarked_are_shared():
    # Copy-on-write contract: a message we annotate must be a deep copy (so the
    # caller's original — including nested content lists — is never mutated), but
    # an UNmarked message may be returned by reference (the perf win).
    big = "x" * 50_000
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": [{"type": "text", "text": "old"}]},  # unmarked (oldest)
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "user", "content": [{"type": "text", "text": big}]},     # marked (last)
    ]
    before = copy.deepcopy(msgs)
    out = apply_cache_control(msgs)
    assert msgs == before                 # caller untouched, nested lists intact
    # the oldest non-system message is unmarked → shared by reference (no copy)
    assert out[1] is msgs[1]
    # marked messages are fresh copies, not the caller's objects
    assert out[0] is not msgs[0]
    assert out[4] is not msgs[4]
    assert out[4]["content"] is not msgs[4]["content"]


def test_output_byte_identical_to_full_deepcopy_path():
    # The optimized COW output must equal what the old `copy.deepcopy(whole list)`
    # path produced — same markers in the same places.
    msgs = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [{"type": "text", "text": "u2a"},
                                     {"type": "text", "text": "u2b"}]},
        {"role": "system", "content": "env volatile"},
    ]

    def reference(api_messages, ttl="5m", native=False):
        from chara.core.cache import _apply_marker, _build_marker
        m = copy.deepcopy(api_messages)
        marker = _build_marker(ttl)
        used = 0
        if m and m[0].get("role") == "system":
            _apply_marker(m[0], marker, native)
            used += 1
        non_sys = [i for i in range(len(m)) if m[i].get("role") != "system"]
        for idx in non_sys[-(4 - used):]:
            _apply_marker(m[idx], marker, native)
        return m

    for native in (False, True):
        for ttl in ("5m", "1h"):
            assert apply_cache_control(copy.deepcopy(msgs), ttl=ttl, native=native) \
                == reference(msgs, ttl=ttl, native=native)
