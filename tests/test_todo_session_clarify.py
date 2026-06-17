"""Tests for the ported general tools: todo, session_search.

Each tool is exercised against a tmp workspace with a minimal fake ToolContext
(only the touchpoints each tool reaches). No network / LLM / external drivers.

The clarify TOOL was retired (the model no longer gets a "give the user options"
shortcut); the terminal-side interactive-question hook (`_stdin_clarify_hook`)
remains as dormant generic plumbing and keeps its unit coverage below.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from lunamoth.tools.builtin import session_search as ss_mod
from lunamoth.tools.builtin import todo as todo_mod
from lunamoth.tools.registry import registry, discover_builtin_tools


# --------------------------------------------------------------------------- #
# Minimal fake context
# --------------------------------------------------------------------------- #

class FakeState:
    def __init__(self, status: Optional[dict] = None):
        self._status = status or {}

    def load(self) -> dict:
        return dict(self._status)


@dataclass
class FakeCtx:
    state: Any = None
    transcript: Any = None
    clarify_hook: Optional[Callable] = None
    _scratch: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Discovery / registration
# --------------------------------------------------------------------------- #

def test_tools_register_and_discover():
    discover_builtin_tools()
    names = registry.get_all_tool_names()
    for n in ("todo", "session_search"):
        assert n in names, f"{n} not discovered"
    # the retired clarify tool must NOT be discoverable any more
    assert "clarify" not in names
    # schema description + params are present (model is post-trained on shape)
    for n in ("todo", "session_search"):
        schema = registry.get_schema(n)
        assert schema and "description" in schema and "parameters" in schema


# --------------------------------------------------------------------------- #
# todo
# --------------------------------------------------------------------------- #

def test_todo_replace_and_read():
    ctx = FakeCtx()
    out = json.loads(todo_mod.todo({"todos": [
        {"id": "1", "content": "first", "status": "pending"},
        {"id": "2", "content": "second", "status": "in_progress"},
    ]}, ctx))
    assert out["summary"]["total"] == 2
    assert out["summary"]["in_progress"] == 1
    # read (no params) returns same list from the per-session store
    again = json.loads(todo_mod.todo({}, ctx))
    assert again["todos"] == out["todos"]


def test_todo_merge_updates_by_id_and_appends():
    ctx = FakeCtx()
    todo_mod.todo({"todos": [{"id": "a", "content": "alpha", "status": "pending"}]}, ctx)
    out = json.loads(todo_mod.todo({"merge": True, "todos": [
        {"id": "a", "status": "completed"},               # update existing
        {"id": "b", "content": "beta", "status": "pending"},  # append new
    ]}, ctx))
    by_id = {t["id"]: t for t in out["todos"]}
    assert by_id["a"]["status"] == "completed"
    assert by_id["a"]["content"] == "alpha"  # preserved
    assert by_id["b"]["content"] == "beta"
    assert out["summary"]["total"] == 2


def test_todo_validation_and_dedupe():
    ctx = FakeCtx()
    out = json.loads(todo_mod.todo({"todos": [
        {"id": "", "content": "", "status": "bogus"},
        {"id": "x", "content": "c", "status": "completed"},
        {"id": "x", "content": "c2", "status": "cancelled"},  # dup id → last wins
    ]}, ctx))
    by_id = {t["id"]: t for t in out["todos"]}
    assert "?" in by_id and by_id["?"]["content"] == "(no description)"
    assert by_id["?"]["status"] == "pending"  # invalid → pending
    assert by_id["x"]["status"] == "cancelled"


def test_todo_content_cap():
    ctx = FakeCtx()
    big = "z" * (todo_mod.MAX_TODO_CONTENT_CHARS + 50)
    out = json.loads(todo_mod.todo({"todos": [
        {"id": "1", "content": big, "status": "pending"},
    ]}, ctx))
    content = out["todos"][0]["content"]
    assert len(content) <= todo_mod.MAX_TODO_CONTENT_CHARS
    assert content.endswith("… [truncated]")


def test_todo_format_for_injection():
    store = todo_mod.TodoStore()
    store.write([
        {"id": "1", "content": "do", "status": "in_progress"},
        {"id": "2", "content": "done", "status": "completed"},
    ])
    block = store.format_for_injection()
    assert "preserved across context compression" in block
    assert "do" in block
    assert "done" not in block  # completed items excluded


# --------------------------------------------------------------------------- #
# terminal clarify hook (front/terminal.py) — dormant interactive-question
# plumbing kept after the clarify tool was retired; its parsing still has tests.
# --------------------------------------------------------------------------- #

def _wire_stdin(monkeypatch, lines):
    """Feed the terminal stdin helpers a queue of answer lines."""
    from lunamoth.front import terminal as term
    queue = list(lines)
    monkeypatch.setattr(term, "_stdin_line_ready", lambda: bool(queue))
    monkeypatch.setattr(term, "_read_line", lambda: queue.pop(0) if queue else None)
    return term


def test_terminal_clarify_numbered_choice(monkeypatch):
    term = _wire_stdin(monkeypatch, ["2"])
    answer = term._stdin_clarify_hook("pick one", ["alpha", "beta", "gamma"])
    assert answer == "beta"


def test_terminal_clarify_free_text(monkeypatch):
    term = _wire_stdin(monkeypatch, ["my own answer"])
    answer = term._stdin_clarify_hook("anything?", None)
    assert answer == "my own answer"


def test_terminal_clarify_out_of_range_is_free_text(monkeypatch):
    # A number outside the choice range is treated as a free-form answer.
    term = _wire_stdin(monkeypatch, ["9"])
    answer = term._stdin_clarify_hook("pick", ["a", "b"])
    assert answer == "9"


def test_terminal_clarify_empty_line_returns_blank(monkeypatch):
    term = _wire_stdin(monkeypatch, [""])
    assert term._stdin_clarify_hook("q", ["a"]) == ""


# --------------------------------------------------------------------------- #
# session_search
# --------------------------------------------------------------------------- #

@pytest.fixture
def transcript(tmp_path):
    from lunamoth.core.transcript import TranscriptStore
    store = TranscriptStore(tmp_path / "transcript.sqlite")
    assert store.available
    return store


def _seed(store, rows):
    """rows = list of (role, content). Appends in order to the current epoch."""
    for role, content in rows:
        store.append(role, content)
        time.sleep(0.001)


def test_session_search_unavailable():
    ctx = FakeCtx(transcript=None)
    out = json.loads(ss_mod.session_search({}, ctx))
    assert "error" in out


def test_session_search_browse(transcript):
    # epoch 0 then bump to epoch 1; browse should skip the current epoch (1).
    _seed(transcript, [("user", "old topic alpha"), ("assistant", "old reply")])
    transcript.reset()  # -> epoch 1 (current)
    _seed(transcript, [("user", "current talk"), ("assistant", "current reply")])

    ctx = FakeCtx(transcript=transcript)
    out = json.loads(ss_mod.session_search({}, ctx))
    assert out["mode"] == "browse"
    sids = [r["session_id"] for r in out["results"]]
    assert "0" in sids       # old epoch shown
    assert "1" not in sids   # current epoch hidden


def test_session_search_discovery(transcript):
    _seed(transcript, [
        ("user", "let us discuss the docker networking refactor"),
        ("assistant", "sure, the auth refactor comes after"),
    ])
    transcript.reset()  # move to a fresh current epoch so epoch 0 is searchable
    ctx = FakeCtx(transcript=transcript)

    out = json.loads(ss_mod.session_search({"query": "docker networking"}, ctx))
    assert out["mode"] == "discover"
    assert out["count"] == 1
    res = out["results"][0]
    assert res["session_id"] == "0"
    assert "docker" in res["snippet"].lower()
    assert res["match_message_id"] is not None
    assert res["bookend_start"]  # kickoff messages present


def test_session_search_discovery_and_or(transcript):
    _seed(transcript, [("user", "alpha only here")])
    transcript.reset()
    ctx = FakeCtx(transcript=transcript)
    # AND: both terms required → no hit
    out = json.loads(ss_mod.session_search({"query": "alpha beta"}, ctx))
    assert out["count"] == 0
    # OR: either term → hit
    out = json.loads(ss_mod.session_search({"query": "alpha OR beta"}, ctx))
    assert out["count"] == 1


def test_session_search_scroll(transcript):
    _seed(transcript, [
        ("user", "m1"), ("assistant", "m2"), ("user", "m3"),
        ("assistant", "m4"), ("user", "m5"),
    ])
    transcript.reset()
    ctx = FakeCtx(transcript=transcript)

    # find a real message id via discovery
    disc = json.loads(ss_mod.session_search({"query": "m3"}, ctx))
    anchor = disc["results"][0]["match_message_id"]

    out = json.loads(ss_mod.session_search(
        {"session_id": "0", "around_message_id": anchor, "window": 1}, ctx))
    assert out["mode"] == "scroll"
    ids = [m["id"] for m in out["messages"]]
    assert anchor in ids
    assert any(m.get("anchor") for m in out["messages"])
    assert len(out["messages"]) <= 3  # window=1 → anchor ± 1


def test_session_search_scroll_rejects_current_epoch(transcript):
    _seed(transcript, [("user", "hello")])
    ctx = FakeCtx(transcript=transcript)  # still epoch 0 = current
    out = json.loads(ss_mod.session_search(
        {"session_id": "0", "around_message_id": 1}, ctx))
    assert "error" in out and "current session" in out["error"].lower()


def test_session_search_read(transcript):
    _seed(transcript, [("user", "first"), ("assistant", "second"), ("user", "third")])
    transcript.reset()
    ctx = FakeCtx(transcript=transcript)
    out = json.loads(ss_mod.session_search({"session_id": "0"}, ctx))
    assert out["mode"] == "read"
    assert out["message_count"] == 3
    assert [m["content"] for m in out["messages"]] == ["first", "second", "third"]


def test_session_search_read_not_found(transcript):
    ctx = FakeCtx(transcript=transcript)
    out = json.loads(ss_mod.session_search({"session_id": "99"}, ctx))
    assert "error" in out and "not found" in out["error"].lower()
