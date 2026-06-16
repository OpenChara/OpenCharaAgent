"""ToolContext — the runtime touchpoints a builtin tool handler reaches.

hermes handlers reach infra through module-level singletons keyed by a per-task
``task_id`` (multi-environment). LunaMoth is one-process-one-chara, so there is
exactly ONE context per agent; it is built once by the ToolGateway and injected
into every ``registry.dispatch(name, args, ctx)`` call. A handler is a pure
module-level function ``def handler(args: dict, ctx: ToolContext) -> str``.

Touchpoint map (hermes → LunaMoth), per .codex-fleet/seam-lunamoth.md:
  session cwd / TERMINAL_CWD      → ctx.workspace  (the chara's sandbox workspace)
  VM / BaseEnvironment backend    → ctx.run_terminal(...) over dir/sandbox/docker
  lazy-imported provider llm      → ctx.llm  (core/llm.py LLMClient)
  ~/.hermes memories/skills home  → ctx.memory / ctx.skills (per-chara sandbox)
  session transcript              → ctx.transcript (SQLite, for session_search)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class ToolContext:
    """References + ephemeral per-session stores shared by all tool handlers."""

    sandbox: Any                       # tools.sandbox.Sandbox (path confinement + workspace)
    state: Any                         # core.state.EnvState (network/writable/isolation/rest)
    audit: Any                         # obs.audit.AuditLog
    memory: Any = None                 # tools.memory.MemoryStore
    wishes: Any = None                 # tools.goals store (the renamed chara-life "goal")
    skills: Any = None                 # tools.skills.SkillStore
    mcp: Any = None                    # tools.mcp.McpManager
    llm: Any = None                    # core.llm.LLMClient (web summarize / execute / delegate)
    transcript: Any = None             # core.transcript (session_search)
    permission_hook: Optional[Callable[[str, str, str, int], bool]] = None
    # An async/blocking callback an interactive frontend supplies for `clarify`
    # (presence-gated, like request_permission). Signature mirrors the protocol.
    clarify_hook: Optional[Callable[[str, list], str]] = None
    # The dispatcher, so execute_code can expose tools to the sandboxed Python.
    dispatch: Optional[Callable[[str, dict], str]] = None
    # The gateway's effective tool set (registry ∩ pack) — the ONE source of
    # which tools are callable; execute_code mirrors it to its sandbox.
    enabled_tool_names: Optional[Callable[[], "set[str]"]] = None

    # ---- ephemeral per-session stores (lazily created, persist across calls) ----
    todo: list = field(default_factory=list)          # todo tool's task list
    processes: Any = None                             # process registry (background jobs)
    browser: Any = None                               # browser session manager
    _scratch: dict = field(default_factory=dict)       # misc per-session tool state

    # ---- convenience the file tools rely on ----
    @property
    def workspace(self) -> Path:
        """The chara's one working directory — every file tool is rooted here."""
        return self.sandbox.root / "workspace"

    @property
    def assets(self) -> Path:
        """The read-only reference shelf — a SIBLING of workspace (never under
        it). The chara reads/uses it (card art + operator-dropped reference
        material) but never writes it; the file tools enforce that."""
        return getattr(self.sandbox, "assets_dir", None) or (self.sandbox.root / "assets")

    def network_on(self) -> bool:
        return bool(self.state.load().get("network_access", False))

    def writable_paths(self) -> list[str]:
        return list(self.state.load().get("writable_paths", []) or [])

    def isolation(self) -> str:
        return str(self.state.load().get("isolation", "sandbox"))

    def run_terminal(self, command: str, *, timeout: int, workdir: Path | None = None) -> str:
        """Run a shell command under the chara's isolation (dir/sandbox/docker).
        Thin pass-through to tools.runner.run_terminal with the live env facts."""
        from .runner import run_terminal as _run
        status = self.state.load()
        return _run(
            command,
            workdir or self.workspace,
            allow_network=bool(status.get("network_access", False)),
            writable_paths=status.get("writable_paths", []),
            timeout=timeout,
        )
