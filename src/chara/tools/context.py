"""ToolContext — the runtime touchpoints a builtin tool handler reaches.

hermes handlers reach infra through module-level singletons keyed by a per-task
``task_id`` (multi-environment). OpenCharaAgent is one-process-one-chara, so there is
exactly ONE context per agent; it is built once by the ToolGateway and injected
into every ``registry.dispatch(name, args, ctx)`` call. A handler is a pure
module-level function ``def handler(args: dict, ctx: ToolContext) -> str``.

Touchpoint map (hermes → OpenCharaAgent), per .codex-fleet/seam-chara.md:
  session cwd / TERMINAL_CWD      → ctx.workspace  (the chara's sandbox workspace)
  VM / BaseEnvironment backend    → ctx.run_terminal(...) over sandbox/admin
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
    polaris: Any = None                # tools.polaris.PolarisStore (read-only north-star; user-owned)
    task: Any = None                   # tools.task.TaskStore (chara-owned life-threads; persisted)
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
    # Factory: a per-worker dispatch with its OWN loop-guardrail scope, sharing
    # the gateway's gate + audit (lock-serialized). delegate_task hands one to
    # each concurrent subagent so workers can't corrupt the parent's guardrails.
    spawn_worker_dispatch: Optional[Callable[[], "Callable[[str, dict], str]"]] = None
    # Delegation depth (0 = the chara itself; 1 = a delegate_task worker). The
    # cap (MAX_DEPTH=1) is enforced by delegate_task: a worker's context carries
    # depth=1, and a worker may not itself spawn workers (grandchild rejected).
    delegate_depth: int = 0

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

    def permissions(self):
        """Typed snapshot of (isolation, network, writable_paths) — the ONE source
        every tool runner reads, so foreground/background/PTY can't disagree."""
        return self.state.permissions()

    def network_on(self) -> bool:
        return self.permissions().network_on

    def writable_paths(self) -> list[str]:
        return list(self.permissions().writable_paths)

    def isolation(self) -> str:
        return self.permissions().isolation

    def run_terminal(self, command: str, *, timeout: int, workdir: Path | None = None,
                     browser: bool = False) -> str:
        """Run a shell command under the chara's isolation (sandbox/admin).
        Thin pass-through to tools.runner.run_terminal with the live env facts.
        ``browser=True`` selects the browser-specific jail (Chromium-capable)."""
        return self.run_terminal_result(command, timeout=timeout, workdir=workdir,
                                        browser=browser).text

    def run_terminal_result(self, command: str, *, timeout: int, workdir: Path | None = None,
                            browser: bool = False):
        """Like run_terminal but returns the structured TerminalResult (text +
        real exit code + timed_out/refused) — so execute_code can judge success on
        the script's actual exit code, not a substring scan of the output."""
        from .runner import run_terminal_result as _run
        perms = self.permissions()
        return _run(
            command,
            workdir or self.workspace,
            isolation=perms.isolation or None,
            allow_network=perms.network_on,
            writable_paths=perms.writable_paths,
            timeout=timeout,
            browser=browser,
        )
