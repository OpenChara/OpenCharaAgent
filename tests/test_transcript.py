"""Durable transcript: roundtrip across instances, epochs, and agent restore."""
import pytest

from lunamoth.session.settings import Settings
from lunamoth.core.transcript import TranscriptStore


def test_roundtrip_across_instances(tmp_path):
    db = tmp_path / "t.db"
    a = TranscriptStore(db)
    a.append("user", "hello")
    a.append("assistant", "hi there")
    b = TranscriptStore(db)  # a fresh process
    assert b.load() == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]


def test_reset_starts_new_epoch_but_keeps_history(tmp_path):
    t = TranscriptStore(tmp_path / "t.db")
    t.append("user", "old world")
    assert t.count() == 1
    t.reset()
    assert t.load() == []
    t.append("user", "new world")
    assert t.load() == [{"role": "user", "content": "new world"}]
    # The old epoch's rows are still on disk (forensics), just not loaded.
    import sqlite3

    with sqlite3.connect(t.path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert total == 2


def test_structured_messages_roundtrip(tmp_path):
    t = TranscriptStore(tmp_path / "t.db")
    call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}
    ], "reasoning_content": "let me look around"}
    t.append_message({"role": "user", "content": "run it"})
    t.append_message(call)
    t.append_message({"role": "tool", "tool_call_id": "c1", "content": "exit=0"})
    t.append_message({"role": "assistant", "content": "done", "kind": "think"})
    rows = TranscriptStore(t.path).load()
    assert rows[0] == {"role": "user", "content": "run it"}
    assert rows[1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert rows[1]["reasoning_content"] == "let me look around"
    assert rows[2] == {"role": "tool", "tool_call_id": "c1", "content": "exit=0"}
    assert rows[3]["kind"] == "think"  # idle monologues stay tagged across restarts


def test_load_tail_limit(tmp_path):
    t = TranscriptStore(tmp_path / "t.db")
    for i in range(10):
        t.append("user", f"m{i}")
    assert [m["content"] for m in t.load(max_messages=3)] == ["m7", "m8", "m9"]


def test_unwritable_path_degrades_gracefully(tmp_path):
    assert TranscriptStore(tmp_path / "nope" / "x" / "t.db").available is True  # parent is created
    # Now simulate failure via a directory sitting at the db path.
    bad = tmp_path / "isadir"
    bad.mkdir()
    t2 = TranscriptStore(bad)  # path is a directory -> sqlite cannot open it
    assert t2.available is False
    t2.append("user", "lost but harmless")
    assert t2.load() == []


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


def test_session_restores_conversation(agent):
    a = agent()
    a.transcript.reset()  # SANDBOX_ROOT is import-time global; isolate this test
    s1 = a.make_session()
    a.handle("记住这句话", s1)
    assert any(role == "user" for role, _ in s1.context.pairs())
    # A brand-new session (fresh attach / restart) restores the same conversation.
    s2 = a.make_session()
    assert ("user", "记住这句话") in s2.context.pairs()
    # /reset starts a new epoch: the next session comes up empty.
    a.handle("/reset", s2)
    s3 = a.make_session()
    assert s3.context.messages == []
