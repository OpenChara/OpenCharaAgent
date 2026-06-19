"""delegate_task — subagent delegation, apple-to-apple with hermes-agent
(reference/hermes-agent/tools/delegate_tool.py), RE-SHAPED for LunaMoth's
one-process-one-chara runtime.

  Hermes spawns N child agents IN-PROCESS via a ThreadPoolExecutor (each its own
  fresh conversation, restricted toolset, focused prompt) and blocks the parent
  until all finish. LunaMoth does the SAME: one-process-one-chara does NOT forbid
  in-process worker threads — the workers share the chara's ONE sandbox/state/
  registry exactly as hermes workers share the session workspace. There is no
  sibling-agent wall here; the earlier "IMPOSSIBLE" claim was overstated.

  Each worker runs a bounded inner LLM loop with:
    - its OWN ``LLMClient(cfg)`` (cfg is a frozen dataclass, safe to reuse), so
      concurrent ``stream_agent`` calls never corrupt the shared client's
      ``last_usage``/``last_prompt_tokens``/httpx state;
    - a *fresh, ephemeral* message context (no chara history, nothing persisted);
    - a *restricted tool subset* (requested/inherited toolsets minus the
      always-blocked set);
    - its OWN loop-guardrail scope (``ctx.spawn_worker_dispatch()``) over the
      chara's gateway, so a worker's repeated failures never bleed into the
      parent chara's guardrail counters — the gateway's ``_dispatch_lock``
      serializes the shared audit + registry dispatch + the per-session scratch
      stores (processes/todo/browser) that tool bodies touch.
  Only the worker's final text returns — intermediate tool results never reach
  the parent context.

  TRUE PARALLEL FAN-OUT: up to ``MAX_CONCURRENT`` workers run concurrently
  (hermes ``_DEFAULT_MAX_CONCURRENT_CHILDREN=3``); results are re-ordered to the
  original task index. Depth is capped at ``MAX_DEPTH=1`` (parent → worker; a
  worker cannot itself spawn workers — grandchild rejected), with a global
  spawn-pause hook (``set_spawn_paused``). The remaining true constraint is
  one-process-one-chara — fine, because workers share the chara sandbox BY
  DESIGN, just like hermes. Dropped vs hermes: ACP transports, provider pools,
  fallback chains, nested orchestration roles.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..registry import registry, tool_error

logger = logging.getLogger("lunamoth.tools.delegate_task")

# ---------------------------------------------------------------------------
# Global spawn-pause (hermes set_spawn_paused / is_spawn_paused, :151-175).
# Active workers keep running; only NEW delegate_task calls fail fast while
# paused. Module-level so it spans every invocation in the process.
# ---------------------------------------------------------------------------
_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False


def set_spawn_paused(paused: bool) -> bool:
    """Globally block/unblock new delegate_task spawns. Returns the new state."""
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused


def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused

# Tools a child must never reach (hermes DELEGATE_BLOCKED_TOOLS, :45-53),
# adapted to LunaMoth's tool names. No recursive delegation, no memory writes,
# no execute_code, no speak/messaging side-effects from a scoped worker.
DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",
    "memory",
    "execute_code",
    "speak",
    "rest",
])

DEFAULT_MAX_ITERATIONS = 50       # hermes :593
DEFAULT_PER_CHILD_TIMEOUT = 600   # hermes per-child timeout (advisory here)
MAX_DEPTH = 1                     # flat: parent -> leaf child only (hermes MAX_DEPTH :133)
MAX_CONCURRENT = 3                # hermes _DEFAULT_MAX_CONCURRENT_CHILDREN :132
MAX_TASKS = 8                     # batch cap (workers run pooled at MAX_CONCURRENT)


def check_delegate_requirements() -> bool:
    """delegate_task needs a live LLM client and a tool dispatcher; the registry
    check_fn cannot see ctx, so this is a coarse always-available gate (the
    handler itself returns a clear error if ctx is missing pieces)."""
    return True


def _subagent_toolsets() -> list[str]:
    """Toolsets a child may request — every registry toolset that still has at
    least one non-blocked tool, excluding the delegation/code toolsets."""
    seen: dict[str, list[str]] = {}
    for name in registry.get_all_tool_names():
        entry = registry.get_entry(name)
        if entry is None:
            continue
        seen.setdefault(entry.toolset, []).append(name)
    out = []
    for ts, tools in seen.items():
        if ts in ("delegation", "code_execution"):
            continue
        if ts.startswith("mcp-"):
            continue
        if all(t in DELEGATE_BLOCKED_TOOLS for t in tools):
            continue
        out.append(ts)
    return sorted(out)


def _tool_names_for_toolsets(toolsets) -> list[str]:
    """Resolve a list of toolset names → concrete tool names, minus the blocked
    set. ``toolsets`` None/empty → every non-blocked tool (inherit-all)."""
    want = set(toolsets or [])
    names = []
    for name in registry.get_all_tool_names():
        if name in DELEGATE_BLOCKED_TOOLS:
            continue
        entry = registry.get_entry(name)
        if entry is None:
            continue
        if want and entry.toolset not in want:
            continue
        names.append(name)
    return sorted(names)


def _build_child_system_prompt(goal: str, context: str | None) -> str:
    """Focused worker prompt (hermes _build_child_system_prompt, :603-644;
    orchestrator block dropped — children are always leaves here)."""
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "You have no access to the parent's conversation history — everything "
        "you need is in the task and context above. Be thorough but concise — "
        "your response is returned to the parent agent as a summary."
    )
    return "\n".join(parts)


def _make_worker_llm(ctx):
    """A FRESH LLMClient for one worker. cfg is a frozen dataclass (safe to
    reuse), but the client carries mutable per-stream state (last_usage,
    last_prompt_tokens, usage_fresh, the httpx session) — concurrent workers
    sharing the parent's client would corrupt those, so each gets its own.

    Only the REAL LLMClient is cloned; any other object (a test/mock double)
    IS the driver of the scoped sub-turn and must be reused as-is."""
    try:
        from ...core.llm import LLMClient
    except Exception:  # noqa: BLE001
        return ctx.llm
    if not isinstance(ctx.llm, LLMClient):
        return ctx.llm
    try:
        return LLMClient(ctx.llm.cfg)
    except Exception:  # noqa: BLE001 - fall back to the shared client rather than crash
        return ctx.llm


def _make_worker_dispatch(ctx):
    """A dispatch with its OWN guardrail scope for one worker, or the shared
    ctx.dispatch when the factory isn't wired (e.g. a bare test ctx)."""
    factory = getattr(ctx, "spawn_worker_dispatch", None)
    if callable(factory):
        try:
            return factory()
        except Exception:  # noqa: BLE001
            pass
    return ctx.dispatch


def _run_single_child(task_index: int, goal: str, context: str | None,
                      toolsets, ctx, max_iterations: int) -> dict:
    """Run one scoped sub-turn IN ITS OWN THREAD and collect a results entry
    (hermes _run_single_child shape). Own LLMClient + own dispatch guard scope so
    concurrent workers never corrupt shared state."""
    start = time.monotonic()
    tool_trace: list[dict] = []

    tool_names = _tool_names_for_toolsets(toolsets)
    try:
        schemas = registry.get_definitions(tool_names, quiet=True)
    except Exception as exc:  # noqa: BLE001
        return _result(task_index, "failed", error=f"could not build child toolset: {exc}",
                       duration=time.monotonic() - start)

    system_prompt = _build_child_system_prompt(goal, context)
    worker_llm = _make_worker_llm(ctx)
    worker_dispatch = _make_worker_dispatch(ctx)

    def _execute(tool_call: dict) -> dict:
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        # Hard guard: never let a blocked tool through even if the schema slips.
        if name in DELEGATE_BLOCKED_TOOLS or name not in tool_names:
            err = f"Tool '{name}' is not available to a delegated subagent."
            tool_trace.append({"tool": name, "args_bytes": len(raw),
                               "result_bytes": len(err), "status": "blocked"})
            return {"display": "", "content": f"ERROR: {err}", "ok": False}
        try:
            result = worker_dispatch(name, args)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            tool_trace.append({"tool": name, "args_bytes": len(raw),
                               "result_bytes": len(err), "status": "error"})
            return {"display": "", "content": f"ERROR: {err}", "ok": False}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        is_err = False
        try:
            parsed = json.loads(text)
            is_err = isinstance(parsed, dict) and "error" in parsed
        except (json.JSONDecodeError, TypeError):
            pass
        tool_trace.append({"tool": name, "args_bytes": len(raw),
                           "result_bytes": len(text), "status": "error" if is_err else "ok"})
        return {"display": "", "content": text, "ok": not is_err}

    # Drive a fresh, ephemeral inner loop. We feed the goal as the user turn and
    # the focused worker brief as the (only) stable system block. No chara
    # history, no volatile tail, nothing persisted.
    final_text_parts: list[str] = []
    try:
        from ...protocol.events import TextDelta
        for event in worker_llm.stream_agent(
            goal,
            context=[],
            stable=[system_prompt],
            volatile=[],
            tools=schemas,
            execute=_execute,
            record=None,
            max_steps=max_iterations,
            in_context=True,
            channel="muse",
        ):
            if isinstance(event, TextDelta):
                final_text_parts.append(event.text)
    except Exception as exc:  # noqa: BLE001
        logger.error("delegate child %d failed: %s", task_index, exc, exc_info=True)
        return _result(task_index, "failed", error=str(exc),
                       duration=time.monotonic() - start, tool_trace=tool_trace)

    summary = "".join(final_text_parts).strip()
    status = "completed" if summary else "failed"
    return _result(
        task_index, status,
        summary=summary or "(subagent produced no final summary)",
        duration=time.monotonic() - start,
        tool_trace=tool_trace,
        exit_reason="completed" if summary else "error",
        model=getattr(getattr(worker_llm, "cfg", None), "model", "") or "",
        error=None if summary else "subagent finished without a summary",
    )


def _result(task_index, status, *, summary="", error=None, duration=0.0,
            tool_trace=None, exit_reason=None, model="") -> dict:
    r = {
        "task_index": task_index,
        "status": status,
        "summary": summary,
        "duration_seconds": round(duration, 2),
        "model": model,
        "exit_reason": exit_reason or status,
        "tool_trace": tool_trace or [],
    }
    if error:
        r["error"] = error
    return r


def delegate_task(args: dict, ctx) -> str:
    """Fan out one or more scoped sub-turns concurrently; return a results array."""
    if ctx.llm is None or not getattr(ctx.llm, "is_live", lambda: False)():
        return tool_error("delegate_task requires a live LLM client.")
    if ctx.dispatch is None:
        return tool_error("delegate_task is unavailable: no tool dispatcher in this context.")
    # Depth cap (hermes MAX_DEPTH=1): a worker (depth>=1) may not spawn workers.
    # delegate_task is also in DELEGATE_BLOCKED_TOOLS so a worker's _execute
    # refuses it before dispatch — this is the explicit, directly-testable guard.
    if int(getattr(ctx, "delegate_depth", 0) or 0) >= MAX_DEPTH:
        return tool_error(
            "delegate_task cannot be nested: a delegated subagent may not spawn "
            "its own subagents (depth limit reached)."
        )
    # Global spawn-pause (hermes is_spawn_paused): refuse NEW spawns while paused.
    if is_spawn_paused():
        return tool_error("delegate_task spawning is paused; try again shortly.")

    goal = args.get("goal")
    context = args.get("context")
    toolsets = args.get("toolsets")
    tasks = args.get("tasks")
    # max_iterations is accepted but ignored if model-supplied — config is
    # authoritative (hermes :2030-2041).
    effective_max_iter = DEFAULT_MAX_ITERATIONS

    # Normalize to a task list (hermes :2061-2079).
    if tasks and isinstance(tasks, list):
        if len(tasks) > MAX_TASKS:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but the limit is "
                f"{MAX_TASKS}. Split into multiple delegate_task calls."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [{"goal": goal, "context": context, "toolsets": toolsets}]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    if not task_list:
        return tool_error("No tasks provided.")

    # Validate each task has a goal (hermes :2082-2088).
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(f"Task {i} must be an object, got {type(task).__name__}.")
        if not str(task.get("goal", "")).strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    # TRUE PARALLEL FAN-OUT (hermes ThreadPoolExecutor, capped at MAX_CONCURRENT).
    # A single task still runs (pool of 1). Each worker gets its own LLMClient +
    # guardrail scope inside _run_single_child; results re-ordered to task index.
    n = len(task_list)
    if n == 1:
        t = task_list[0]
        results = [_run_single_child(
            0, str(t["goal"]), t.get("context") or context,
            t.get("toolsets") or toolsets, ctx, effective_max_iter,
        )]
        return json.dumps({"results": results}, ensure_ascii=False)

    results_by_index: dict[int, dict] = {}
    workers = min(MAX_CONCURRENT, n)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="delegate") as pool:
        futures = {
            pool.submit(
                _run_single_child,
                i, str(t["goal"]), t.get("context") or context,
                t.get("toolsets") or toolsets, ctx, effective_max_iter,
            ): i
            for i, t in enumerate(task_list)
        }
        for fut, i in futures.items():
            try:
                results_by_index[i] = fut.result()
            except Exception as exc:  # noqa: BLE001 - never let one worker abort the batch
                logger.error("delegate worker %d crashed: %s", i, exc, exc_info=True)
                results_by_index[i] = _result(i, "failed", error=str(exc))

    results = [results_by_index[i] for i in range(n)]
    return json.dumps({"results": results}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schema (hermes DELEGATE_TASK_SCHEMA, :2773-2890) — re-shaped, ACP params dropped
# ---------------------------------------------------------------------------

def _delegate_description() -> str:
    return (
        "Delegate one or more self-contained tasks to a scoped subagent that "
        "runs in an ISOLATED context (no access to your conversation history) "
        "and returns only a final summary — intermediate tool results never "
        "enter your context. Use this to keep a reasoning-heavy or "
        "data-heavy subtask from flooding your own context window, or to run "
        "several independent subtasks AT THE SAME TIME.\n\n"
        "Subagents run on your own model with a restricted toolset; a batch runs "
        f"in PARALLEL (up to {MAX_CONCURRENT} at once) and the results come back "
        "in task order. A subagent cannot itself delegate. Blocked tools for "
        "subagents: " + ", ".join(sorted(DELEGATE_BLOCKED_TOOLS)) + "."
    )


def _build_dynamic_schema_overrides() -> dict:
    """Refresh the toolset hints from the live registry at get_definitions()
    time (hermes _build_dynamic_schema_overrides)."""
    toolset_str = ", ".join(f"'{n}'" for n in _subagent_toolsets())
    return {
        "description": _delegate_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What the subagent should accomplish. Be specific and "
                        "self-contained -- the subagent knows nothing about your "
                        "conversation history."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Background information the subagent needs: file paths, "
                        "error messages, project structure, constraints. The more "
                        "specific you are, the better the subagent performs."
                    ),
                },
                "toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Toolsets to enable for this subagent. "
                        "Default: inherits your enabled toolsets. "
                        f"Available toolsets: {toolset_str}."
                    ),
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "Task goal"},
                            "context": {"type": "string", "description": "Task-specific context"},
                            "toolsets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": f"Toolsets for this specific task. Available: {toolset_str}.",
                            },
                        },
                        "required": ["goal"],
                    },
                    "description": (
                        f"Batch mode: run up to {MAX_TASKS} tasks in PARALLEL "
                        f"(up to {MAX_CONCURRENT} at a time), each in its own "
                        "isolated context; results return in task order. "
                        "Provide this OR 'goal'."
                    ),
                },
            },
            "required": [],
        },
    }


DELEGATE_TASK_SCHEMA = _build_dynamic_schema_overrides()


registry.register(
    "delegate_task", "delegation",
    DELEGATE_TASK_SCHEMA,
    delegate_task,
    check_fn=check_delegate_requirements,
    emoji="🔀",
    max_result_size_chars=100_000,
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)
