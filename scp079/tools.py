from __future__ import annotations

import json
from typing import Any

from .audit import AuditLog
from .python_sandbox import run_limited_python
from .sandbox import Sandbox, SandboxViolation
from .state import ContainmentState


class ToolGateway:
    def __init__(self, sandbox: Sandbox, state: ContainmentState, audit: AuditLog):
        self.sandbox = sandbox
        self.state = state
        self.audit = audit

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        allowed = set(self.state.load().get("tool_access", []))
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
        try:
            result = {"ok": True, "data": method(**kwargs)}
        except (SandboxViolation, FileNotFoundError, ValueError, PermissionError) as e:
            result = {"ok": False, "error": str(e)}
        self.audit.write("tool_call", tool=name, args=self._safe_args(kwargs), result=result)
        return result

    def _safe_args(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {k: (v[:300] if isinstance(v, str) else v) for k, v in kwargs.items()}

    def tool_inspect_cell(self) -> dict[str, Any]:
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
        return "written"

    def tool_write_log(self, text: str) -> str:
        self.audit.write("079_log", text=text[:1000])
        return "logged"

    def tool_run_python(self, code: str) -> str:
        return run_limited_python(code, self.sandbox.root / "workspace")

    def as_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)
