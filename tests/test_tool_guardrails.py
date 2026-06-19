"""Tool-loop guardrails (audit #24, the shape of hermes tool_guardrails.py):
identical failing calls warned at 2 and refused at 5; a tool with 8
consecutive failures (any args) is blocked until something succeeds or
reset_guardrails() runs. An unattended chara must not be able to spend a
night (and a key's budget) re-running the same failing call.

The guardrails live in ToolGateway and wrap the (hermes-ported) registry
dispatch. To drive a failing/succeeding tool we register fake handlers into
the registry and let the gateway's gate/guard/audit run on top, exactly as a
real model call would.
"""
import json

import pytest

from lunamoth.core.state import EnvState
from lunamoth.obs.audit import AuditLog
from lunamoth.tools.gateway import ToolGateway
from lunamoth.tools.registry import registry, discover_builtin_tools, tool_result
from lunamoth.tools.sandbox import Sandbox


# A tiny no-arg schema shared by the fake tools.
_SCHEMA = {"description": "fake", "parameters": {"type": "object", "properties": {}}}


class FakeTool:
    """A registry handler we can flip between success and failure, counting the
    times the BODY actually executes (refusals never reach it)."""

    def __init__(self, exc=None):
        self.calls = 0
        self.exc = exc

    def __call__(self, args, ctx):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return tool_result(ok=True)


def _register(name, handler):
    registry.register(name, "fake-test", dict(_SCHEMA), handler, override=True)


@pytest.fixture
def gw(tmp_path):
    # write_log succeeds; terminal is the tool under test (swapped per-test).
    # Ensure the real builtins are registered, then snapshot the entries so we
    # restore them on teardown (these tool names are registered globally — never
    # leave a fake behind for a sibling test).
    discover_builtin_tools()
    saved = {n: registry.get_entry(n) for n in ("terminal", "read_file", "search_files")}
    _register("terminal", FakeTool())
    _register("read_file", FakeTool())
    _register("search_files", FakeTool())
    g = ToolGateway(
        Sandbox(tmp_path / "sandbox"),
        EnvState(tmp_path / "env_status.json"),
        AuditLog(tmp_path / "audit.jsonl"),
    )
    g.set_enabled(["terminal", "read_file", "search_files"])
    yield g
    for n, entry in saved.items():
        if entry is not None:
            registry.register(
                entry.name, entry.toolset, entry.schema, entry.handler,
                check_fn=entry.check_fn, requires_env=entry.requires_env,
                description=entry.description, emoji=entry.emoji,
                max_result_size_chars=entry.max_result_size_chars,
                dynamic_schema_overrides=entry.dynamic_schema_overrides,
                override=True,
            )
        else:
            registry.deregister(n)


def _set_terminal(g, handler):
    """Repoint the registered 'terminal' tool at a new handler."""
    _register("terminal", handler)
    return handler


def _audit_events(g):
    return [json.loads(line)["event"] for line in g.audit.path.read_text(encoding="utf-8").splitlines()]


def test_identical_failure_warns_at_2(gw):
    _set_terminal(gw, FakeTool(ValueError("boom")))
    first = gw.call("terminal", command="x")
    assert first["ok"] is False and "loop guard" not in first["error"]
    second = gw.call("terminal", command="x")
    assert "loop guard" in second["error"] and "failed 2 times" in second["error"]


def test_different_args_are_different_signatures(gw):
    _set_terminal(gw, FakeTool(ValueError("boom")))
    gw.call("terminal", command="x")
    other = gw.call("terminal", command="y")
    assert "loop guard" not in other["error"]  # not the same failing call


def test_identical_failure_refused_at_5(gw):
    fail = _set_terminal(gw, FakeTool(ValueError("boom")))
    for _ in range(4):
        gw.call("terminal", command="x")
    assert fail.calls == 4
    fifth = gw.call("terminal", command="x")
    assert fifth["ok"] is False and "refusing to run terminal" in fifth["error"]
    assert fail.calls == 4  # the 5th identical attempt never executed
    assert "tool_loop_refused" in _audit_events(gw)


def test_success_resets_the_exact_counter(gw):
    _set_terminal(gw, FakeTool(ValueError("boom")))
    gw.call("terminal", command="x")
    _set_terminal(gw, FakeTool())  # now it succeeds
    assert gw.call("terminal", command="x")["ok"] is True
    _set_terminal(gw, FakeTool(ValueError("boom")))
    again = gw.call("terminal", command="x")
    assert "loop guard" not in again["error"]  # counter started over after the success


def test_tool_streak_blocks_after_8_consecutive_failures(gw):
    fail = _set_terminal(gw, FakeTool(ValueError("boom")))
    for i in range(8):  # different args each time: the exact-signature gate never trips
        out = gw.call("terminal", command=f"cmd-{i}")
        assert "refusing" not in out["error"] and "blocked" not in out["error"]
    assert fail.calls == 8
    ninth = gw.call("terminal", command="cmd-new")
    assert ninth["ok"] is False and "terminal is blocked" in ninth["error"]
    assert fail.calls == 8  # never executed


def test_any_success_resets_the_streak(gw):
    _set_terminal(gw, FakeTool(ValueError("boom")))
    for i in range(7):
        gw.call("terminal", command=f"cmd-{i}")
    _set_terminal(gw, FakeTool())  # one success
    assert gw.call("terminal", command="ok")["ok"] is True
    fail2 = _set_terminal(gw, FakeTool(ValueError("boom")))
    out = gw.call("terminal", command="post-success")
    assert fail2.calls == 1 and "blocked" not in out["error"]  # streak started over


def test_streaks_are_per_tool(gw):
    _set_terminal(gw, FakeTool(ValueError("boom")))
    for i in range(8):
        gw.call("terminal", command=f"cmd-{i}")
    assert gw.call("read_file", text="still works")["ok"] is True  # other tools unaffected


def test_reset_guardrails_clears_both_gates(gw):
    fail = _set_terminal(gw, FakeTool(ValueError("boom")))
    for i in range(8):
        gw.call("terminal", command="x" if i < 4 else f"cmd-{i}")
    assert "blocked" in gw.call("terminal", command="y")["error"]
    gw.reset_guardrails()  # the fresh-turn seam
    executed_before = fail.calls
    out = gw.call("terminal", command="x")
    assert fail.calls == executed_before + 1  # executes again
    assert "loop guard" not in out["error"] and "refusing" not in out["error"]


def test_refusals_do_not_compound_state(gw):
    fail = _set_terminal(gw, FakeTool(ValueError("boom")))
    for _ in range(4):
        gw.call("terminal", command="x")
    for _ in range(10):  # ten refusals must not advance the streak toward 8
        gw.call("terminal", command="x")
    out = gw.call("terminal", command="different")  # streak is still 4, so this runs
    assert fail.calls == 5
    assert "blocked" not in out["error"]


def test_denied_tools_count_as_failures_too(gw):
    # A model hammering a tool the pack denies is the same loop.
    for _ in range(4):
        out = gw.call("rest", minutes=5)
        assert "tool denied" in out["error"]
    fifth = gw.call("rest", minutes=5)
    assert "refusing to run rest" in fifth["error"]


def test_mcp_calls_are_guarded_too(gw):
    class DeadMcp:
        calls = 0

        def allowed_servers(self, entries):
            return ["srv"]

        def call(self, name, args):
            DeadMcp.calls += 1
            raise BrokenPipeError("server pipe closed")

    gw.mcp = DeadMcp()
    gw.set_enabled(["terminal"], ["srv"])
    for _ in range(4):
        gw.call("mcp__srv__fetch", url="http://x")
    assert DeadMcp.calls == 4
    fifth = gw.call("mcp__srv__fetch", url="http://x")
    assert "refusing to run mcp__srv__fetch" in fifth["error"]
    assert DeadMcp.calls == 4


# ---------------------------------------------------------------------------
# delegate_task worker isolation (parallel fan-out concurrency-safety)
# ---------------------------------------------------------------------------
def test_worker_dispatch_has_isolated_guard_scope(gw):
    """A delegate_task worker's dispatch carries its OWN loop-guardrail scope:
    a worker hammering a failing tool must NOT corrupt the parent's streaks, so
    the parent's next real dispatch still works."""
    _set_terminal(gw, FakeTool(ValueError("boom")))
    worker = gw.spawn_worker_dispatch()
    # Drive the worker's guard to the streak-block threshold on its own scope.
    for _ in range(8):
        worker("terminal", {"command": "x"})
    # The parent's guard counters are untouched.
    assert gw._guard_tool_streaks == {}
    assert gw._guard_exact_failures == {}
    # And the parent can still dispatch a fresh (now-succeeding) tool.
    _set_terminal(gw, FakeTool())
    res = gw.call("terminal", command="ok")
    assert res["ok"] is True


def test_concurrent_workers_dont_corrupt_parent_guardrails(gw):
    """Run N worker dispatchers concurrently against the shared gateway, then
    confirm the parent's guardrail state is clean and a normal call still works
    (the #24 guard + audit must be thread-safe under the parallel fan-out)."""
    import threading

    _set_terminal(gw, FakeTool())  # succeeds
    start = threading.Barrier(4)
    errors: list = []

    def run_worker(i):
        try:
            disp = gw.spawn_worker_dispatch()
            start.wait()
            for j in range(20):
                disp("terminal", {"command": f"{i}-{j}"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run_worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker threads raised: {errors}"
    # Parent guardrails never touched by worker traffic.
    assert gw._guard_tool_streaks == {}
    assert gw._guard_exact_failures == {}
    # A normal parent dispatch still works after the concurrent storm.
    res = gw.call("terminal", command="final")
    assert res["ok"] is True
