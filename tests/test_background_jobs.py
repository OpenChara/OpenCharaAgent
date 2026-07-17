"""Background image generation + the turn-boundary notification drain.

generate_image runs in the background (a daemon worker) and pushes a completion
event onto the process registry's queue; the agent drains the queue at each turn
boundary (stream_handle / stream_think) and injects a synthetic user message so
the chara reacts to the finished job. (The handler/worker contract itself is
pinned in test_image_gen.py.)
"""
from __future__ import annotations

import pytest

from chara.tools.builtin._process_registry import (
    format_background_notification,
    get_registry,
)


# ---- format_background_notification (model-facing, neutral wording) -------------

def test_format_image_gen_ready_and_failed():
    ready = format_background_notification(
        {"type": "image_gen", "status": "ready", "path": "works/x.png"})
    assert "works/x.png" in ready and "MEDIA:works/x.png" in ready
    failed = format_background_notification(
        {"type": "image_gen", "status": "failed", "error": "boom"})
    assert "FAILED" in failed and "boom" in failed and "Nothing was saved" in failed


def test_format_completion_and_unknown():
    c = format_background_notification(
        {"type": "completion", "session_id": "p1", "command": "ls", "exit_code": 0, "output": "a\nb"})
    assert "p1" in c and "exit code 0" in c and "ls" in c and "a\nb" in c
    # an event type with no model-facing form is skipped (empty string)
    assert format_background_notification({"type": "nope"}) == ""


# ---- terminal inline watch notes must NOT steal completion/image_gen -----------

def test_collect_watch_notes_leaves_completions_for_agent_layer():
    """The terminal tool surfaces watch_* matches inline in its JSON result, but it
    must not consume the completion/image_gen notices the agent's turn-boundary drain
    owns. Draining them destructively here silently dropped 'job finished' notices."""
    from chara.tools.builtin._process_registry import ProcessRegistry
    from chara.tools.builtin.terminal import _collect_watch_notes
    reg = ProcessRegistry()
    reg.completion_queue.put({"type": "completion", "session_id": "p1", "command": "build", "exit_code": 0})
    reg.completion_queue.put({"type": "watch_match", "session_id": "p2", "pattern": "ready", "output": "up"})
    reg.completion_queue.put({"type": "image_gen", "status": "ready", "path": "works/x.png"})
    # inline surface: only the watch match comes back as a note
    notes = _collect_watch_notes(reg)
    assert notes == ["[watch:ready] up"]
    # the completion + image_gen survive for the agent layer's sole drain
    left = {e.get("type") for e in reg.drain_notifications()}
    assert left == {"completion", "image_gen"}


def test_has_pending_notifications_skips_consumed_completions():
    """The wake peek must agree with drain_notifications: a queue holding only an
    already-consumed completion is NOT pending (else the supervisor fires a no-op
    react). A delegate/image_gen event (never skipped) IS pending."""
    from chara.tools.builtin._process_registry import ProcessRegistry
    reg = ProcessRegistry()
    assert reg.has_pending_notifications() is False
    reg.completion_queue.put({"type": "completion", "session_id": "p1", "exit_code": 0})
    reg._completion_consumed.add("p1")  # model already polled it
    assert reg.has_pending_notifications() is False  # consumed → not pending
    reg.completion_queue.put({"type": "delegate", "status": "done", "results": []})
    assert reg.has_pending_notifications() is True   # delegate always surfaces
    # Peeking did not consume anything.
    assert {e.get("type") for e in reg.drain_notifications()} == {"delegate"}


def test_drain_watch_notes_partitions_by_type():
    from chara.tools.builtin._process_registry import ProcessRegistry
    reg = ProcessRegistry()
    reg.completion_queue.put({"type": "watch_disabled", "message": "watch off"})
    reg.completion_queue.put({"type": "completion", "session_id": "p1", "exit_code": 1})
    watch = reg.drain_watch_notes()
    assert [e["type"] for e in watch] == ["watch_disabled"]
    assert [e["type"] for e in reg.drain_notifications()] == ["completion"]


# ---- the drain (gateway accessor + agent injection) ----------------------------

@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.core.agent import CharaAgent
    from chara.session.settings import Settings
    return CharaAgent(Settings(character_path="", toolpack="sandbox"))


def test_gateway_drains_and_formats(agent):
    reg = get_registry(agent.tools._ctx())
    reg.completion_queue.put({"type": "image_gen", "status": "ready", "path": "works/moth.png"})
    notices = agent.tools.background_notices()
    assert len(notices) == 1 and "works/moth.png" in notices[0]
    # draining is destructive — a second call is empty
    assert agent.tools.background_notices() == []


def test_gateway_notices_empty_without_registry(agent):
    # no background job ever ran → no registry → no notices, never raises
    assert agent.tools.background_notices() == []


def test_agent_injects_background_notice_into_context(agent):
    s = agent.make_session()
    reg = get_registry(agent.tools._ctx())
    reg.completion_queue.put({"type": "image_gen", "status": "ready", "path": "works/moth.png"})
    agent._inject_background_notices(s)
    pairs = s.context.pairs()
    assert any(r == "user" and "works/moth.png" in c for r, c in pairs)


def test_no_notices_no_injection(agent):
    s = agent.make_session()
    before = len(s.context.messages)
    assert agent._inject_background_notices(s) == ""  # nothing pending → "" returned
    assert len(s.context.messages) == before


# ---- pending_notices peek + the completion-WAKE turn (stream_react) -------------

def test_pending_notices_peek_is_nondestructive(agent):
    assert agent.pending_notices() is False
    reg = get_registry(agent.tools._ctx())
    reg.completion_queue.put({"type": "image_gen", "status": "ready", "path": "works/x.png"})
    # peeking twice must NOT consume the notice (the supervisor polls it repeatedly)
    assert agent.pending_notices() is True
    assert agent.pending_notices() is True
    assert agent.tools.background_notices()  # still drainable


def test_inject_returns_the_text(agent):
    s = agent.make_session()
    reg = get_registry(agent.tools._ctx())
    reg.completion_queue.put({"type": "delegate", "status": "done",
                              "results": [{"task_index": 0, "status": "completed", "summary": "ok"}]})
    text = agent._inject_background_notices(s)
    assert "subtask 0" in text and "ok" in text


def test_stream_react_is_noop_when_nothing_pending(agent):
    s = agent.make_session()
    before = len(s.context.messages)
    evs = list(agent.stream_react(s))
    assert evs == []                          # no turn ran
    assert len(s.context.messages) == before  # context untouched


def test_stream_react_drains_and_reacts(agent):
    s = agent.make_session()
    reg = get_registry(agent.tools._ctx())
    reg.completion_queue.put({"type": "image_gen", "status": "ready", "path": "works/x.png"})
    list(agent.stream_react(s))  # exhaust the turn
    # the finished job was drained into context as a USER message (like a user word)…
    assert any(r == "user" and "works/x.png" in c for r, c in s.context.pairs())
    # …and the queue is now empty (the wake consumed it; no re-wake loop)
    assert agent.pending_notices() is False


# ---- kill_all: reap a chara's background process groups at session teardown -----

def test_kill_all_reaps_running_groups_and_is_noop_when_empty():
    import subprocess
    import time
    from chara.tools.builtin._process_registry import ProcessRegistry, ProcessSession

    reg = ProcessRegistry()
    assert reg.kill_all() == 0  # nothing running → no-op (the session-alive case)

    # a real DETACHED background process (its own session/group, exactly how the
    # registry spawns servers) — stands in for a chara's http.server.
    p = subprocess.Popen(["sleep", "30"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reg._running["t1"] = ProcessSession(id="t1", command="sleep 30", pid=p.pid, process=p)
    assert p.poll() is None  # running

    assert reg.kill_all() == 1  # reaped at teardown
    for _ in range(50):
        if p.poll() is not None:
            break
        time.sleep(0.1)
    assert p.poll() is not None  # the process group is gone
    assert reg.kill_all() == 0  # idempotent — already exited
