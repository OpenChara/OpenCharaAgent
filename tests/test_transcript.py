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
    ], "reasoning_content": "let me look around",
        "reasoning_details": [{"type": "reasoning.encrypted", "signature": "sig-1"}]}
    t.append_message({"role": "user", "content": "run it"})
    t.append_message(call)
    t.append_message({"role": "tool", "tool_call_id": "c1", "content": "exit=0"})
    t.append_message({"role": "assistant", "content": "done"})
    rows = TranscriptStore(t.path).load()
    assert rows[0] == {"role": "user", "content": "run it"}
    assert rows[1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert rows[1]["reasoning_content"] == "let me look around"
    # reasoning_details (signature/continuity blocks) survives the round-trip intact.
    assert rows[1]["reasoning_details"] == [{"type": "reasoning.encrypted", "signature": "sig-1"}]
    assert rows[2] == {"role": "tool", "tool_call_id": "c1", "content": "exit=0"}
    # Self-work / chat assistant turns load uniformly as ordinary messages
    # (no per-message classification — hermes-faithful).
    assert rows[3] == {"role": "assistant", "content": "done"}


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


def test_export_jsonl_complete_roundtrip(tmp_path):
    import json

    t = TranscriptStore(tmp_path / "t.db")
    t.append_message({"role": "user", "content": "run it"})
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}
    ], "reasoning_content": "let me look around"}
    t.append_message(call)
    t.append_message({"role": "tool", "tool_call_id": "c1", "content": "exit=0\nfile.txt"})
    t.append_message({"role": "assistant", "content": "done"})

    out = tmp_path / "conv.jsonl"
    n = t.export_jsonl(out)
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert n == 4 and len(lines) == 4
    # Oldest first, every kind present.
    assert lines[0]["role"] == "user" and lines[0]["content"] == "run it"
    # The struct row expands back to its FULL message dict.
    assert lines[1]["kind"] == "struct"
    assert lines[1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert lines[1]["reasoning_content"] == "let me look around"
    # The tool result line.
    assert lines[2]["role"] == "tool" and lines[2]["tool_call_id"] == "c1"
    assert "exit=0" in lines[2]["content"]
    # Every line carries id + ts.
    assert all("id" in o and "ts" in o for o in lines)


def test_export_jsonl_only_current_epoch(tmp_path):
    import json

    t = TranscriptStore(tmp_path / "t.db")
    t.append("user", "old")
    t.reset()
    t.append("user", "new")
    out = tmp_path / "conv.jsonl"
    t.export_jsonl(out)
    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert [o["content"] for o in lines] == ["new"]


def test_export_jsonl_missing_db_is_empty_not_fabricated(tmp_path):
    # Force the read-only export path to face a DB that was never written.
    fresh = TranscriptStore.__new__(TranscriptStore)
    fresh.path = tmp_path / "absent.db"
    fresh.available = True
    out = tmp_path / "empty.jsonl"
    assert fresh.export_jsonl(out) == 0
    assert out.read_text(encoding="utf-8") == ""


def test_load_display_includes_legacy_tool_rows(tmp_path):
    t = TranscriptStore(tmp_path / "t.db")
    t.append("user", "hi")
    # A legacy forensic tool row (older builds wrote kind='tool').
    import json as _json
    t.append("tool", _json.dumps({"role": "tool", "tool_call_id": "x", "content": "42"}), kind="tool")
    model_view = t.load()       # what the model replays — excludes 'tool'
    display_view = t.load_display()  # what the frontend shows — includes 'tool'
    assert all(m.get("role") != "tool" for m in model_view)
    assert any(m.get("role") == "tool" and m.get("content") == "42" for m in display_view)


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
    # /reset starts a new epoch AND re-seeds the card's opening line (first_mes),
    # so the chara re-introduces itself. The prior conversation is gone — no user
    # turn survives; only the re-seeded assistant greeting (if the card has one).
    a.handle("/reset", s2)
    s3 = a.make_session()
    assert ("user", "记住这句话") not in s3.context.pairs()
    assert all(role == "assistant" for role, _ in s3.context.pairs())
