"""Tests for the ported execute_code + delegate_task tools.

These exercise each tool against a tmp workspace with a minimal fake ctx
(workspace/state/run_terminal/dispatch/llm), mocking the LLM and tool dispatch.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

from chara.tools.builtin import execute_code as ec_mod
from chara.tools.builtin import delegate_task as dt_mod
from chara.tools.registry import registry


def _drain_delegate(ctx, args, timeout=5.0):
    """delegate_task is NON-BLOCKING: it submits a background fan-out and returns
    {status: submitted}. This helper submits, then blocks on the registry's
    completion queue and returns the drained completion event — which carries the
    same `results` array the old synchronous call returned. On a validation/paused/
    depth error (no job started) it returns the error dict instead."""
    out = json.loads(dt_mod.delegate_task(args, ctx))
    if out.get("status") != "submitted":
        return out
    from chara.tools.builtin._process_registry import get_registry
    return get_registry(ctx).completion_queue.get(timeout=timeout)


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
        from chara.protocol.events import TextDelta
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
    from chara.tools.context import ToolContext
    root = tmp_path / "chara"
    (root / "workspace").mkdir(parents=True, exist_ok=True)

    def _run_terminal(command, *, timeout, workdir=None):
        if terminal_records is not None:
            terminal_records.append({"command": command, "timeout": timeout, "workdir": workdir})
        return terminal_output

    def _run_terminal_result(command, *, timeout, workdir=None, browser=False):
        from chara.tools.runner import TerminalResult
        text = _run_terminal(command, timeout=timeout, workdir=workdir)
        # Default the fake to a clean exit (0) so execute_code reports success;
        # a non-zero "exit=N" prefix in terminal_output is honored for error-path tests.
        code = 0
        if text.startswith("exit="):
            try:
                code = int(text.split("\n", 1)[0].split("=", 1)[1])
            except ValueError:
                code = 0
        return TerminalResult(text=text, exit_code=code)

    ctx = ToolContext(
        sandbox=_FakeSandbox(root),
        state=_FakeState(tool_access),
        audit=types.SimpleNamespace(write=lambda *a, **k: None),
        llm=llm,
        dispatch=dispatch,
    )
    ctx.run_terminal = _run_terminal  # type: ignore[method-assign]
    ctx.run_terminal_result = _run_terminal_result  # type: ignore[method-assign]
    # execute_code now mirrors the gateway's effective set; in tests the fake
    # state's tool_access expresses that set.
    ctx.enabled_tool_names = lambda: set(ctx.state.load().get("tool_access") or [])
    return ctx


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_both_tools_registered():
    """Both live again: delegate_task was re-enabled once it became non-blocking
    with an enforced per-child timeout (delegate_task._DELEGATE_ENABLED)."""
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
    # Read the module-level schema directly — the tool is shelved (unregistered),
    # but the code (and its schema) is kept intact for when it's re-enabled.
    schema = dt_mod.DELEGATE_TASK_SCHEMA
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


def test_execute_code_nonzero_exit_is_error_not_success(tmp_path):
    # A script that exits non-zero must report status=error with the real code —
    # the old substring-scan reported success unless the output said "timed out".
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}",
                   terminal_output="exit=1\nSTDERR:\nTraceback ... SystemExit")
    out = json.loads(ec_mod.execute_code({"code": "raise SystemExit(1)"}, ctx))
    assert out["status"] == "error"
    assert out["exit_code"] == 1
    assert "exited with code 1" in out["error"]


def test_execute_code_secret_redaction(tmp_path):
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}",
                   terminal_output="token=sk-ABCDEFGHIJKLMNOPQRSTUV done")
    out = json.loads(ec_mod.execute_code({"code": "print('x')"}, ctx))
    # The central redactor (core.redact) masks the secret to a partial form
    # (sk-ABC...STUV) — the FULL secret never reaches the model's context.
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in out["output"]
    assert "sk-ABC...STUV" in out["output"]


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
        _write_req(rpc_dir, "req_000001", {"tool": "read_file", "args": {"path": "x"}})
        res = _wait_for(rpc_dir / "res_000001")
        assert json.loads(res)["echo"] == {"path": "x"}
        # Disallowed tool — never dispatched.
        _write_req(rpc_dir, "req_000002", {"tool": "memory", "args": {}})
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
        _write_req(rpc_dir, "req_000001", {
            "tool": "terminal",
            "args": {"command": "ls", "background": True, "pty": True},
        })
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
        _write_req(rpc_dir, "req_000001", {"tool": "read_file", "args": {}})
        res = _wait_for(rpc_dir / "res_000001")
        assert "Tool call limit reached" in json.loads(res)["error"]
    finally:
        stop.set()
        th.join(timeout=2)


def test_generate_tools_module_intersection():
    src = ec_mod.generate_tools_module(["read_file", "terminal", "web_search"], "/tmp/rpc")
    assert "def read_file(" in src
    assert "def terminal(" in src
    # web tools are SHELVED (web.py _WEB_TOOLS_ENABLED=False → never registered),
    # so no stub is generated even when a caller lists them as enabled.
    assert "def web_search(" not in src
    # Not enabled -> no stub.
    assert "def patch(" not in src
    assert "def json_parse(" in src and "def retry(" in src


def test_schema_omits_shelved_web_tools():
    """The model-facing schema must not advertise the shelved web tools — a
    script calling them dead-ends on the RPC allowlist (they're unregistered)."""
    desc = ec_mod.EXECUTE_CODE_SCHEMA["description"]
    assert "web_search" not in desc
    assert "web_extract" not in desc
    code_desc = ec_mod.EXECUTE_CODE_SCHEMA["parameters"]["properties"]["code"]["description"]
    assert "web_search" not in code_desc
    assert "web_search" not in ec_mod.SANDBOX_ALLOWED_TOOLS
    assert "web_extract" not in ec_mod.SANDBOX_ALLOWED_TOOLS


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


def _write_req(rpc_dir: Path, name: str, payload: dict) -> None:
    """Write a req_* file ATOMICALLY (tmp + rename) — exactly like the real producer
    (_rpc_send). The loop polls on a short interval and skips *.tmp, so this is what
    keeps it from reading a half-written request (a non-atomic write_text races the
    poll, the partial JSON fails to parse, an error response is written without ever
    dispatching — the source of the macOS-CI flake)."""
    req = rpc_dir / name
    tmp = rpc_dir / (name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.rename(req)


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
    from chara.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("Did the thing.", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    out = _drain_delegate(ctx, {"goal": "summarize"})
    assert "results" in out
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["status"] == "completed"
    assert r["summary"] == "Did the thing."
    assert r["task_index"] == 0
    assert "tool_trace" in r


def test_delegate_submit_is_non_blocking(tmp_path):
    """The call returns immediately with a submit receipt — it does NOT block for
    results (the whole point: subagents run alongside the main agent)."""
    from chara.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    out = json.loads(dt_mod.delegate_task({"goal": "x"}, ctx))
    assert out["status"] == "submitted"
    assert out["n"] == 1 and "job_id" in out
    assert "results" not in out  # didn't wait for them


def test_delegate_per_child_timeout_is_enforced(tmp_path, monkeypatch):
    """A worker that stalls past the per-child timeout becomes a real `timed_out`
    result instead of hanging the fan-out (the fix for the 'stuck' failure mode)."""
    monkeypatch.setattr(dt_mod, "DEFAULT_PER_CHILD_TIMEOUT", 0.2)

    class _SlowLLM:
        cfg = _FakeLLMCfg()

        def is_live(self):
            return True

        def stream_agent(self, *a, **k):
            import time as _t
            _t.sleep(1.0)  # outlives the 0.2s per-child limit
            from chara.protocol.events import TextDelta
            yield TextDelta("late", k.get("channel", "say"))

    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=_SlowLLM())
    evt = _drain_delegate(ctx, {"goal": "slow"}, timeout=5.0)
    assert evt["results"][0]["status"] == "timed_out"


def test_delegate_completion_event_formats(tmp_path):
    """The delegate completion event renders as a model-facing notice line."""
    from chara.tools.builtin._process_registry import format_background_notification
    line = format_background_notification({
        "type": "delegate", "status": "done",
        "results": [{"task_index": 0, "status": "completed", "summary": "found it"}],
    })
    assert "subtask 0" in line and "found it" in line
    fail = format_background_notification({"type": "delegate", "status": "failed",
                                           "error": "boom"})
    assert "FAILED" in fail and "boom" in fail


def test_delegate_batch_parallel_in_order(tmp_path):
    from chara.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    out = _drain_delegate(ctx, {"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]})
    # N tasks -> N results, re-ordered to original task index.
    assert [r["task_index"] for r in out["results"]] == [0, 1, 2]
    assert all(r["status"] == "completed" for r in out["results"])


def test_delegate_batch_actually_concurrent(tmp_path):
    """Workers run on separate threads concurrently: a barrier that needs all
    workers present to release only completes if they overlap."""
    import threading
    from chara.protocol.events import TextDelta

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
    out = _drain_delegate(ctx, {"tasks": [{"goal": f"t{i}"} for i in range(n)]})
    assert len(out["results"]) == n
    assert all(r["status"] == "completed" for r in out["results"])
    # Each worker ran on its own thread (true fan-out, not the caller thread).
    assert len(seen_threads) == n


def test_delegate_depth_cap_rejects_grandchild(tmp_path):
    """A ctx already at delegate_depth>=MAX_DEPTH (i.e. inside a worker) refuses
    to spawn — no grandchildren."""
    from chara.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    ctx.delegate_depth = dt_mod.MAX_DEPTH
    out = json.loads(dt_mod.delegate_task({"goal": "spawn more"}, ctx))
    assert "error" in out and "nested" in out["error"].lower()


def test_delegate_spawn_pause(tmp_path):
    from chara.protocol.events import TextDelta
    llm = _FakeLLM(events=[TextDelta("ok", "muse")])
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=llm)
    assert dt_mod.set_spawn_paused(True) is True
    try:
        out = json.loads(dt_mod.delegate_task({"goal": "x"}, ctx))
        assert "error" in out and "paused" in out["error"].lower()
    finally:
        dt_mod.set_spawn_paused(False)
    # Unpaused again, it runs.
    out2 = _drain_delegate(ctx, {"goal": "x"})
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
            from chara.protocol.events import TextDelta
            yield TextDelta("done", k.get("channel", "say"))

    import chara.core.llm as llm_mod
    orig = llm_mod.LLMClient
    llm_mod.LLMClient = _SpyClient
    try:
        parent = _SpyClient(_FakeLLMCfg())   # ctx.llm IS an LLMClient (the spy)
        constructed.clear()                  # ignore the parent's construction
        ctx = make_ctx(tmp_path, dispatch=lambda n, a: "{}", llm=parent)
        _drain_delegate(ctx, {"tasks": [{"goal": "a"}, {"goal": "b"}]})
    finally:
        llm_mod.LLMClient = orig
    # Two workers -> two distinct fresh clients, neither is the parent.
    assert len(constructed) == 2
    assert len({id(c) for c in constructed}) == 2
    assert parent not in constructed


def test_delegate_blocks_forbidden_tool(tmp_path):
    """A subagent attempting a blocked tool gets a refusal in its trace, and the
    parent dispatch is never invoked for it."""
    from chara.protocol.events import TextDelta
    dispatched = []
    tool_call = {"function": {"name": "memory", "arguments": "{}"}}
    llm = _FakeLLM(events=[TextDelta("done", "muse")], tool_call=tool_call)
    ctx = make_ctx(tmp_path, dispatch=lambda n, a: dispatched.append(n) or "{}", llm=llm)
    out = _drain_delegate(ctx, {"goal": "try memory"})
    r = out["results"][0]
    assert any(t["status"] == "blocked" for t in r["tool_trace"])
    assert "memory" not in dispatched


def test_delegate_allowed_tool_dispatches(tmp_path):
    from chara.protocol.events import TextDelta
    dispatched = []

    def disp(name, args):
        dispatched.append(name)
        return json.dumps({"ok": True})

    # read_file is in tool_access; with no toolsets the child inherits all
    # non-blocked tools, so read_file must be reachable.
    tool_call = {"function": {"name": "read_file", "arguments": json.dumps({"path": "x"})}}
    llm = _FakeLLM(events=[TextDelta("done", "muse")], tool_call=tool_call)
    ctx = make_ctx(tmp_path, dispatch=disp, llm=llm)
    out = _drain_delegate(ctx, {"goal": "read it"})
    r = out["results"][0]
    # read_file is a real registered tool (search group). If present it
    # dispatches; if that sibling group isn't loaded, it's blocked-as-unknown.
    trace = r["tool_trace"]
    assert trace and trace[0]["tool"] == "read_file"


def test_delegate_blocked_tools_constant():
    # The blocked set is preserved (hermes parity + OpenCharaAgent chara-life tools).
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


# ---- per-child timeout semantics (2026-07-02 audit P2) ----------------------------
# The budget must run from each child's OWN start: the old sequential
# fut.result(timeout=600) gave task N a budget of 600s × its wait position and
# reported queued-but-never-started tasks as "timed out" (a fabricated result).


def test_fanout_per_child_timeout_is_from_own_start(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setattr(dt_mod, "DEFAULT_PER_CHILD_TIMEOUT", 1)

    def fake_child(i, goal, context, toolsets, ctx, max_iter):
        if goal == "hang":
            _time.sleep(5.0)
        else:
            _time.sleep(0.05)
        return dt_mod._result(i, "done", summary=f"ok-{i}")

    monkeypatch.setattr(dt_mod, "_run_single_child", fake_child)
    tasks = [{"goal": "hang"}, {"goal": "a"}, {"goal": "b"}, {"goal": "c"}]
    t0 = _time.monotonic()
    results = dt_mod._run_fanout(tasks, None, None, ctx=None, max_iter=3)
    elapsed = _time.monotonic() - t0
    # The hung child times out on ITS budget; the quick ones — including the one
    # queued behind it — complete for real, and the batch returns promptly.
    assert results[0]["status"] == "timed_out"
    assert [r["status"] for r in results[1:]] == ["done", "done", "done"]
    assert [r["summary"] for r in results[1:]] == ["ok-1", "ok-2", "ok-3"]
    assert elapsed < 4.0


def test_fanout_wedged_slots_report_never_started(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setattr(dt_mod, "DEFAULT_PER_CHILD_TIMEOUT", 1)

    def fake_child(i, goal, context, toolsets, ctx, max_iter):
        _time.sleep(10.0)  # wedge every slot
        return dt_mod._result(i, "done", summary=f"late-{i}")

    monkeypatch.setattr(dt_mod, "_run_single_child", fake_child)
    tasks = [{"goal": "h1"}, {"goal": "h2"}, {"goal": "h3"}, {"goal": "queued"}]
    results = dt_mod._run_fanout(tasks, None, None, ctx=None, max_iter=3)
    assert [r["status"] for r in results[:3]] == ["timed_out"] * 3
    # The queued task never ran: reported as never-started, not fabricated as
    # having exceeded a budget it never got.
    assert results[3]["status"] == "timed_out"
    assert "never started" in results[3]["error"]
