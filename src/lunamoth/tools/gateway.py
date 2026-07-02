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
from .polaris import PolarisStore
from .mcp import McpError, McpManager
from .memory import MemoryStore
from .skills import SkillStore
from .sandbox import Sandbox
from .context import ToolContext
from .registry import TOOL_ERROR_KEY, registry, discover_builtin_tools
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
        memory: MemoryStore | None = None, polaris: "PolarisStore | None" = None,
        skills: "SkillStore | None" = None, mcp: "McpManager | None" = None,
        llm: Any = None, transcript: Any = None, task: Any = None,
    ):
        _ensure_discovered()
        self.sandbox = sandbox
        self.state = state
        self.audit = audit
        self.memory = memory
        self.polaris = polaris
        self.task = task
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
        with self._dispatch_lock:
            return self._ctx_locked()

    def _ctx_locked(self) -> ToolContext:
        if self._ctx_obj is None:
            self._ctx_obj = ToolContext(
                sandbox=self.sandbox, state=self.state, audit=self.audit,
                memory=self.memory, polaris=self.polaris, task=self.task, skills=self.skills,
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

    def background_notices(self) -> "list[str]":
        """Drain finished background jobs (image gen, background terminal) off the
        process registry and render them as model-facing lines. The agent injects
        these at a turn boundary so the chara reacts to a job that finished while it
        was idle or working. Empty when nothing is pending / no registry yet."""
        ctx = self._ctx_obj
        reg = getattr(ctx, "processes", None) if ctx is not None else None
        if reg is None:
            return []
        try:
            from .builtin._process_registry import format_background_notification
            events = reg.drain_notifications()
            return [s for s in (format_background_notification(e) for e in events) if s]
        except Exception:  # noqa: BLE001 — notifications are best-effort, never fatal
            return []

    def has_pending_notifications(self) -> bool:
        """Cheap, non-destructive: is there a finished-background-job notice waiting
        to be drained? Read by the snapshot so the supervisor can drive a
        completion-wake turn. Does NOT consume the queue."""
        ctx = self._ctx_obj
        reg = getattr(ctx, "processes", None) if ctx is not None else None
        try:
            # Mirror drain_notifications' skip logic (an already-consumed completion
            # is not pending), so the wake never fires a no-op turn.
            return bool(reg is not None and reg.has_pending_notifications())
        except Exception:  # noqa: BLE001
            return False

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
        """Callable builtin tools.

        The DEFAULT is ALL registered tools (hermes parity — the model gets the
        whole surface; there is no user-facing tool picker). A pack of ``["*"]``
        means exactly that and is what the bundled default pack declares; the same
        ``*`` wildcard the MCP allow-list already uses. A pack with an explicit
        list narrows to ``registered ∩ list`` (a card author can still restrict).
        ``None`` means a TOOL-LESS chara (a plain-roleplay card with no pack) —
        it gets nothing and is free to just narrate. The registry is the source
        of truth for what exists, so a newly-registered tool is callable with
        nothing to edit. Runtime toggles like ``/net off`` gate at CALL time."""
        if self.enabled_tools is None:
            return set()
        all_names = set(registry.get_all_tool_names())
        if "*" in self.enabled_tools:
            return all_names
        return all_names & self.enabled_tools

    def has_tools(self) -> bool:
        return bool(self._effective()) or bool(self.mcp_allowed)

    # ---- dispatch --------------------------------------------------------------------

    def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        """Run one tool: loop-guard refusal → dispatch → audit → guard record.
        Returns {"ok": bool, "data": <json str>} or {"ok": False, "error": str}.

        ``_dispatch_lock`` guards only the shared MUTABLE state (guardrail
        counters + the audit trail), never the tool body: an execute_code child
        script's tool RPC arrives on a DIFFERENT thread and dispatches back
        through this gateway, so holding the lock across the whole run would
        deadlock it against its own turn (RLock re-entrancy is same-thread
        only) — and would serialize delegate_task's "parallel" workers."""
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
        with self._dispatch_lock:
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
        a fresh scope; guard counters + audit writes are serialized by
        ``_dispatch_lock``, while tool bodies run unlocked and genuinely in
        parallel (the registry has its own lock for its tables)."""
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
            # The tool body runs UNLOCKED (see call()): workers really do run in
            # parallel, and a worker's own child-script RPC can dispatch back in.
            if name.startswith("mcp__"):
                result = self._call_mcp(name, kwargs)
            else:
                result = self._dispatch(name, kwargs)
            with self._dispatch_lock:
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
            with self._dispatch_lock:
                self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        if registry.get_entry(name) is None:
            result = {"ok": False, "error": f"unknown tool: {name}"}
            with self._dispatch_lock:
                self.audit.write("tool_unknown", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        t0 = time.monotonic()
        payload = registry.dispatch(name, dict(kwargs), self._ctx())
        ok = not _is_error_json(payload)
        result = {"ok": ok, "data": payload} if ok else {"ok": False, "error": _error_text(payload)}
        with self._dispatch_lock:
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
            with self._dispatch_lock:
                self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        try:
            result = {"ok": True, "data": self.mcp.call(name, kwargs)}
        except McpError as e:
            result = {"ok": False, "error": str(e)}
            _log.warning("%s failed: %s", name, e)
        except Exception as e:  # noqa: BLE001 - a dead server's pipe must not kill the turn
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            with self._dispatch_lock:
                self.audit.write("tool_crash", tool=name, args=self._safe_args(kwargs), result=result)
            _log.exception("tool %s crashed", name)
        with self._dispatch_lock:
            self.audit.write("tool_call", tool=name, args=self._safe_args(kwargs), result={"ok": result["ok"]})
        return result

    def _safe_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # Truncate AND redact: the audit trail is a security record that can be
        # exported, so a secret passed as a tool arg (a key in a `terminal`
        # export=…, a token in a url) must not land in it in clear. Central redactor.
        from ..core.redact import redact_sensitive_text
        return {
            k: (redact_sensitive_text(v[:300], force=True) if isinstance(v, str) else v)
            for k, v in kwargs.items()
        }

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
    """Did the handler signal failure? Authoritative path: the explicit
    ``__tool_error__`` sentinel that tool_error stamps (TOOL_ERROR_KEY). Fallback
    (for raw error dicts built outside tool_error — MCP, dispatch internals):
    the legacy heuristic of a non-empty top-level "error". A result that merely
    carries "error": null (e.g. a background-launch success) is NOT a failure —
    gating on key presence alone once turned such successes into a spurious
    "ERROR: None" and sent the model chasing ghosts."""
    if not isinstance(payload, str):
        return False
    s = payload.lstrip()
    if not s.startswith("{"):
        return False
    if TOOL_ERROR_KEY not in s and '"error"' not in s:
        return False
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    if obj.get(TOOL_ERROR_KEY):  # explicit, authoritative
        return True
    return obj.get("error") not in (None, "")  # legacy shape fallback


def _error_text(payload: str) -> str:
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict) and obj.get("error") not in (None, ""):
            return str(obj["error"])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return str(payload)
