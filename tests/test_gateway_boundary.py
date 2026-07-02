"""Tool dispatch exception boundary (audit #23): a crashing tool becomes an
error RESULT fed to the model + an audit record — never a raw traceback that
aborts the whole streaming turn.

Builtin handlers run through the hermes-ported registry, which catches any
Exception and turns it into a {"error": ...} JSON string; the gateway classifies
that as a failed result and audits the call. MCP crashes still take the
gateway's own crash branch (tool_crash). KeyboardInterrupt is a BaseException,
not caught by the registry, so the safety quit still propagates."""
import json

import pytest

from lunamoth.core.state import EnvState
from lunamoth.obs.audit import AuditLog
from lunamoth.tools.gateway import ToolGateway
from lunamoth.tools.registry import registry, discover_builtin_tools, tool_error, tool_result
from lunamoth.tools.sandbox import Sandbox


_SCHEMA = {"description": "fake", "parameters": {"type": "object", "properties": {}}}


def _register(name, handler):
    registry.register(name, "fake-test", dict(_SCHEMA), handler, override=True)


@pytest.fixture
def gw(tmp_path):
    # Ensure the real builtins are registered, then snapshot the entries; restore
    # them on teardown so the fakes never leak into a sibling test (these tool
    # names are globally registered).
    discover_builtin_tools()
    saved = {n: registry.get_entry(n) for n in ("terminal", "write_log", "inspect_env")}
    _register("terminal", lambda args, ctx: tool_result(ok=True))
    _register("write_log", lambda args, ctx: tool_result(ok=True))
    _register("inspect_env", lambda args, ctx: tool_result(ok=True))
    g = ToolGateway(
        Sandbox(tmp_path / "sandbox"),
        EnvState(tmp_path / "env_status.json"),
        AuditLog(tmp_path / "audit.jsonl"),
    )
    g.set_enabled(["terminal", "write_log", "inspect_env"])
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


def test_effective_tools_default_all_via_wildcard(gw):
    """hermes parity: the default pack is ['*'] = every registered tool. An
    explicit list narrows; None means a tool-less (pure-roleplay) chara."""
    from lunamoth.tools.registry import registry as _reg
    all_names = set(_reg.get_all_tool_names())
    assert all_names  # builtins are registered

    gw.set_enabled(["*"])
    assert gw._effective() == all_names  # wildcard → everything

    gw.set_enabled(["terminal"])
    assert gw._effective() == ({"terminal"} & all_names)  # explicit list narrows

    gw.set_enabled(None)
    assert gw._effective() == set()  # tool-less chara


def _set_terminal(handler):
    _register("terminal", handler)


def _audit_events(g):
    return [json.loads(line)["event"] for line in g.audit.path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("exc", [BrokenPipeError("pipe gone"), OSError(24, "too many open files"), KeyError("missing")])
def test_tool_crash_becomes_error_result(gw, exc):
    def boom(args, ctx):
        raise exc

    _set_terminal(boom)
    out = gw.call("terminal", command="ls")
    assert out["ok"] is False
    assert type(exc).__name__ in out["error"]  # visible, typed, never silent
    # The crash was contained into a recorded tool_call, not a raised traceback.
    assert "tool_call" in _audit_events(gw)


def test_handler_returned_error_is_a_clean_result(gw):
    # A handler that signals failure the hermes way (return tool_error) is a
    # plain failed result, not a crash — the message is carried verbatim.
    _set_terminal(lambda args, ctx: tool_error("minutes must be a number"))
    out = gw.call("terminal", command="x")
    assert out == {"ok": False, "error": "minutes must be a number"}


def test_mcp_crash_is_contained_too(gw):
    class FakeMcp:
        def allowed_servers(self, entries):
            return ["dead"]

        def call(self, name, args):
            raise BrokenPipeError("server pipe closed")

    gw.mcp = FakeMcp()
    gw.mcp_allowed = ["dead"]
    out = gw.call("mcp__dead__anything", text="x")
    assert out["ok"] is False and "BrokenPipeError" in out["error"]
    assert "tool_crash" in _audit_events(gw)


def test_keyboard_interrupt_still_propagates(gw):
    def interrupt(args, ctx):
        raise KeyboardInterrupt

    _set_terminal(interrupt)
    with pytest.raises(KeyboardInterrupt):
        gw.call("terminal", command="x")  # safety quit must never be swallowed


def test_error_null_is_not_a_failure():
    """Regression: a success result that merely carries `"error": null` (the terminal
    background path) must NOT be classified as a failure. Gating on key presence
    turned such successes into a spurious 'ERROR: None'."""
    from lunamoth.tools.gateway import _is_error_json
    assert _is_error_json(json.dumps({"output": "started", "pid": 1, "error": None})) is False
    assert _is_error_json(json.dumps({"error": ""})) is False
    assert _is_error_json(json.dumps({"error": "real failure"})) is True
    # Edges the classifier must hold (success/failure inferred from JSON shape):
    assert _is_error_json(json.dumps({"ok": True, "data": 1})) is False  # no error key → success
    assert _is_error_json("plain non-json string") is False             # not JSON → success
    assert _is_error_json(json.dumps(["a", "b"])) is False              # JSON but not a dict
    assert _is_error_json(json.dumps({"data": {"error": "nested"}})) is False  # only TOP-level error counts
    # And the canonical helpers round-trip through the classifier correctly:
    assert _is_error_json(tool_error("boom")) is True
    assert _is_error_json(tool_result(ok=True, value=1)) is False
    # The explicit sentinel is authoritative: a tool_error is a failure even if a
    # caller passed error="" (the legacy heuristic alone would miss this).
    from lunamoth.tools.registry import TOOL_ERROR_KEY
    assert TOOL_ERROR_KEY in tool_error("x")
    assert _is_error_json(json.dumps({TOOL_ERROR_KEY: True, "error": ""})) is True
    # A success result is never misread, even if it carries the key set falsy.
    assert _is_error_json(json.dumps({TOOL_ERROR_KEY: False, "data": 1})) is False


def test_cross_thread_tool_rpc_does_not_deadlock(gw):
    """The execute_code shape (2026-07-02 P1): while one tool RUNS, its child
    script calls another tool back through the gateway from a DIFFERENT thread.
    ``_dispatch_lock`` must guard only the guard counters + audit, never the
    tool body — holding it across the body deadlocks the callback (RLock
    re-entrancy is same-thread only) and serializes delegate_task workers."""
    import threading

    inner: dict = {}

    def outer_handler(args, ctx):
        def rpc():
            inner["result"] = gw.call("write_log")

        t = threading.Thread(target=rpc, daemon=True)
        t.start()
        t.join(timeout=3.0)
        inner["done"] = not t.is_alive()
        return tool_result(ok=True)

    _register("terminal", outer_handler)
    res = gw.call("terminal")
    assert res["ok"] is True
    assert inner.get("done") is True, "cross-thread tool RPC deadlocked against _dispatch_lock"
    assert inner["result"]["ok"] is True
