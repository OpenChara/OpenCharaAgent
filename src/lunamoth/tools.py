from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .audit import AuditLog
from .goals import GoalStore
from .mcp import McpError, McpManager
from .memory import MemoryLimits, MemoryStore
from .skills import SkillStore
from .runner import run_terminal
from .sandbox import Sandbox, SandboxViolation
from .state import EnvState

# Caps for operator-grantable resources/waits.
_MAX_PERMISSION_WAIT = 300
_MIN_PERMISSION_WAIT = 5
_MAX_MEMORY_CHARS = 64_000
PERMISSION_KINDS = ("network", "writable_path", "memory", "other")


class ToolGateway:
    def __init__(
        self, sandbox: Sandbox, state: EnvState, audit: AuditLog,
        memory: MemoryStore | None = None, goals: "GoalStore | None" = None,
        skills: "SkillStore | None" = None, mcp: "McpManager | None" = None,
    ):
        self.sandbox = sandbox
        self.state = state
        self.audit = audit
        self.memory = memory
        self.goals = goals
        self.skills = skills
        self.mcp = mcp
        # Tools the active tool pack enables. None => no pack selected => no tools.
        self.enabled_tools: set[str] | None = None
        # MCP servers the active pack opts into (resolved names).
        self.mcp_allowed: list[str] = []
        # Set by an interactive frontend: (kind, reason, detail, wait_seconds) -> granted?
        # Blocks the calling (worker) thread up to wait_seconds. None => nobody to ask.
        self.permission_hook: "Callable[[str, str, str, int], bool] | None" = None

    def set_enabled(self, tools: "list[str] | set[str] | None", mcp_servers: "list[str] | None" = None) -> None:
        self.enabled_tools = set(tools) if tools is not None else None
        self.mcp_allowed = self.mcp.allowed_servers(mcp_servers) if self.mcp else []

    def _effective(self) -> set[str]:
        """Tools actually callable = implemented ∩ env allowlist ∩ active pack."""
        if self.enabled_tools is None:
            return set()
        implemented = set(self._all_schemas())
        allowlist = set(self.state.load().get("tool_access", []))
        return implemented & allowlist & self.enabled_tools

    def has_tools(self) -> bool:
        return bool(self._effective()) or bool(self.mcp_allowed)

    def call(self, name: str, /, **kwargs: Any) -> dict[str, Any]:
        # `name` is positional-only: tool ARGUMENTS may legitimately be called
        # "name" too (read_skill/create_skill), and must not collide with it.
        if name.startswith("mcp__"):
            return self._call_mcp(name, kwargs)
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

    def _call_mcp(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """External MCP tool — same gating shape (pack opt-in) and audit trail."""
        server = name.split("__", 2)[1] if name.count("__") >= 2 else ""
        if self.mcp is None or server not in self.mcp_allowed:
            result = {"ok": False, "error": f"tool denied: {name}"}
            self.audit.write("tool_denied", tool=name, args=self._safe_args(kwargs), result=result)
            return result
        try:
            result = {"ok": True, "data": self.mcp.call(name, kwargs)}
        except McpError as e:
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

    def tool_memory(self, action: str, target: str = "memory", content: str = "", old_text: str = "") -> str:
        """add / replace / remove an entry in the 'memory' or 'user' store."""
        if self.memory is None:
            raise ValueError("memory not available")
        action = (action or "").strip().lower()
        target = (target or "memory").strip().lower()
        if target not in ("memory", "user"):
            raise ValueError("target must be 'memory' or 'user'")
        if action == "add":
            self.memory.add(target, content)
        elif action == "replace":
            self.memory.replace(target, old_text, content)
        elif action == "remove":
            self.memory.remove(target, old_text)
        else:
            raise ValueError("action must be 'add', 'replace', or 'remove'")
        return json.dumps(
            {"ok": True, "target": target, "entries": self.memory.entries(target), "usage": self.memory.usage(target)},
            ensure_ascii=False,
        )

    def tool_add_goal(self, text: str) -> str:
        if self.goals is None:
            raise ValueError("goals not available")
        goal = self.goals.add(text, by="chara")
        return f"goal {goal['id']} added: {goal['text']}"

    def tool_set_goal_status(self, goal_id: str, status: str) -> str:
        if self.goals is None:
            raise ValueError("goals not available")
        goal = self.goals.set_status(goal_id, status)
        return f"goal {goal['id']} -> {goal['status']}: {goal['text']}"

    def tool_read_skill(self, name: str) -> str:
        if self.skills is None:
            raise ValueError("skills not available")
        return self.skills.read(name)

    def tool_create_skill(self, name: str, description: str, content: str) -> str:
        if self.skills is None:
            raise ValueError("skills not available")
        path = self.skills.create(name, description, content)
        return f"skill {name!r} saved to {path} — it is now in your skill index"

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
            new_chars = min(_MAX_MEMORY_CHARS, limits.memory_chars * 2)
            # Growing only — set_limits keeps it consistent (no discard on grow).
            self.memory.set_limits(MemoryLimits(memory_chars=new_chars, user_chars=limits.user_chars))
            return f"granted: memory budget raised to {new_chars} chars."
        return "granted. The operator will act on it."

    # ---- native function-calling schemas ------------------------------------------

    def _all_schemas(self) -> dict[str, dict[str, Any]]:
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
            "memory": {
                "description": (
                    "Maintain your durable memory across sessions. Two stores: 'memory' (notes to "
                    "yourself — ongoing work, what you've made, decisions, taste) and 'user' (durable "
                    "facts about the operator). action: 'add' appends an entry; 'replace' swaps the "
                    "entry containing old_text for content (empty content deletes it); 'remove' deletes "
                    "the entry containing old_text. Keep entries short and durable; this is curated, not a log. "
                    "New entries take effect in your prompt next session (this session's response confirms the save)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "target": {"type": "string", "enum": ["memory", "user"], "description": "Which store (default 'memory')."},
                        "content": {"type": "string", "description": "Entry text, for add/replace."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry to replace/remove."},
                    },
                    "required": ["action"],
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
            "add_goal": {
                "description": (
                    "Add a goal of your own to your goal list. Goals persist across "
                    "sessions and appear in your context; unattended time is a good "
                    "time to pursue them."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "The goal, in one line."}},
                    "required": ["text"],
                },
            },
            "set_goal_status": {
                "description": (
                    "Update one of your goals: mark it done (ONLY when truly finished — "
                    "your work must be real) or dropped (no longer worth pursuing)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string", "description": "The goal id, e.g. g3."},
                        "status": {"type": "string", "enum": ["active", "done", "dropped"]},
                    },
                    "required": ["goal_id", "status"],
                },
            },
            "read_skill": {
                "description": (
                    "Fetch the full text of a skill from your skill index (the index in "
                    "your context shows names + one-line descriptions only)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "The skill name from the index."}},
                    "required": ["name"],
                },
            },
            "create_skill": {
                "description": (
                    "Write (or revise) one of YOUR OWN skills: durable know-how saved as a "
                    "SKILL.md you can read back in any future session. Distill things you "
                    "had to figure out the hard way. Overwriting the same name revises it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "kebab-case, e.g. tune-the-synth"},
                        "description": {"type": "string", "description": "One line for the index."},
                        "content": {"type": "string", "description": "The full know-how (markdown)."},
                    },
                    "required": ["name", "description", "content"],
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
        if self.mcp is not None and self.mcp_allowed:
            out.extend(self.mcp.schemas(self.mcp_allowed))
        return out

    def as_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)
