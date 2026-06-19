"""ToolGateway — the thin dispatch shim over the hermes-ported tool registry.

The tool BODIES live in ``tools/builtin/*.py`` (each self-registers into
``tools.registry.registry`` at import). The gateway keeps the LunaMoth identity
the registry has no equivalent for:
  - the SECURITY audit trail (every call audited),
  - the #24 loop guardrails (warn@2 / refuse@5 / streak-block@8),
  - the gate: implemented(registry) ∩ pack.tools (the pack is the allowlist;
    runtime flags like network gate at call time, not by removing tools),
  - MCP dispatch (mcp__server__tool), and the {ok,data} result the agent loop
    consumes (`core/agent.py:_execute_tool`).

Handlers return a JSON STRING (hermes contract); the gateway classifies it
ok/failed (failed = a dict carrying a top-level "error") for the loop guard and
hands the string back as the model-facing content.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable

from ..obs.audit import AuditLog
from ..obs import get_logger
from .goals import GoalStore
from .mcp import McpError, McpManager
from .memory import MemoryStore
from .skills import SkillStore
from .sandbox import Sandbox
from .context import ToolContext
from .registry import registry, discover_builtin_tools
from ..core.state import EnvState

_log = get_logger("tools")

PERMISSION_KINDS = ("network", "writable_path", "memory", "other")

# Tool-loop guardrails (audit #24; shape of hermes agent/tool_guardrails.py).
GUARD_EXACT_WARN_AT = 2      # identical failing call: warn the model at the 2nd failure
GUARD_EXACT_REFUSE_AT = 5    # ... and refuse the 5th identical attempt outright
GUARD_STREAK_REFUSE_AT = 8   # consecutive failures of one tool (any args) before it is blocked

# Builtin tool modules self-register on first import of this package; do it once.
_DISCOVERED = False


def _ensure_discovered() -> None:
    global _DISCOVERED
    if not _DISCOVERED:
        discover_builtin_tools()
        _DISCOVERED = True


class ToolGateway:
    def __init__(
        self, sandbox: Sandbox, state: EnvState, audit: AuditLog,
        memory: MemoryStore | None = None, goals: "GoalStore | None" = None,
        skills: "SkillStore | None" = None, mcp: "McpManager | None" = None,
        llm: Any = None, transcript: Any = None,
    ):
        _ensure_discovered()
        self.sandbox = sandbox
        self.state = state
        self.audit = audit
        self.memory = memory
        self.goals = goals
        self.skills = skills
        self.mcp = mcp
        self.llm = llm
        self.transcript = transcript
        self.enabled_tools: set[str] | None = None
        self.mcp_allowed: list[str] = []
        self.permission_hook: "Callable[[str, str, str, int], bool] | None" = None
        self.clarify_hook: "Callable[[str, list], str] | None" = None
        self._guard_exact_failures: dict[str, int] = {}
        self._guard_tool_streaks: dict[str, int] = {}
        self._ctx_obj: ToolContext | None = None
        # Serializes the mutable state shared by concurrent dispatchers — the
        # loop-guardrail counters and the audit trail. The parent's tool loop is
        # single-threaded, but delegate_task fans out N in-process workers that
        # each call back through THIS gateway (shared sandbox = shared chara);
        # without the lock those concurrent dispatches would interleave guardrail
        # mutations and corrupt the parent's counters. (Each worker is also given
        # its OWN guardrail scope via spawn_worker_dispatch so a worker's failures
        # never touch the parent's streaks at all — the lock here only protects
        # the parent counters + the audit during true overlap.)
        self._dispatch_lock = threading.RLock()

    # ---- runtime binding (llm/transcript are built after the gateway) ----------------

    def set_runtime(self, *, llm: Any = None, transcript: Any = None) -> None:
        if llm is not None:
            self.llm = llm
        if transcript is not None:
            self.transcript = transcript
        self._ctx_obj = None  # rebuild ctx with the new refs

    def _ctx(self) -> ToolContext:
        if self._ctx_obj is None:
            self._ctx_obj = ToolContext(
                sandbox=self.sandbox, state=self.state, audit=self.audit,
                memory=self.memory, wishes=self.goals, skills=self.skills,
                mcp=self.mcp, llm=self.llm, transcript=self.transcript,
                permission_hook=self.permission_hook, clarify_hook=self.clarify_hook,
                dispatch=self._code_dispatch,
                enabled_tool_names=self._effective,  # single source: execute_code asks the gate
                spawn_worker_dispatch=self.spawn_worker_dispatch,
                delegate_depth=0,
            )
        # permission/clarify hooks are set after construction — keep them live.
        self._ctx_obj.permission_hook = self.permission_hook
        self._ctx_obj.clarify_hook = self.clarify_hook
        return self._ctx_obj

    def todo_injection(self) -> str | None:
        """The active todo list rendered for re-injection after a compaction.

        Hermes re-injects the live task list once the old window is summarized,
        so the model's in-progress work is never compressed away. Returns the
        text block (pending/in_progress items only) or None when there is no
        active task list. Reads the TodoStore stashed on the ToolContext by the
        todo tool (builtin/todo.py); no-op when the tool was never used."""
        ctx = self._ctx_obj
        if ctx is None:
            return None
        store = ctx._scratch.get("todo_store")
        fmt = getattr(store, "format_for_injection", None)
        if not callable(fmt):
            return None
        try:
            return fmt()
        except Exception:
            return None

    def _code_dispatch(self, name: str, args: dict) -> str:
        """The tool surface execute_code exposes to sandboxed Python: same gate +
        guard + audit as a model call, returning the raw JSON string."""
        res = self.call(name, **(args or {}))
        return res.get("data", "") if res.get("ok") else json.dumps({"error": res.get("error", "")}, ensure_ascii=False)

    # ---- pack / allowlist gating -----------------------------------------------------

    def set_enabled(self, tools: "list[str] | set[str] | None", mcp_servers: "list[str] | None" = None) -> None:
        self.enabled_tools = set(tools) if tools is not None else None
        self.mcp_allowed = self.mcp.allowed_servers(mcp_servers) if self.mcp else []

    def _effective(self) -> set[str]:
        """Callable builtin tools = registered ∩ active pack.

        The pack (set_enabled) is the per-chara allowlist; the registry is the
        source of truth for what exists. There is no third hand-kept list — a
        newly-registered tool in the pack is callable with nothing to edit.
        Runtime toggles like `/net off` gate at CALL time (network_access), they
        do not remove tools here."""
        if self.enabled_tools is None:
            return set()
        return set(registry.get_all_tool_names()) & self.enabled_tools

    def has_tools(self) -> bool:
        return bool(self._effective()) or bool(self.mcp_allowed)

    # ---- dispatch --------------------------------------------------------------------

    def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        """Run one tool: loop-guard refusal → dispatch → audit → guard record.
        Returns {"ok": bool, "data": <json str>} or {"ok": False, "error": str}.

        The whole sequence holds ``_dispatch_lock`` so that, when delegate_task
        workers dispatch concurrently, the parent's guardrail counters and the
        audit trail are never interleaved. The lock is re-entrant: a tool whose
        handler calls back into the gateway on the same thread won't deadlock."""
        with self._dispatch_lock:
            signature = self._guard_signature(name, kwargs)
            refusal = self._guard_refusal(name, signature)
            if refusal is not None:
                self.audit.write("tool_loop_refused", tool=name, args=self._safe_args(kwargs), result=refusal)
                _log.warning("%s refused by loop guard: %s", name, refusal["error"])
                return refusal
            if name.startswith("mcp__"):
                result = self._call_mcp(name, kwargs)
            else:
                result = self._dispatch(name, kwargs)
            return self._guard_record(name, signature, result)

    # ---- delegate_task worker isolation ----------------------------------------------

    def spawn_worker_dispatch(self) -> "Callable[[str, dict], str]":
        """A dispatch callable for ONE delegate_task worker, with its OWN
        loop-guardrail scope but sharing this gateway's pack gate, MCP, and
        (lock-serialized) audit + registry dispatch.

        Why a separate scope: a worker is a short-lived sub-turn; its repeated
        failures are the worker's problem, not the parent chara's — they must
        not bleed into ``_guard_tool_streaks`` and block the parent's next real
        turn. Why still go through this gateway: the worker shares the chara's
        ONE sandbox/state/registry, so the gate (registered ∩ pack), the audit
        trail, and tool bodies must be the chara's. Concurrent workers each get
        a fresh scope; the shared registry.dispatch + audit are serialized by
        ``_dispatch_lock`` inside ``_dispatch``/``_call_mcp``."""
        exact: dict[str, int] = {}
        streaks: dict[str, int] = {}

        def _worker_call(name: str, args: dict) -> str:
            kwargs = dict(args or {})
            with self._dispatch_lock:
                signature = self._guard_signature(name, kwargs)
                # Worker-local guard scope (mirrors the parent's logic, own dicts).
                streak = streaks.get(name, 0)
                failures = exact.get(signature, 0)
                refusal: dict[str, Any] | None = None
                if streak >= GUARD_STREAK_REFUSE_AT:
                    refusal = {"ok": False, "error": (
                        f"{name} is blocked for this subagent: it failed {streak} times in a "
                        f"row. Take a different approach.")}
                elif failures >= GUARD_EXACT_REFUSE_AT - 1:
                    refusal = {"ok": False, "error": (
                        f"refusing to run {name}: this exact call already failed {failures} "
                        f"times for this subagent. Change the arguments or strategy.")}
                if refusal is not None:
                    self.audit.write("tool_loop_refused", tool=name,
                                     args=self._safe_args(kwargs), result=refusal)
                    return json.dumps({"error": refusal["error"]}, ensure_ascii=False)
                if name.startswith("mcp__"):
                    result = self._call_mcp(name, kwargs)
                else:
                    result = self._dispatch(name, kwargs)
                # Record into the worker-local scope, not the parent's.
                if result.get("ok"):
                    exact.pop(signature, None)
                    streaks.pop(name, None)
                    return result.get("data", "")
                exact[signature] = failures + 1
                streaks[name] = streak + 1
                return json.dumps({"error": result.get("error", "")}, ensure_ascii=False)

        return _worker_call

    def _dispatch(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Run one builtin tool: allowlist gate, registry dispatch, classify the
        JSON-string result, audit. The registry already turns any handler
        exception into a {"error": ...} JSON string, so a tool never aborts the
        turn — this layer adds the audit + the ok/failed split for the guard."""
        if name not in self._effective():
            result = {"ok": False, "error": f"tool denied: {name}"}
            self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        if registry.get_entry(name) is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
            self.audit.write("tool_unknown", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        t0 = time.monotonic()
        payload = registry.dispatch(name, dict(kwargs), self._ctx())
        ok = not _is_error_json(payload)
        result = {"ok": ok, "data": payload} if ok else {"ok": False, "error": _error_text(payload)}
        self.audit.write("tool_call", tool=name, args=self._safe_args(kwargs), result={"ok": ok})
        if ok:
            _log.debug("%s ok in %.2fs", name, time.monotonic() - t0)
        else:
            _log.warning("%s failed: %s", name, result.get("error", ""))
        return result

    def _call_mcp(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        server = name.split("__", 2)[1] if name.count("__") >= 2 else ""
        if self.mcp is None or server not in self.mcp_allowed:
            result = {"ok": False, "error": f"tool denied: {name}"}
            self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        try:
            result = {"ok": True, "data": self.mcp.call(name, kwargs)}
        except McpError as e:
            result = {"ok": False, "error": str(e)}
            _log.warning("%s failed: %s", name, e)
        except Exception as e:  # noqa: BLE001 - a dead server's pipe must not kill the turn
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.audit.write("tool_crash", tool=name, args=self._safe_args(kwargs), result=result)
            _log.exception("tool %s crashed", name)
        self.audit.write("tool_call", tool=name, args=self._safe_args(kwargs), result={"ok": result["ok"]})
        return result

    def _safe_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {k: (v[:300] if isinstance(v, str) else v) for k, v in kwargs.items()}

    def result_cap(self, name: str) -> int | float:
        """The agent-layer backstop cap for a tool's content (the registry
        default, or a per-tool override). Tools self-cap below this."""
        return registry.get_max_result_size(name)

    def as_json(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    # ---- loop guardrails (audit #24) — unchanged from the pre-registry gateway -------

    @staticmethod
    def _guard_signature(name: str, kwargs: dict[str, Any]) -> str:
        canonical = json.dumps(kwargs, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{name}\x00{canonical}".encode("utf-8")).hexdigest()

    def reset_guardrails(self) -> None:
        self._guard_exact_failures.clear()
        self._guard_tool_streaks.clear()

    def _guard_refusal(self, name: str, signature: str) -> "dict[str, Any] | None":
        streak = self._guard_tool_streaks.get(name, 0)
        if streak >= GUARD_STREAK_REFUSE_AT:
            return {"ok": False, "error": (
                f"{name} is blocked: it failed {streak} times in a row. Stop using "
                f"this tool for now and take a genuinely different approach — or tell "
                f"your user about the blocker. (A fresh conversation turn unblocks it.)"
            )}
        failures = self._guard_exact_failures.get(signature, 0)
        if failures >= GUARD_EXACT_REFUSE_AT - 1:
            return {"ok": False, "error": (
                f"refusing to run {name}: this exact call (same arguments) already "
                f"failed {failures} times. Retrying it unchanged will not work — "
                f"change the arguments or the strategy, or explain the blocker."
            )}
        return None

    def _guard_record(self, name: str, signature: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ok"):
            self._guard_exact_failures.pop(signature, None)
            self._guard_tool_streaks.pop(name, None)
            return result
        failures = self._guard_exact_failures.get(signature, 0) + 1
        self._guard_exact_failures[signature] = failures
        self._guard_tool_streaks[name] = self._guard_tool_streaks.get(name, 0) + 1
        if failures >= GUARD_EXACT_WARN_AT:
            result = dict(result)
            result["error"] = str(result.get("error", "")) + (
                f"\n[loop guard: this exact {name} call has now failed {failures} times. "
                f"Inspect the error and change strategy instead of retrying it unchanged.]"
            )
        return result

    # ---- schema emission -------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function specs for the tools the active pack enables."""
        out = registry.get_definitions(self._effective())
        if self.mcp is not None and self.mcp_allowed:
            out.extend(self.mcp.schemas(self.mcp_allowed))
        return out

    def schemas_names(self) -> list[str]:
        names: list[str] = []
        for spec in self.schemas():
            fn = spec.get("function", {})
            if fn.get("name"):
                names.append(fn["name"])
        return names


def _is_error_json(payload: str) -> bool:
    """A handler signalled failure when it returned a JSON object whose top-level
    "error" key carries a non-empty value (hermes' tool_error shape). A success
    result that merely *includes* an "error": null field (e.g. the terminal
    background path) is NOT a failure — gating on key presence alone turned such
    successes into a spurious "ERROR: None" and sent the model chasing ghosts."""
    if not isinstance(payload, str):
        return False
    s = payload.lstrip()
    if not s.startswith("{") or '"error"' not in s:
        return False
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and obj.get("error") not in (None, "")


def _error_text(payload: str) -> str:
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict) and obj.get("error") not in (None, ""):
            return str(obj["error"])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return str(payload)
