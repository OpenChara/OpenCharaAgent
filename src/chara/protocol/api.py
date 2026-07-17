"""CharaHandle — the one name a frontend knows the backend by.

Frontends (TUI, plain terminal, headless run, future web/desktop/messaging)
hold a CharaHandle and NOTHING else: streams come out as protocol events,
commands go in as text, state comes out as an immutable snapshot. In-process
today the handle wraps CharaAgent directly; over a wire tomorrow the same
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
from ..content.knobs import DEFAULT_QUIET

# Re-exported for frontends (the UI shows token estimates without touching core).
from ..core.context import estimate_tokens  # noqa: F401

# Operator words that grant a permission request — shared by every frontend.
GRANT_WORDS = frozenset({"y", "yes", "allow", "ok", "同意", "允许", "是"})


@dataclass(frozen=True)
class Reply:
    """Result of one /command: human-readable text + optional structured data.
    `verbose` marks long-form output a frontend should give real estate
    (the TUI's panel) instead of a console one-liner."""
    ok: bool
    text: str = ""
    data: Any = None
    verbose: bool = False


@dataclass(frozen=True)
class CommandInfo:
    name: str   # "aspiration"
    usage: str  # "/aspiration [text | clear]"
    help: str   # one line


@dataclass(frozen=True)
class AttachInfo:
    """Everything a frontend needs to open a session.

    `opening` is the DECIDED first move: 'greeting' = the card's first_mes, shown
    once at the chara's very first opening (the TRANSCRIPT epoch is empty), already
    persisted server-side · 'none' = continue silently (a re-open, or a card with
    no first_mes). The chara has NO attach/detach awareness — opening the chat is
    not an event, only a way to watch and talk.

    'arrival' and 'probe' are RESERVED, forward-compat values for a future
    GM/world-event layer; nothing emits them today, but the 'event' stream path
    (stream_event) stays live for injected world events. Frontends that still
    branch on them keep working — they simply never fire."""
    char_name: str
    lang: str
    mode: str
    show_thinking: bool
    restored: tuple          # restored transcript tail (message dicts, read-only)
    opening: str             # greeting | none today; arrival | probe reserved (see above)
    opening_text: str


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
    rest_until: float        # epoch the chara chose to rest until (0 = not resting)
    quiet: int               # engagement: silence (s) before it resumes its own work
    patience: float          # effective base seconds between spontaneous cycles
    embodiment: str          # literal | actor
    website: bool            # personal_website module active (homepage in the website tab)
    context_tokens: int
    context_max: int
    memory_chars: int
    memory_max: int
    memory_text: str
    memory_path: str
    sandbox_root: str
    workspace_root: str
    # The active chara's frozen-card visuals, so a chat view can show the real
    # avatar/art instead of a placeholder glyph (all '' when the card has none).
    avatar_uri: str = ""
    sprite_url: str = ""
    bg_url: str = ""
    keyvisual_url: str = ""
    # A finished background job (image gen / delegate / background terminal) is
    # waiting to be drained. The supervisor reads this to drive a completion-wake
    # turn (stream_react) regardless of autonomy mode. Cheap, non-destructive.
    pending_notices: bool = False
    # The chara's provider endpoint, so a per-chara model picker can show WHICH
    # saved provider key is active (matched by provider+base_url). '' when unset.
    base_url: str = ""
    # Aspiration/skills/MCP listings are NOT here on purpose: the snapshot feeds a
    # status line polled several times a second, and those need disk walks.
    # Rich UIs get them from /aspiration /skills /mcp Reply.data on demand.


def test_connection(settings) -> tuple[bool, str]:
    """Validate endpoint+key+model for a Settings draft (welcome screen/wizard)."""
    from ..core.llm import LLMClient

    return LLMClient(settings.to_llm_config()).test_connection()


def browser_driver_status() -> tuple[str | None, bool]:
    """The optional browser_* tool driver's state, for `chara setup browser`
    / `doctor`. Returns (agent_browser_cli_path_or_None, chromium_installed).
    Frontends reach the backend only through this layer; cli.py must not import
    tools/ directly (architecture rule)."""
    from ..tools.builtin import _browser_driver as drv

    drv._reset_caches_for_test()  # report the real current state, not a stale cache
    return drv.find_agent_browser(), drv.chromium_installed()


def apply_browser_runtime_fixups() -> bool:
    """Apply post-install fixes the browser driver needs to run under the OS jail
    (currently the crashpad ``--database`` shim). Called by `chara setup
    browser`. Frontends reach this only through protocol/ (architecture rule)."""
    from ..tools.builtin import _browser_driver as drv

    return drv.ensure_crashpad_db_fix()


class CharaHandle:
    """In-process implementation: wraps one CharaAgent + its session."""

    # Status lines poll snapshot() many times a second; state is files on disk.
    _SNAPSHOT_TTL = 0.5

    def __init__(self, settings=None, agent=None):
        from ..core.agent import CharaAgent

        self._agent = agent or CharaAgent(settings)
        self._session = None
        self._snap: "tuple[float, StateSnapshot] | None" = None

    # ---- lifecycle -------------------------------------------------------------

    def attach(self, present: bool = True) -> AttachInfo:
        """Open the conversation and decide the first move.

        The chara is INDEPENDENT of whether a human is watching: `present` is
        kept only for transport/backward-compat and changes nothing about the
        chara — opening the chat is not an event.

        First move (one tree, every frontend): the card's first_mes is shown
        exactly ONCE — at the chara's very first opening, recognized by an EMPTY
        transcript epoch (no prior rows) — and is persisted to the transcript
        FIRST, before anything else, so it can never be lost to a process death
        or a dropped frontend socket. Every later open finds a non-empty epoch
        and opens silently (opening='none'); the prior conversation rides
        `restored`. `/reset` bumps the epoch to empty, so the first_mes naturally
        re-shows on the next open."""
        a = self._agent
        if self._session is None:
            self._session = a.make_session()
        # Capture `restored` BEFORE committing any greeting, so the opener is sent
        # once (as opening_text) and never also folded into the restored tail on
        # this same attach. On every LATER open the greeting rides `restored` from
        # the transcript and opening='none' — no double-show.
        restored = self._display_restored()

        # The transcript is the SINGLE authority for "has this chara ever opened".
        # A fresh epoch (no rows) → show + persist the card greeting once. Persist
        # FIRST: record_greeting writes the transcript row before we return, so a
        # crash/drop after this point still leaves the greeting on disk and on the
        # board. (make_session restored an empty context for an empty epoch, so the
        # row we add is the first message — no double-show on reconnect.)
        greeting = a.greeting() or ""
        empty_epoch = a.transcript.count() == 0
        if greeting and empty_epoch:
            opening, opening_text = "greeting", greeting
            self.record_greeting(opening_text)  # transcript first — survives anything
        else:
            opening, opening_text = "none", ""

        return AttachInfo(
            char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
            show_thinking=bool(a.settings.show_thinking),
            restored=restored, opening=opening, opening_text=opening_text,
        )

    def _display_restored(self) -> tuple:
        """The restored tail SENT TO THE FRONTEND for display.

        Built from the transcript's fuller display view (load_display includes
        legacy kind='tool' forensic rows), so the history panel can show tool
        calls, tool results and reasoning. This is DISPLAY-ONLY: the model's
        replayed context (self._session.context → context.render()) is NOT
        touched — tool results stay forensic for the model on purpose. The DB
        is the single source of truth, so a reconnect always shows the current
        conversation. If the transcript is unavailable, fall back to the live
        in-memory context view (never fabricated)."""
        a = self._agent
        rows = a.transcript.load_display(max_messages=a.RESTORE_MAX_MESSAGES)
        if rows:
            return tuple(rows)
        return tuple(dict(m) for m in self._session.context.messages)

    def record_greeting(self, text: str) -> None:
        """Commit the card greeting (first_mes) to the conversation, exactly once.

        attach() calls this server-side the moment it decides to greet — the
        opener reaches the transcript (and thus the board preview + any reopen)
        before attach() even returns, so it survives a process death or a dropped
        frontend socket. A frontend's later `greet` round-trip (and any
        double-fire) is a harmless no-op: the transcript is the authority, and a
        non-empty epoch already carries the line."""
        if not text:
            return
        if self._session is None:
            self._session = self._agent.make_session()
        # Idempotent: don't re-append a greeting the transcript already holds (the
        # frontend echoes opening_text back via `greet` after attach committed it).
        last = self._session.context.messages[-1] if self._session.context.messages else None
        if last and last.get("role") == "assistant" and (last.get("content") or "") == text:
            return
        self._session.context.add("assistant", text)

    def detach(self) -> None:
        """Transport teardown only.

        The chara is independent of attach/detach: leaving the chat is not an
        event and leaves no trace. Frontends/transports call this on the way out;
        there is nothing for the agent to register."""
        return None

    # ---- conversation (generators of protocol events) ---------------------------

    def stream_user(self, text: str, attachments=None) -> "Iterator[Event]":
        return self._agent.stream_handle(text, self._session, attachments)

    def stream_event(self, text: str) -> "Iterator[Event]":
        return self._agent.stream_event(text, self._session)

    def stream_idle(self) -> "Iterator[Event]":
        return self._agent.stream_think(self._session)

    def stream_react(self) -> "Iterator[Event]":
        return self._agent.stream_react(self._session)

    # ---- commands ----------------------------------------------------------------

    def command(self, line: str) -> Reply:
        from ..core import commands

        self._snap = None  # a command may change anything the snapshot reports
        return commands.execute(self._agent, self._session, line)

    def commands(self) -> "tuple[CommandInfo, ...]":
        from ..core import commands

        return commands.infos()

    def command_is_exclusive(self, line: str) -> bool:
        """Whether `line` mutates state a streaming turn shares (context buffer /
        LLM route) and therefore must not run while a turn is in flight. The
        server checks this before executing a `command` RPC; the classifier
        lives with the registry so the two can't drift."""
        from ..core import commands

        return commands.is_exclusive(line)

    # ---- state -------------------------------------------------------------------

    def snapshot(self, fresh: bool = False) -> StateSnapshot:
        if not fresh and self._snap and time.monotonic() - self._snap[0] < self._SNAPSHOT_TTL:
            return self._snap[1]
        a = self._agent
        status = a.state.load()
        mem = a.memory
        visuals = self._card_visuals()
        snap = StateSnapshot(
            char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
            pending_notices=a.pending_notices(),
            provider=a.settings.provider, base_url=a.settings.base_url,
            model=a.settings.model,
            reasoning=a.settings.reasoning or "medium",
            reasoning_supported=a.llm.reasoning_supported(),
            show_thinking=bool(a.settings.show_thinking),
            user_name=a.settings.user_name,
            isolation=a.state.permissions().isolation,  # the ONE authority (backend())
            net_on=bool(status.get("network_access")),
            rest_until=float(status.get("rest_until", 0.0) or 0.0),
            quiet=int(getattr(a.settings, "quiet", DEFAULT_QUIET)),
            patience=float(a.effective_patience()),
            embodiment=a.effective_embodiment(),
            website=a.website_active(),
            context_tokens=self._session.context.token_count() if self._session else 0,
            context_max=self._session.context.max_tokens if self._session else a.context_limit(),
            memory_chars=mem.chars("memory") + mem.chars("user"),
            memory_max=(mem.limits.memory_chars + mem.limits.user_chars) or 1,
            memory_text=mem.render(),
            memory_path=str(mem.root),
            sandbox_root=str(a.sandbox.root),
            workspace_root=str(a.sandbox.root / "workspace"),
            avatar_uri=visuals["avatar_uri"],
            sprite_url=visuals["sprite_url"],
            bg_url=visuals["bg_url"],
            keyvisual_url=visuals["keyvisual_url"],
        )
        self._snap = (time.monotonic(), snap)
        return snap

    def _card_visuals(self) -> dict:
        """The active chara's frozen-card visuals (avatar + art URLs). Cached per
        handle keyed on the card path — the frozen session card doesn't change
        within a life, and the snapshot polls several times a second."""
        from ..content.cards import card_visuals

        path = (self._agent.character.source_path if self._agent.character else "") or ""
        cached = getattr(self, "_visuals_cache", None)
        if cached is not None and cached[0] == path:
            return cached[1]
        v = card_visuals(path)
        self._visuals_cache = (path, v)
        return v

    def resolve_media(self, rel: str) -> str | None:
        """Resolve a chara-emitted ``MEDIA:`` path (workspace-relative, or absolute
        inside the jail) to an absolute, readable file path — or ``None`` if it
        escapes the sandbox, isn't a file, or exceeds the size cap. This is the ONE
        place the sandbox boundary is enforced for OUTBOUND files: every surface that
        delivers/renders a marker (messaging upload, the web asset route) resolves
        through here, so "what the chara may surface" can't drift between them."""
        from .media import MAX_MEDIA_BYTES

        a = self._agent
        try:
            fp = a.sandbox.resolve_readable(rel)
            if not fp.is_file():
                return None
            if fp.stat().st_size > MAX_MEDIA_BYTES:
                return None
        except Exception:  # noqa: BLE001 — a bad/escaping path resolves to "not allowed"
            return None
        return str(fp)

    def reconfigure(self, settings) -> None:
        self._agent.reconfigure(settings)
        self._visuals_cache = None  # the card may have changed
        self._snap = None
        # max_tokens AND the trim buffer together — resizing only max_tokens can
        # zero the trim target after a wide→narrow swap (see sync_context_window).
        self._agent.sync_context_window(self._session)

    @property
    def settings(self):
        return self._agent.settings

    # ---- permission + operator plumbing -------------------------------------------

    def set_permission_hook(self, hook: "Callable[[str, str, str, int], bool] | None") -> None:
        self._agent.tools.permission_hook = hook

    def set_clarify_hook(self, hook: "Callable[[str, list], str] | None") -> None:
        """Supply the interactive callback the `clarify` tool blocks on
        (question, choices) -> answer. Mirrors set_permission_hook: an
        interactive frontend installs it; without one, clarify degrades to a
        clear tool_error instead of fabricating an answer. Presence-gated at
        call time, like request_permission."""
        self._agent.tools.clarify_hook = hook

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
