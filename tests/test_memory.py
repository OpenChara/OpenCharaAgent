import pytest

from lunamoth.memory import MemoryLimits, MemoryStore


def test_add_replace_remove(tmp_path):
    m = MemoryStore(tmp_path / "mem")
    m.add("memory", "working on a moth poem")
    m.add("memory", "likes pale blue")
    assert m.entries("memory") == ["working on a moth poem", "likes pale blue"]
    # replace by substring
    m.replace("memory", "moth poem", "finished the moth poem -> poem.txt")
    assert "finished the moth poem -> poem.txt" in m.entries("memory")
    # remove by substring
    m.remove("memory", "pale blue")
    assert m.entries("memory") == ["finished the moth poem -> poem.txt"]
    # empty content on replace deletes
    m.replace("memory", "finished", "")
    assert m.entries("memory") == []


def test_two_stores_are_independent(tmp_path):
    m = MemoryStore(tmp_path / "mem")
    m.add("memory", "note to self")
    m.add("user", "operator prefers zh")
    assert m.entries("memory") == ["note to self"]
    assert m.entries("user") == ["operator prefers zh"]
    snap = m.snapshot()
    assert snap == {"memory": ["note to self"], "user": ["operator prefers zh"]}


def test_persists_across_instances(tmp_path):
    MemoryStore(tmp_path / "mem").add("memory", "durable")
    assert MemoryStore(tmp_path / "mem").entries("memory") == ["durable"]


def test_budget_drops_oldest(tmp_path):
    m = MemoryStore(tmp_path / "mem", MemoryLimits(memory_chars=40, user_chars=40))
    m.add("memory", "a" * 30)
    m.add("memory", "b" * 30)  # both can't fit in 40 chars -> oldest dropped
    entries = m.entries("memory")
    assert entries == ["b" * 30]


def test_default_limits():
    lim = MemoryLimits()
    assert lim.memory_chars == 4000 and lim.user_chars == 2000
    assert lim.cap("memory") == 4000 and lim.cap("user") == 2000


def test_set_limits_grow_is_silent(tmp_path):
    m = MemoryStore(tmp_path / "mem", MemoryLimits(memory_chars=100, user_chars=100))
    m.add("memory", "x" * 80)
    warnings = m.set_limits(MemoryLimits(memory_chars=4000, user_chars=2000))
    assert warnings == []
    assert m.entries("memory") == ["x" * 80]  # nothing discarded on grow


def test_set_limits_shrink_warns_and_discards(tmp_path):
    m = MemoryStore(tmp_path / "mem", MemoryLimits(memory_chars=4000, user_chars=2000))
    m.add("memory", "a" * 100)
    m.add("memory", "b" * 100)
    warnings = m.set_limits(MemoryLimits(memory_chars=120, user_chars=2000))
    assert warnings and "memory" in warnings[0] and "discarded" in warnings[0]
    assert m.entries("memory") == ["b" * 100]  # oldest dropped to fit
    assert m.chars("memory") <= 120


def test_bad_target_and_missing_args(tmp_path):
    m = MemoryStore(tmp_path / "mem")
    with pytest.raises(ValueError):
        m.add("nope", "x")
    with pytest.raises(ValueError):
        m.add("memory", "")          # empty content
    with pytest.raises(ValueError):
        m.replace("memory", "", "x")  # no old_text
    with pytest.raises(ValueError):
        m.remove("memory", "nonexistent")  # no match


def test_frozen_snapshot_decouples_prompt_from_writes(tmp_path):
    # The system-prompt memory block is FROZEN at session start: a mid-session
    # write changes disk + the tool response, but NOT the injected block — until
    # the next session reloads. This is the prompt-cache fix.
    from lunamoth.settings import Settings
    from lunamoth.agent import LunaMothAgent

    a = LunaMothAgent(Settings(provider="mock", character_path="", toolpack="sandbox"))
    a.memory = MemoryStore(tmp_path / "mem")  # hermetic, empty store
    a.make_session()  # freezes the (empty) snapshot
    assert a._memory_text() == ""
    a.memory.add("memory", "made poem.txt")  # mid-session write
    assert a._memory_text() == ""            # frozen block unchanged this session
    a.make_session()                         # a new session reloads the snapshot
    assert "made poem.txt" in a._memory_text()
