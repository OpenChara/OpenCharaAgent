"""CharaHandle — the one name a frontend knows the backend by.

Frontends (TUI, plain terminal, headless run, future web/desktop/messaging)
hold a CharaHandle and NOTHING else: streams come out as protocol events,
commands go in as text, state comes out as an immutable snapshot. In-process
today the handle wraps LunaMothAgent directly; over a wire tomorrow the same
surface becomes an RPC stub and no frontend code changes (hermes's web and
desktop are exactly such clients of one dispatch).

This module MAY import the backend (core/tools/session); the pure-contract
restriction applies to events.py/codec.py only (see tests/test_architecture.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .events import Event

# Re-exported for frontends (the UI shows token estimates without touching core).
from ..core.context import estimate_tokens  # noqa: E402,F401


@dataclass(frozen=True)
class Reply:
    """Result of one /command: human-readable text + optional structured data."""
    ok: bool
    text: str = ""
    data: Any = None


@dataclass(frozen=True)
class CommandInfo:
    name: str   # "goal"
    usage: str  # "/goal [text | done <id> | drop <id>]"
    help: str   # one line


@dataclass(frozen=True)
class AttachInfo:
    """Everything a frontend needs to open a session (greeting decision tree)."""
    char_name: str
    lang: str
    mode: str
    show_thinking: bool
    restored: tuple          # restored transcript tail (message dicts, read-only)
    greeting: str            # card first_mes ("" if none)
    first_meeting: bool
    attach_text: str         # card on_attach prompt ("" if none)


@dataclass(frozen=True)
class StateSnapshot:
    """One coherent read of the chara's state for status lines / telemetry."""
    char_name: str
    lang: str
    mode: str
    provider: str
    model: str
    reasoning: str
    reasoning_supported: bool
    show_thinking: bool
    user_name: str
    isolation: str
    net_on: bool
    user_present: bool
    context_tokens: int
    context_max: int
    memory_chars: int
    memory_max: int
    memory_text: str
    memory_path: str
    sandbox_root: str
    workspace_root: str
    goals: tuple
    skills: tuple
    mcp_servers: tuple       # (name, command, allowed)


def test_connection(settings) -> tuple[bool, str]:
    """Validate endpoint+key+model for a Settings draft (welcome screen/wizard)."""
    from ..core.llm import LLMClient

    return LLMClient(settings.to_llm_config()).test_connection()


class CharaHandle:
    """In-process implementation: wraps one LunaMothAgent + its session."""

    # Status lines poll snapshot() many times a second; state is files on disk.
    _SNAPSHOT_TTL = 0.5

    def __init__(self, settings=None, agent=None):
        from ..core.agent import LunaMothAgent

        self._agent = agent or LunaMothAgent(settings)
        self._session = None
        self._snap: "tuple[float, StateSnapshot] | None" = None

    # ---- lifecycle -------------------------------------------------------------

    def attach(self, present: bool = True) -> AttachInfo:
        """Open (or adopt) the conversation. `present` = a human is watching;
        a daemon attaches with present=False and adopts any queued handoff."""
        a = self._agent
        self._session = a.make_session()
        restored = tuple(dict(m) for m in self._session.context.messages)
        a.state.set_present(present)
        if present:
            a.presence.pop_event()  # discard any stale handoff — we're here now
        else:
            # Adopt the chara: if a detaching frontend queued a departure line,
            # continue *knowing* the operator left. Usually already restored
            # from the transcript — don't duplicate.
            handoff = a.presence.pop_event()
            recent = self._session.context.messages[-3:]
            if handoff and not any(
                m.get("role") == "system" and m.get("content") == handoff for m in recent
            ):
                self._session.context.add("system", handoff)
        first = a.presence.first_meeting() and not restored
        info = AttachInfo(
            char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
            show_thinking=bool(a.settings.show_thinking),
            restored=restored, greeting=a.greeting() or "",
            first_meeting=first, attach_text=a.attach_event_text() if present else "",
        )
        a.presence.mark_met()
        return info

    def record_greeting(self, text: str) -> None:
        """Commit a displayed card greeting (first_mes) to the conversation."""
        self._session.context.add("assistant", text)

    def detach(self) -> None:
        """Presence bookkeeping on the way out (idempotence is the caller's job)."""
        if self._session is not None:
            self._agent.note_detach(self._session)
        self._agent.state.set_present(False)

    def set_present(self, present: bool) -> None:
        self._agent.state.set_present(present)

    # ---- conversation (generators of protocol events) ---------------------------

    def stream_user(self, text: str) -> "Iterator[Event]":
        return self._agent.stream_handle(text, self._session)

    def stream_event(self, text: str) -> "Iterator[Event]":
        return self._agent.stream_event(text, self._session)

    def stream_idle(self) -> "Iterator[Event]":
        return self._agent.stream_think(self._session)

    # ---- commands ----------------------------------------------------------------

    def command(self, line: str) -> Reply:
        from ..core import commands

        self._snap = None  # a command may change anything the snapshot reports
        return commands.execute(self._agent, self._session, line)

    def commands(self) -> "tuple[CommandInfo, ...]":
        from ..core import commands

        return commands.infos()

    # ---- state -------------------------------------------------------------------

    def snapshot(self, fresh: bool = False) -> StateSnapshot:
        if not fresh and self._snap and time.monotonic() - self._snap[0] < self._SNAPSHOT_TTL:
            return self._snap[1]
        a = self._agent
        status = a.state.load()
        mem = a.memory
        servers = tuple(
            (name, str(a.mcp.servers[name].get("command", "?")), name in a.tools.mcp_allowed)
            for name in sorted(a.mcp.servers)
        )
        snap = StateSnapshot(
            char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
            provider=a.settings.provider, model=a.settings.model,
            reasoning=a.settings.reasoning or "medium",
            reasoning_supported=a.llm.reasoning_supported(),
            show_thinking=bool(a.settings.show_thinking),
            user_name=a.settings.user_name,
            isolation=str(status.get("isolation", a.settings.py_backend)),
            net_on=bool(status.get("network_access")),
            user_present=bool(status.get("user_present")),
            context_tokens=self._session.context.token_count() if self._session else 0,
            context_max=self._session.context.max_tokens if self._session else a.context_limit(),
            memory_chars=mem.chars("memory") + mem.chars("user"),
            memory_max=(mem.limits.memory_chars + mem.limits.user_chars) or 1,
            memory_text=mem.render(),
            memory_path=str(mem.root),
            sandbox_root=str(a.sandbox.root),
            workspace_root=str(a.sandbox.root / "workspace"),
            goals=tuple(a.goals.all()),
            skills=tuple(a.skills.scan()),
            mcp_servers=servers,
        )
        self._snap = (time.monotonic(), snap)
        return snap

    def reconfigure(self, settings) -> None:
        self._agent.reconfigure(settings)
        if self._session is not None:
            self._session.context.max_tokens = self._agent.context_limit()

    @property
    def settings(self):
        return self._agent.settings

    # ---- permission + operator plumbing -------------------------------------------

    def set_permission_hook(self, hook: "Callable[[str, str, str, int], bool] | None") -> None:
        self._agent.tools.permission_hook = hook

    def operator_command(self, command: str, timeout: int = 120) -> str:
        """The OPERATOR's shell in the chara's sandbox — same runner, same
        isolation, same audit trail (`! <cmd>` in the TUI)."""
        from ..tools.runner import run_terminal

        a = self._agent
        a.audit.write("operator_command", command=command[:500])
        status = a.state.load()
        return run_terminal(
            command,
            a.sandbox.root / "workspace",
            allow_network=bool(status.get("network_access", False)),
            writable_paths=status.get("writable_paths", []),
            timeout=timeout,
        )

    def audit_tail(self, n: int = 20) -> list:
        return self._agent.audit.tail(n)
