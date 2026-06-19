"""Tests for the ported `terminal` + `process` background-job tools."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Minimal fake ctx (no real EnvState/Sandbox needed)
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, isolation="admin", network=False, writable=None):
        self._d = {
            "isolation": isolation,
            "network_access": network,
            "writable_paths": writable or [],
        }

    def load(self):
        return dict(self._d)


class _FakeCtx:
    def __init__(self, workspace: Path, isolation="admin"):
        self._workspace = workspace
        self.state = _FakeState(isolation=isolation)
        self.processes = None

    @property
    def workspace(self) -> Path:
        return self._workspace

    def run_terminal(self, command, *, timeout, workdir=None):
        from lunamoth.tools.runner import run_terminal as _run
        status = self.state.load()
        return _run(
            command,
            workdir or self.workspace,
            isolation="admin",
            allow_network=bool(status.get("network_access", False)),
            writable_paths=status.get("writable_paths", []),
            timeout=timeout,
        )


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return _FakeCtx(ws, isolation="admin")


# ---------------------------------------------------------------------------
# Registration / discovery
# ---------------------------------------------------------------------------

def test_modules_register():
    from lunamoth.tools.registry import registry, discover_builtin_tools

    discover_builtin_tools()
    names = registry.get_all_tool_names()
    assert "terminal" in names
    assert "process" in names


def test_schemas_match_hermes_shape():
    from lunamoth.tools.builtin.terminal import TERMINAL_SCHEMA
    from lunamoth.tools.builtin.process import PROCESS_SCHEMA

    tp = TERMINAL_SCHEMA["parameters"]["properties"]
    assert set(tp) == {"command", "background", "timeout", "workdir", "pty",
                       "notify_on_complete", "watch_patterns"}
    assert TERMINAL_SCHEMA["parameters"]["required"] == ["command"]
    # Param SHAPE matches the reference byte-for-byte (model recognition); the
    # description is de-branded (no "the VM" in model-facing text).
    assert tp["command"]["description"] == "The command to execute in your environment"

    pp = PROCESS_SCHEMA["parameters"]["properties"]
    assert pp["action"]["enum"] == [
        "list", "poll", "log", "wait", "kill", "write", "submit", "close"
    ]
    assert PROCESS_SCHEMA["parameters"]["required"] == ["action"]


# ---------------------------------------------------------------------------
# terminal — foreground
# ---------------------------------------------------------------------------

def test_terminal_foreground_runs(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    out = terminal({"command": "echo hello-fg"}, ctx)
    assert "hello-fg" in out
    assert "exit=0" in out


def test_terminal_non_string_command(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    res = json.loads(terminal({"command": 123}, ctx))
    assert res["status"] == "error"
    assert "expected string" in res["error"]


def test_terminal_blocked_workdir(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    res = json.loads(terminal({"command": "echo hi", "workdir": "/tmp; rm -rf /"}, ctx))
    assert res["status"] == "blocked"
    assert "disallowed character" in res["error"]


def test_terminal_foreground_longlived_hard_blocked(ctx):
    # hermes parity (owner 2026-06-19): a long-lived / self-backgrounding command
    # in the foreground is HARD-BLOCKED (not run), with guidance to background it.
    from lunamoth.tools.builtin.terminal import terminal
    import json

    res = json.loads(terminal({"command": "nohup python -m http.server 8000"}, ctx))
    assert res["status"] == "blocked"
    assert "background=true" in res["error"]

    res2 = json.loads(terminal({"command": "python -m http.server 8000"}, ctx))
    assert res2["status"] == "blocked"

    # A normal foreground command is unaffected.
    out = terminal({"command": "echo hi"}, ctx)
    assert "blocked" not in out


def test_terminal_foreground_timeout_clamped(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    out = terminal({"command": "echo ok", "timeout": 999999}, ctx)
    assert "ok" in out
    assert "clamped" in out  # runner clamps, never rejects


# ---------------------------------------------------------------------------
# terminal — background + process roundtrip
# ---------------------------------------------------------------------------

def _wait_exit(reg, sid, deadline=10.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        r = reg.poll(sid)
        if r.get("status") == "exited":
            return r
        time.sleep(0.05)
    raise AssertionError("process did not exit in time")


def test_background_spawn_and_poll(ctx):
    from lunamoth.tools.builtin.terminal import terminal
    from lunamoth.tools.builtin._process_registry import get_registry

    res = json.loads(terminal(
        {"command": "echo bg-out; sleep 0.2", "background": True, "notify_on_complete": True},
        ctx,
    ))
    assert res["output"] == "Background process started"
    sid = res["session_id"]
    assert sid.startswith("proc_")
    assert res["pid"] > 0
    assert res["notify_on_complete"] is True

    reg = get_registry(ctx)
    final = _wait_exit(reg, sid)
    assert final["exit_code"] == 0
    assert "bg-out" in final["output_preview"]


def test_background_silent_hint(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    res = json.loads(terminal({"command": "echo x", "background": True}, ctx))
    assert "hint" in res  # silent bg gets a nudge


def test_background_notify_watch_mutex(ctx):
    from lunamoth.tools.builtin.terminal import terminal

    res = json.loads(terminal(
        {"command": "echo x", "background": True,
         "notify_on_complete": True, "watch_patterns": ["DONE"]},
        ctx,
    ))
    assert "watch_patterns_ignored" in res
    assert "watch_patterns" not in res  # dropped


def test_process_list_poll_log(ctx):
    from lunamoth.tools.builtin.terminal import terminal
    from lunamoth.tools.builtin.process import process
    from lunamoth.tools.builtin._process_registry import get_registry

    res = json.loads(terminal(
        {"command": "echo line1; echo line2; sleep 0.1", "background": True},
        ctx,
    ))
    sid = res["session_id"]
    reg = get_registry(ctx)
    _wait_exit(reg, sid)

    listing = json.loads(process({"action": "list"}, ctx))
    assert any(p["session_id"] == sid for p in listing["processes"])

    polled = json.loads(process({"action": "poll", "session_id": sid}, ctx))
    assert polled["status"] == "exited"

    logged = json.loads(process({"action": "log", "session_id": sid}, ctx))
    assert "line1" in logged["output"]
    assert "line2" in logged["output"]
    assert logged["total_lines"] >= 2


def test_process_wait(ctx):
    from lunamoth.tools.builtin.terminal import terminal
    from lunamoth.tools.builtin.process import process

    res = json.loads(terminal(
        {"command": "echo waited; sleep 0.2", "background": True},
        ctx,
    ))
    sid = res["session_id"]
    waited = json.loads(process({"action": "wait", "session_id": sid, "timeout": 10}, ctx))
    assert waited["status"] == "exited"
    assert waited["exit_code"] == 0
    assert "waited" in waited["output"]


def test_process_kill(ctx):
    from lunamoth.tools.builtin.terminal import terminal
    from lunamoth.tools.builtin.process import process
    from lunamoth.tools.builtin._process_registry import get_registry

    res = json.loads(terminal(
        {"command": "sleep 30", "background": True},
        ctx,
    ))
    sid = res["session_id"]
    killed = json.loads(process({"action": "kill", "session_id": sid}, ctx))
    assert killed["status"] == "killed"

    reg = get_registry(ctx)
    polled = reg.poll(sid)
    assert polled["status"] == "exited"


def test_process_write_submit_to_stdin(ctx):
    from lunamoth.tools.builtin.terminal import terminal
    from lunamoth.tools.builtin.process import process
    from lunamoth.tools.builtin._process_registry import get_registry

    # A reader that echoes a line from stdin then exits.
    res = json.loads(terminal(
        {"command": "read x; echo GOT=$x", "background": True, "notify_on_complete": True},
        ctx,
    ))
    sid = res["session_id"]

    sub = json.loads(process({"action": "submit", "session_id": sid, "data": "ping"}, ctx))
    assert sub["status"] == "ok"

    reg = get_registry(ctx)
    final = _wait_exit(reg, sid)
    assert "GOT=ping" in final["output_preview"]


def test_process_missing_session_id(ctx):
    from lunamoth.tools.builtin.process import process

    res = json.loads(process({"action": "poll"}, ctx))
    assert "error" in res
    assert "session_id is required" in res["error"]


def test_process_unknown_action(ctx):
    from lunamoth.tools.builtin.process import process

    res = json.loads(process({"action": "frobnicate"}, ctx))
    assert "error" in res
    assert "Unknown process action" in res["error"]


def test_process_not_found(ctx):
    from lunamoth.tools.builtin.process import process

    res = json.loads(process({"action": "poll", "session_id": "proc_doesnotexist"}, ctx))
    assert res["status"] == "not_found"


# ---------------------------------------------------------------------------
# registry internals — watch rate-limit + reconcile + prune
# ---------------------------------------------------------------------------

def test_watch_rate_limit_strikes_and_promotes():
    from lunamoth.tools.builtin._process_registry import (
        ProcessRegistry, ProcessSession, WATCH_STRIKE_LIMIT,
    )

    reg = ProcessRegistry()
    s = ProcessSession(id="proc_x", command="c", watch_patterns=["BOOM"], started_at=time.time())

    # First match emits and opens a cooldown window.
    reg._check_watch_patterns(s, "line BOOM here\n")
    assert s._watch_hits == 1

    # A strike is one WINDOW, not one match: a strike is racked only when a match
    # arrives inside an active cooldown with no prior strike-candidate for that
    # window. Each strike therefore needs (a) a fresh emission opening a window,
    # then (b) a dropped match inside it. Drive WATCH_STRIKE_LIMIT such windows.
    for _ in range(WATCH_STRIKE_LIMIT):
        if s._watch_disabled:
            break
        # (a) Expire the window and emit again -> candidate reset to False.
        s._watch_cooldown_until = time.time() - 1
        reg._check_watch_patterns(s, "emit BOOM\n")
        # (b) A match inside the new cooldown -> one strike for this window.
        reg._check_watch_patterns(s, "drop BOOM\n")

    assert s._watch_disabled is True
    assert s.notify_on_complete is True  # promoted

    # A watch_disabled summary event was queued.
    events = reg.drain_notifications()
    assert any(e.get("type") == "watch_disabled" for e in events)


def test_reconcile_flips_exited_when_reader_blocked():
    from lunamoth.tools.builtin._process_registry import ProcessRegistry, ProcessSession

    reg = ProcessRegistry()

    class _FakeStdout:
        def fileno(self):
            raise ValueError("no real fd")  # drain becomes a no-op

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout()
            self.returncode = 7

        def poll(self):
            return 7  # direct child already exited

    s = ProcessSession(id="proc_r", command="c", started_at=time.time())
    s.process = _FakeProc()
    reg._running[s.id] = s

    reg._reconcile_local_exit(s)
    assert s.exited is True
    assert s.exit_code == 7
    assert s.id in reg._finished


def test_prune_evicts_over_max():
    from lunamoth.tools.builtin import _process_registry as pr

    reg = pr.ProcessRegistry()
    # Stuff finished sessions over MAX_PROCESSES; prune should evict the oldest.
    for i in range(pr.MAX_PROCESSES + 5):
        s = pr.ProcessSession(id=f"proc_{i}", command="c", started_at=time.time() + i, exited=True)
        reg._finished[s.id] = s
    with reg._lock:
        reg._prune_if_needed()
    assert len(reg._finished) < pr.MAX_PROCESSES + 5


def test_prune_drops_expired_ttl():
    from lunamoth.tools.builtin import _process_registry as pr

    reg = pr.ProcessRegistry()
    old = pr.ProcessSession(
        id="proc_old", command="c",
        started_at=time.time() - pr.FINISHED_TTL_SECONDS - 10, exited=True,
    )
    reg._finished[old.id] = old
    with reg._lock:
        reg._prune_if_needed()
    assert "proc_old" not in reg._finished
