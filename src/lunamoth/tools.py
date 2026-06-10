from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog
from .memory import MemoryLimits, MemoryStore
from .runner import run_terminal
from .sandbox import Sandbox, SandboxViolation
from .state import EnvState

# Caps for operator-grantable resources/waits.
_MAX_PERMISSION_WAIT = 300
_MIN_PERMISSION_WAIT = 5
_MAX_MEMORY_CHARS = 64_000
PERMISSION_KINDS = ("network", "writable_path", "memory", "other")


class ToolGateway:
    def __init__(self, sandbox: Sandbox, state: EnvState, audit: AuditLog, memory: MemoryStore | None = None):
        self.sandbox = sandbox
        self.state = state
        self.audit = audit
        self.memory = memory
        # Tools the active tool pack enables. None => no pack selected => no tools.
        self.enabled_tools: set[str] | None = None
        # Set by an interactive frontend: (kind, reason, detail, wait_seconds) -> granted?
        # Blocks the calling (worker) thread up to wait_seconds. None => nobody to ask.
        self.permission_hook: "Callable[[str, str, str, int], bool] | None" = None

    def set_enabled(self, tools: "list[str] | set[str] | None") -> None:
        self.enabled_tools = set(tools) if tools is not None else None

    def _effective(self) -> set[str]:
        """Tools actually callable = implemented ∩ env allowlist ∩ active pack."""
        if self.enabled_tools is None:
            return set()
        implemented = set(self._all_schemas())
        allowlist = set(self.state.load().get("tool_access", []))
        return implemented & allowlist & self.enabled_tools

    def has_tools(self) -> bool:
        return bool(self._effective())

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        allowed = self._effective()
        if name not in allowed:
            result = {"ok": False, "error": f"tool denied: {name}"}
            self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        try:
            method = getattr(self, f"tool_{name}")
        except AttributeError:
            result = {"ok": False, "error": f"unknown tool: {name}"}
            self.audit.write("tool_unknown", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        # Validate required args up front so a model that emits an empty or truncated
        # arguments object gets a useful hint instead of a raw Python TypeError.
        schema = self._all_schemas().get(name, {}).get("parameters", {})
        missing = [r for r in schema.get("required", []) if r not in kwargs or kwargs[r] in (None, "")]
        if missing:
            props = ", ".join(schema.get("properties", {}).keys()) or "(none)"
            result = {"ok": False, "error": f"{name} is missing required argument(s): {', '.join(missing)}. Required parameters: {props}."}
            self.audit.write("tool_badargs", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        try:
            result = {"ok": True, "data": method(**kwargs)}
        except TypeError as e:
            props = ", ".join(schema.get("properties", {}).keys()) or "(none)"
            result = {"ok": False, "error": f"{name} called with wrong arguments ({e}). Parameters are: {props}."}
        except (SandboxViolation, FileNotFoundError, ValueError, PermissionError) as e:
            result = {"ok": False, "error": str(e)}
        self.audit.write("tool_call", tool=name, args=self._safe_args(kwargs), result=result)
        return result

    def _safe_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {k: (v[:300] if isinstance(v, str) else v) for k, v in kwargs.items()}

    # ---- tool implementations -----------------------------------------------------

    def tool_inspect_env(self) -> dict[str, Any]:
        return self.state.load()

    def tool_list_files(self) -> list[str]:
        return self.sandbox.list_files()

    def tool_read_file(self, filename: str) -> str:
        return self.sandbox.read_file(filename)

    def tool_list_workspace(self) -> list[str]:
        return self.sandbox.list_workspace()

    def tool_read_workspace_file(self, filename: str) -> str:
        return self.sandbox.read_workspace_file(filename)

    def tool_write_file(self, filename: str, text: str) -> str:
        self.sandbox.write_file(filename, text)
        return f"wrote {filename}"

    def tool_write_log(self, text: str) -> str:
        self.audit.write("note", text=text[:1000])
        return "logged"

    def tool_terminal(self, command: str, timeout: int | None = None, workdir: str | None = None) -> str:
        status = self.state.load()
        return run_terminal(
            command,
            self.sandbox.root / "workspace",
            allow_network=bool(status.get("network_access", False)),
            writable_paths=status.get("writable_paths", []),
            timeout=int(timeout) if timeout else 30,
            workdir=workdir,
        )

    def tool_read_memory(self) -> str:
        if self.memory is None:
            raise ValueError("memory not available")
        return self.memory.render()

    def tool_write_memory(self, content: str) -> str:
        if self.memory is None:
            raise ValueError("memory not available")
        written = self.memory.replace(content)
        return f"memory saved ({len(written)} chars)"

    def tool_request_permission(self, kind: str, reason: str, detail: str = "", wait_seconds: int = 60) -> str:
        """Ask the operator for a capability or more resources.

        Presence-gated: while the operator is attached the request is shown in
        their console and waits up to wait_seconds (timeout = deny); while the
        operator is away it is denied immediately and only logged.
        """
        kind = (kind or "").strip().lower()
        if kind not in PERMISSION_KINDS:
            raise ValueError(f"kind must be one of {PERMISSION_KINDS}")
        if not self.state.load().get("user_present", False):
            return (
                "denied: the operator is away. Requests are only considered while the "
                "operator is attached; this one was logged for them to see later."
            )
        if self.permission_hook is None:
            return "denied: no operator console is available to approve requests."
        wait = max(_MIN_PERMISSION_WAIT, min(_MAX_PERMISSION_WAIT, int(wait_seconds or 60)))
        granted = bool(self.permission_hook(kind, str(reason or ""), str(detail or ""), wait))
        if not granted:
            return "denied: the operator declined (or did not answer in time)."
        if kind == "network":
            self.state.set_network(True)
            return "granted: network access is now ON."
        if kind == "writable_path":
            p = str(Path(detail).expanduser().resolve()) if detail.strip() else ""
            if not p:
                return "granted in principle, but no path was given — request again with the path in `detail`."
            self.state.add_writable_path(p)
            return f"granted: {p} is now writable."
        if kind == "memory":
            if self.memory is None:
                return "granted, but no memory store is attached."
            limits = self.memory.limits
            new_chars = min(_MAX_MEMORY_CHARS, limits.max_chars * 2)
            self.memory.limits = MemoryLimits(max_tokens=limits.max_tokens * 2, max_chars=new_chars)
            return f"granted: memory budget raised to {new_chars} chars."
        return "granted. The operator will act on it."

    # ---- native function-calling schemas ------------------------------------------

    def _memory_budget(self) -> int:
        return self.memory.limits.max_chars if self.memory else 0

    def _all_schemas(self) -> dict[str, dict[str, Any]]:
        budget = self._memory_budget()
        return {
            "terminal": {
                "description": (
                    "Run a shell command in your workspace and get stdout/stderr back. "
                    "Language-agnostic: use it to run python3/node, write and read files, use git, etc. "
                    "Writes are confined to the workspace; network is off unless the operator enabled it. "
                    "Keep commands bounded (they time out); no interactive prompts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The shell command to execute."},
                        "timeout": {"type": "integer", "description": "Max seconds to wait (default 30)."},
                        "workdir": {"type": "string", "description": "Working directory (relative to the workspace)."},
                    },
                    "required": ["command"],
                },
            },
            "read_memory": {
                "description": "Read your durable memory document (persists across the conversation).",
                "parameters": {"type": "object", "properties": {}},
            },
            "write_memory": {
                "description": (
                    "Replace your durable memory document with new full text. "
                    f"Budget: about {budget} characters — writes beyond that are truncated, so summarize and keep what matters."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string", "description": "The complete new memory text."}},
                    "required": ["content"],
                },
            },
            "list_files": {
                "description": "List the read-only files provided to you.",
                "parameters": {"type": "object", "properties": {}},
            },
            "read_file": {
                "description": "Read one of the read-only files provided to you.",
                "parameters": {
                    "type": "object",
                    "properties": {"filename": {"type": "string"}},
                    "required": ["filename"],
                },
            },
            "list_workspace": {
                "description": "List files in your read/write workspace.",
                "parameters": {"type": "object", "properties": {}},
            },
            "read_workspace_file": {
                "description": "Read a file from your read/write workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"filename": {"type": "string"}},
                    "required": ["filename"],
                },
            },
            "write_file": {
                "description": "Write a text file into your read/write workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"filename": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["filename", "text"],
                },
            },
            "inspect_env": {
                "description": "Inspect your runtime environment (isolation level, network on/off, allowed tools).",
                "parameters": {"type": "object", "properties": {}},
            },
            "write_log": {
                "description": "Append a line to your audit log.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            "request_permission": {
                "description": (
                    "Ask the operator to grant a capability or more resources. Only works while "
                    "the operator is present (check inspect_env: user_present); when they are away "
                    "the request is denied automatically, so don't bother asking — note it in memory "
                    "and ask when they return. You choose how long to wait; no answer means no."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(PERMISSION_KINDS),
                            "description": (
                                "network = internet access for the terminal tool; writable_path = write "
                                "access to a directory outside your workspace (put the path in detail); "
                                "memory = a larger durable-memory budget; other = anything else (explain in reason)."
                            ),
                        },
                        "reason": {"type": "string", "description": "Why you need it — shown to the operator."},
                        "detail": {"type": "string", "description": "Kind-specific detail, e.g. the path for writable_path."},
                        "wait_seconds": {
                            "type": "integer",
                            "description": "How long you are willing to wait for an answer (5-300, default 60). Timeout = denied.",
                        },
                    },
                    "required": ["kind", "reason"],
                },
            },
        }

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function specs for the tools the active pack enables."""
        allowed = self._effective()
        specs = self._all_schemas()
        out: list[dict[str, Any]] = []
        for name, spec in specs.items():
            if name not in allowed:
                continue
            out.append({
                "type": "function",
                "function": {"name": name, "description": spec["description"], "parameters": spec["parameters"]},
            })
        return out

    def as_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)
