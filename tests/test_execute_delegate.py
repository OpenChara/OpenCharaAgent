"""Tests for the ported execute_code + delegate_task tools.

These exercise each tool against a tmp workspace with a minimal fake ctx
(workspace/state/run_terminal/dispatch/llm), mocking the LLM and tool dispatch.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

from lunamoth.tools.builtin import execute_code as ec_mod
from lunamoth.tools.builtin import delegate_task as dt_mod
from lunamoth.tools.registry import registry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeState:
    def __init__(self, tool_access=None):
        self._access = tool_access if tool_access is not None else [
            "read_file", "write_file", "terminal", "web_search",
        ]

    def load(self):
        return {
            "tool_access": list(self._access),
            "network_access": False,
            "writable_paths": [],
            "isolation": "admin",
        }


class _FakeSandbox:
    def __init__(self, root: Path):
        self.root = root


class _FakeLLMCfg:
    model = "fake/model"


class _FakeLLM:
    """Drives delegate's scoped sub-turn. stream_agent yields the recorded
    events, optionally invoking the execute callback for a scripted tool call."""
    def __init__(self, *, events=None, tool_call=None, live=True):
        self.cfg = _FakeLLMCfg()
        self._events = events
        self._tool_call = tool_call
        self._live = live

    def is_live(self):
        return self._live

    def stream_agent(self, user_text, context, stable, volatile, tools, execute,
                     record=None, max_steps=8, in_context=True, channel="say"):
        from lunamoth.protocol.events import TextDelta
        if self._tool_call is not None:
            # Fire one tool call so the trace + dispatch path is exercised.
            execute(self._tool_call)
        if self._events is not None:
            for ev in self._events:
                yield ev
        else:
            yield TextDelta("done summary", channel)


def make_ctx(tmp_path, *, dispatch=None, llm=None, tool_access=None,
             terminal_output="OK", terminal_records=None):
    from lunamoth.tools.context import ToolContext
    root = tmp_path / "chara"
    (root / "workspace").mkdir(parents=True, exist_ok=True)

    def _run_terminal(command, *, timeout, workdir=None):
        if terminal_records is not None:
            terminal_records.append({"command": command, "timeout": timeout, "workdir": workdir})
        return terminal_output

    ctx = ToolContext(
        sandbox=_FakeSandbox(root),
        state=_FakeState(tool_access),
        audit=types.SimpleNamespace(write=lambda *a, **k: None),
        llm=llm,
        dispatch=dispatch,
    )
    ctx.run_terminal = _run_terminal  # type: ignore[method-assign]
    # execute_code now mirrors the gateway's effective set; in tests the fake
    # state's tool_access expresses that set.
    ctx.enabled_tool_names = lambda: set(ctx.state.load().get("tool_access") or [])
    return ctx


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_both_tools_registered():
    names = registry.get_all_tool_names()
    assert "execute_code" in names
    assert "delegate_task" in names
    assert registry.get_entry("execute_code").toolset == "code_execution"
    assert registry.get_entry("delegate_task").toolset == "delegation"


def test_execute_code_schema_shape():
    schema = registry.get_schema("execute_code")
    assert "description" in schema
    assert schema["parameters"]["required"] == ["code"]
    assert "code" in schema["parameters"]["properties"]


def test_delegate_schema_shape():
    schema = registry.get_schema("delegate_task")
    props = schema["parameters"]["properties"]
    assert set(props) >= {"goal", "context", "toolsets", "tasks"}
    assert schema["parameters"]["required"] == []
    # The new reality: true parallel in-process fan-out (no "SEQUENTIAL" claim).
    assert "PARALLEL" in schema["description"].upper()
    assert "SEQUENTIAL" not in schema["description"].upper()


# ---------------------------------------------------------------------------
# execute_code
# ---------------------------------------------------------------------------
def test_execute_code_empty_code(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}")
    out = json.loads(ec_mod.execute_code({"code": "  "}, ctx))
    assert "error" in out


def test_execute_code_no_dispatch(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=None)
    out = json.loads(ec_mod.execute_code({"code": "print(1)"}, ctx))
    assert "error" in out


def test_execute_code_runs_and_returns_stdout(tmp_path):
    records = []
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}",
                   terminal_output="hello from sandbox\n", terminal_records=records)
    out = json.loads(ec_mod.execute_code({"code": "print('hello from sandbox')"}, ctx))
    assert out["status"] == "success"
    assert "hello from sandbox" in out["output"]
    assert out["tool_calls_made"] == 0
    assert "duration_seconds" in out
    # A python child was launched inside the isolation.
    assert records and "python3 script.py" in records[0]["command"]
    # Staging dir cleaned up.
    assert not list((tmp_path / "chara" / "workspace").glob(".execute_code_*"))


def test_execute_code_secret_redaction(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}",
                   terminal_output="token=sk-ABCDEFGHIJKLMNOPQRSTUV done")
    out = json.loads(ec_mod.execute_code({"code": "print('x')"}, ctx))
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in out["output"]
    assert "[REDACTED]" in out["output"]


def test_execute_code_stdout_truncation(tmp_path):
    big = "A" * (ec_mod.MAX_STDOUT_BYTES + 5000)
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", terminal_output=big)
    out = json.loads(ec_mod.execute_code({"code": "print('x')"}, ctx))
    assert "OUTPUT TRUNCATED" in out["output"]
    assert len(out["output"]) < len(big)


def test_execute_code_rpc_server_dispatches(tmp_path):
    """The RPC server thread services a req_ file and writes a res_ file via
    ctx.dispatch, enforcing the allowlist."""
    import threading
    calls = []

    def fake_dispatch(name, args):
        calls.append((name, args))
        return json.dumps({"ok": True, "echo": args})

    rpc_dir = tmp_path / "rpc"
    rpc_dir.mkdir()
    stop = threading.Event()
    counter = [0]
    log = []
    th = threading.Thread(
        target=ec_mod._rpc_server_loop,
        args=(rpc_dir, fake_dispatch, log, counter, 50,
              ec_mod.SANDBOX_ALLOWED_TOOLS, stop),
        daemon=True,
    )
    th.start()
    try:
        # Allowed tool.
        (rpc_dir / "req_000001").write_text(
            json.dumps({"tool": "read_file", "args": {"path": "x"}}), encoding="utf-8")
        res = _wait_for(rpc_dir / "res_000001")
        assert json.loads(res)["echo"] == {"path": "x"}
        # Disallowed tool — never dispatched.
        (rpc_dir / "req_000002").write_text(
            json.dumps({"tool": "memory", "args": {}}), encoding="utf-8")
        res2 = _wait_for(rpc_dir / "res_000002")
        assert "error" in json.loads(res2)
        assert all(name != "memory" for name, _ in calls)
    finally:
        stop.set()
        th.join(timeout=2)


def test_execute_code_rpc_strips_terminal_blocked_params(tmp_path):
    import threading
    seen = []

    def fake_dispatch(name, args):
        seen.append((name, dict(args)))
        return "{}"

    rpc_dir = tmp_path / "rpc"
    rpc_dir.mkdir()
    stop = threading.Event()
    th = threading.Thread(
        target=ec_mod._rpc_server_loop,
        args=(rpc_dir, fake_dispatch, [], [0], 50,
              ec_mod.SANDBOX_ALLOWED_TOOLS, stop),
        daemon=True,
    )
    th.start()
    try:
        (rpc_dir / "req_000001").write_text(json.dumps({
            "tool": "terminal",
            "args": {"command": "ls", "background": True, "pty": True},
        }), encoding="utf-8")
        _wait_for(rpc_dir / "res_000001")
        assert seen and "background" not in seen[0][1] and "pty" not in seen[0][1]
        assert seen[0][1]["command"] == "ls"
    finally:
        stop.set()
        th.join(timeout=2)


def test_execute_code_rpc_call_limit(tmp_path):
    import threading
    rpc_dir = tmp_path / "rpc"
    rpc_dir.mkdir()
    stop = threading.Event()
    counter = [50]  # already at the cap
    th = threading.Thread(
        target=ec_mod._rpc_server_loop,
        args=(rpc_dir, lambda n, a: "{}", [], counter, 50,
              ec_mod.SANDBOX_ALLOWED_TOOLS, stop),
        daemon=True,
    )
    th.start()
    try:
        (rpc_dir / "req_000001").write_text(
            json.dumps({"tool": "read_file", "args": {}}), encoding="utf-8")
        res = _wait_for(rpc_dir / "res_000001")
        assert "Tool call limit reached" in json.loads(res)["error"]
    finally:
        stop.set()
        th.join(timeout=2)


def test_generate_tools_module_intersection():
    src = ec_mod.generate_tools_module(["read_file", "terminal", "web_search"], "/tmp/rpc")
    assert "def read_file(" in src
    assert "def terminal(" in src
    assert "def web_search(" in src
    # Not enabled -> no stub.
    assert "def patch(" not in src
    assert "def json_parse(" in src and "def retry(" in src


def test_check_sandbox_requirements_posix():
    # On the CI/dev mac+linux targets this is True.
    assert ec_mod.check_sandbox_requirements() is True


def _wait_for(path: Path, timeout=3.0) -> str:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


# ---------------------------------------------------------------------------
# delegate_task
# ---------------------------------------------------------------------------
def test_delegate_requires_live_llm(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_FakeLLM(live=False))
    out = json.loads(dt_mod.delegate_task({"goal": "do x"}, ctx))
    assert "error" in out


def test_delegate_requires_goal_or_tasks(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_FakeLLM())
    out = json.loads(dt_mod.delegate_task({}, ctx))
    assert "error" in out


def test_delegate_missing_task_goal(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_FakeLLM())
    out = json.loads(dt_mod.delegate_task({"tasks": [{"context": "no goal"}]}, ctx))
    assert "error" in out


def test_delegate_too_many_tasks(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_FakeLLM())
    tasks = [{"goal": f"t{i}"} for i in range(dt_mod.MAX_TASKS + 1)]
    out = json.loads(dt_mod.delegate_task({"tasks": tasks}, ctx))
    assert "error" in out and "Too many tasks" in out["error"]


def test_delegate_single_goal_runs_subturn(tmp_path):
    from lunamoth.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("Did the thing.", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    out = json.loads(dt_mod.delegate_task({"goal": "summarize"}, ctx))
    assert "results" in out
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["status"] == "completed"
    assert r["summary"] == "Did the thing."
    assert r["task_index"] == 0
    assert "tool_trace" in r


def test_delegate_batch_parallel_in_order(tmp_path):
    from lunamoth.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    out = json.loads(dt_mod.delegate_task(
        {"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]}, ctx))
    # N tasks -> N results, re-ordered to original task index.
    assert [r["task_index"] for r in out["results"]] == [0, 1, 2]
    assert all(r["status"] == "completed" for r in out["results"])


def test_delegate_batch_actually_concurrent(tmp_path):
    """Workers run on separate threads concurrently: a barrier that needs all
    workers present to release only completes if they overlap."""
    import threading
    from lunamoth.protocol.events import TextDelta

    n = 3
    barrier = threading.Barrier(n, timeout=5)
    seen_threads: set[int] = set()

    class _BarrierLLM:
        cfg = _FakeLLMCfg()

        def is_live(self):
            return True

        def stream_agent(self, user_text, context, stable, volatile, tools,
                         execute, record=None, max_steps=8, in_context=True,
                         channel="say"):
            seen_threads.add(threading.get_ident())
            barrier.wait()  # only releases if all n workers are here at once
            yield TextDelta("done", channel)

    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_BarrierLLM())
    out = json.loads(dt_mod.delegate_task(
        {"tasks": [{"goal": f"t{i}"} for i in range(n)]}, ctx))
    assert len(out["results"]) == n
    assert all(r["status"] == "completed" for r in out["results"])
    # Each worker ran on its own thread (true fan-out, not the caller thread).
    assert len(seen_threads) == n


def test_delegate_depth_cap_rejects_grandchild(tmp_path):
    """A ctx already at delegate_depth>=MAX_DEPTH (i.e. inside a worker) refuses
    to spawn — no grandchildren."""
    from lunamoth.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    ctx.delegate_depth = dt_mod.MAX_DEPTH
    out = json.loads(dt_mod.delegate_task({"goal": "spawn more"}, ctx))
    assert "error" in out and "nested" in out["error"].lower()


def test_delegate_spawn_pause(tmp_path):
    from lunamoth.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    assert dt_mod.set_spawn_paused(True) is True
    try:
        out = json.loads(dt_mod.delegate_task({"goal": "x"}, ctx))
        assert "error" in out and "paused" in out["error"].lower()
    finally:
        dt_mod.set_spawn_paused(False)
    # Unpaused again, it runs.
    out2 = json.loads(dt_mod.delegate_task({"goal": "x"}, ctx))
    assert out2["results"][0]["status"] == "completed"


def test_delegate_per_worker_llm_client(tmp_path):
    """Each worker gets its OWN LLMClient (cloned from cfg) so concurrent workers
    never share the client's mutable per-stream state. We patch the LLMClient
    symbol delegate_task imports and count constructions. The parent ctx.llm must
    itself be (an instance of) the patched LLMClient so the clone path engages."""
    constructed = []

    class _SpyClient:
        def __init__(self, cfg):
            self.cfg = cfg
            constructed.append(self)  # hold the object so ids can't be reused

        def is_live(self):
            return True

        def stream_agent(self, *a, **k):
            from lunamoth.protocol.events import TextDelta
            yield TextDelta("done", k.get("channel", "say"))

    import lunamoth.core.llm as llm_mod
    orig = llm_mod.LLMClient
    llm_mod.LLMClient = _SpyClient
    try:
        parent = _SpyClient(_FakeLLMCfg())   # ctx.llm IS an LLMClient (the spy)
        constructed.clear()                  # ignore the parent's construction
        ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=parent)
        json.loads(dt_mod.delegate_task(
            {"tasks": [{"goal": "a"}, {"goal": "b"}]}, ctx))
    finally:
        llm_mod.LLMClient = orig
    # Two workers -> two distinct fresh clients, neither is the parent.
    assert len(constructed) == 2
    assert len({id(c) for c in constructed}) == 2
    assert parent not in constructed


def test_delegate_blocks_forbidden_tool(tmp_path):
    """A subagent attempting a blocked tool gets a refusal in its trace, and the
    parent dispatch is never invoked for it."""
    from lunamoth.protocol.events import TextDelta
    dispatched = []
    tool_call = {"function": {"name": "memory", "arguments": "{}"}}
    llm = _FakeLLM(events=[TextDelta("done", "muse")], tool_call=tool_call)
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: dispatched.append(n) or "{}", llm=llm)
    out = json.loads(dt_mod.delegate_task({"goal": "try memory"}, ctx))
    r = out["results"][0]
    assert any(t["status"] == "blocked" for t in r["tool_trace"])
    assert "memory" not in dispatched


def test_delegate_allowed_tool_dispatches(tmp_path):
    from lunamoth.protocol.events import TextDelta
    dispatched = []

    def disp(name, args):
        dispatched.append(name)
        return json.dumps({"ok": True})

    # read_file is in tool_access; with no toolsets the child inherits all
    # non-blocked tools, so read_file must be reachable.
    tool_call = {"function": {"name": "read_file", "arguments": json.dumps({"path": "x"})}}
    llm = _FakeLLM(events=[TextDelta("done", "muse")], tool_call=tool_call)
    ctx = make_ctx(tmp_path, dispatch=disp, llm=llm)
    out = json.loads(dt_mod.delegate_task({"goal": "read it"}, ctx))
    r = out["results"][0]
    # read_file is a real registered tool (search group). If present it
    # dispatches; if that sibling group isn't loaded, it's blocked-as-unknown.
    trace = r["tool_trace"]
    assert trace and trace[0]["tool"] == "read_file"


def test_delegate_blocked_tools_constant():
    # The blocked set is preserved (hermes parity + LunaMoth chara-life tools).
    assert {"delegate_task", "memory", "execute_code", "speak"} <= dt_mod.DELEGATE_BLOCKED_TOOLS


def test_subagent_toolsets_excludes_delegation_and_code():
    ts = dt_mod._subagent_toolsets()
    assert "delegation" not in ts
    assert "code_execution" not in ts


def test_execute_code_runs_from_workspace_not_double_cd(tmp_path):
    """Regression: execute_code must NOT pass workdir=stage_dir. Doing so made the
    real cwd the stage dir under sandbox-darwin/admin isolation, so the command's own
    `cd {rel}` double-applied (stage_dir/.execute_code_* → not found): the script
    never ran yet status=success. cwd must default to the workspace, with a single
    relative cd into the stage dir."""
    records = []
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}",
                   terminal_output="ok\n", terminal_records=records)
    ec_mod.execute_code({"code": "print('ok')"}, ctx)
    assert records, "run_terminal was not called"
    rec = records[0]
    assert rec["workdir"] is None, "must not pass workdir=stage_dir (the double-cd bug)"
    assert rec["command"].startswith("cd ") and ".execute_code_" in rec["command"]
