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
from ..core.context import estimate_tokens  # noqa: F401
from .. import presence

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
    name: str   # "wish"
    usage: str  # "/wish [text | done <id> | drop <id>]"
    help: str   # one line


@dataclass(frozen=True)
class AttachInfo:
    """Everything a frontend needs to open a session.

    `opening` is the DECIDED first move (the greeting decision tree lives here,
    not in each frontend): 'greeting' = display opening_text and call
    record_greeting · 'arrival' = stream_event(opening_text) · 'probe' =
    stream_user(opening_text) · 'none' = continue silently.

    NOTE: attach() today only ever decides 'greeting' (first meeting) or 'none'.
    'arrival' and 'probe' are RESERVED, forward-compat values: the presence model
    deliberately keeps a RE-attach silent (the chara registers you only when you
    speak — the entered marker carries that, see _presence_marker), so nothing
    currently emits them. They stay in the contract (and the frontend branches
    stay live) for the future GM/world-event layer; do NOT mistake them for dead
    code, and do NOT remove the 'event' stream path — stream_event uses it for
    world events independently of these openings. (The old card on_attach/on_detach
    'reaction turn' hook was removed; on_attach/on_detach now only override the
    neutral enter/leave marker TEXT — see presence.marker_text.)"""
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
    user_present: bool
    rest_until: float        # epoch the chara chose to rest until (0 = not resting)
    quiet: int               # engagement: silence (s) before it resumes its own work
    patience: float          # effective base seconds between spontaneous cycles
    embodiment: str          # literal | actor
    context_tokens: int
    context_max: int
    memory_chars: int
    memory_max: int
    memory_text: str
    memory_path: str
    sandbox_root: str
    workspace_root: str
    # Wishes/skills/MCP listings are NOT here on purpose: the snapshot feeds a
    # status line polled several times a second, and those need disk walks.
    # Rich UIs get them from /wish /skills /mcp Reply.data on demand.


def test_connection(settings) -> tuple[bool, str]:
    """Validate endpoint+key+model for a Settings draft (welcome screen/wizard)."""
    from ..core.llm import LLMClient

    return LLMClient(settings.to_llm_config()).test_connection()


def browser_driver_status() -> tuple[str | None, bool]:
    """The optional browser_* tool driver's state, for `lunamoth setup browser`
    / `doctor`. Returns (agent_browser_cli_path_or_None, chromium_installed).
    Frontends reach the backend only through this layer; cli.py must not import
    tools/ directly (architecture rule)."""
    from ..tools.builtin import _browser_driver as drv

    drv._reset_caches_for_test()  # report the real current state, not a stale cache
    return drv.find_agent_browser(), drv.chromium_installed()


class CharaHandle:
    """In-process implementation: wraps one LunaMothAgent + its session."""

    # Status lines poll snapshot() many times a second; state is files on disk.
    _SNAPSHOT_TTL = 0.5

    def __init__(self, settings=None, agent=None):
        from ..core.agent import LunaMothAgent

        self._agent = agent or LunaMothAgent(settings)
        self._session = None
        self._snap: "tuple[float, StateSnapshot] | None" = None
        # Latches once a human has been delivered the opener — a resident
        # greets once per life, not once per page-load. A background (present
        # =False) adopt never sets it, so it can't eat the human's greeting.
        self._greeted = False
        # The card greeting (first_mes) is persisted to the transcript exactly
        # once, server-side, the moment attach() decides to show it — NOT left to
        # a frontend `greet` round-trip (which, if dropped, would show the opener
        # once but never record it, losing it from the transcript + board while
        # the first-meeting flag is already consumed). This latch makes the later
        # `greet` RPC idempotent.
        self._greeting_committed = False
        # Visit bookkeeping: a visit is presence-true → presence-false. A
        # wordless visit leaves NO trace; the first words insert a neutral
        # "entered" marker (once) and leaving after speaking adds a "left" one.
        self._visit_spoke = False
        self._visit_announced = False

    # ---- lifecycle -------------------------------------------------------------

    def attach(self, present: bool = True) -> AttachInfo:
        """Open (or adopt) the conversation. `present` = a human is watching;
        a daemon attaches with present=False to drive idle life.

        The state machine (so every frontend, and the supervisor's background
        adopt-then-human-attach sequence, behaves identically):
        - present=False: pure adoption — restore, set presence false, adopt a
          queued handoff. NEVER greets and NEVER consumes the first-meeting,
          so the daemon pre-attaching a resident can't eat the human's opener.
        - present=True, first human this life: the greeting decision tree below
          runs once, then `_greeted` latches.
        - present=True, already greeted (reconnect / page reload): presence
          fact only, opening='none' — a resident greets once per life, not per
          page-load. `restored` is recomputed every time, so a reconnect after
          a conversation shows that conversation (the old stale-cache bug)."""
        a = self._agent
        if self._session is None:
            self._session = a.make_session()
        restored = self._display_restored()
        a.state.set_present(present)

        if not present:
            # Daemon/background adoption: continue knowing the operator left.
            handoff = a.presence.pop_event()
            recent = self._session.context.messages[-3:]
            if handoff and not any(
                m.get("role") == "system" and m.get("content") == handoff for m in recent
            ):
                self._session.context.add("system", handoff)
                restored = self._display_restored()
            return AttachInfo(
                char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
                show_thinking=bool(a.settings.show_thinking),
                restored=restored, opening="none", opening_text="",
            )

        # present=True — a human is watching.
        a.presence.pop_event()  # discard any stale handoff — we're here now
        # A reconnect after the opener was already delivered (same life) is
        # presence bookkeeping only — a resident greets once per life, not once
        # per page-load.
        if self._greeted:
            a.presence.mark_met()
            self._begin_visit()
            return AttachInfo(
                char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
                show_thinking=bool(a.settings.show_thinking),
                restored=self._display_restored(), opening="none", opening_text="",
            )
        # Entering the room never wakes a RESTING chara: stay silent — but do NOT
        # consume the first meeting. The card's first_mes is the chara's one
        # introduction; a nap must DEFER it, never burn it, so it still shows the
        # first time the chara is awake and actually seen (regression: folding
        # `resting` into the greeted short-circuit marked-met a never-greeted
        # sleeper and lost its first_mes forever).
        resting = float(a.state.load().get("rest_until", 0.0) or 0.0) > time.time()
        if resting:
            self._begin_visit()
            return AttachInfo(
                char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
                show_thinking=bool(a.settings.show_thinking),
                restored=self._display_restored(), opening="none", opening_text="",
            )
        # Entering the room never forces a turn: you can just watch the chara
        # do its own thing. The ONLY opener is the card's designed first_mes,
        # shown ONCE at the first meeting (a brand-new chara introducing
        # itself). A return visit, or a card with no first_mes, opens silently
        # — the chara learns you arrived only when YOU speak (see stream_user).
        greeting = a.greeting() or ""
        # The first-meeting flag is the authority (mark_met() flips it after this
        # attach, so a reconnect never re-greets). Do NOT also gate on `restored`:
        # a live chara that already did some self-work before you first open the
        # chat has a non-empty transcript, but this is STILL its first meeting with
        # you and its first_mes intro must show.
        first = a.presence.first_meeting()
        if greeting and first:
            opening, opening_text = "greeting", greeting
        else:
            opening, opening_text = "none", ""
        info = AttachInfo(
            char_name=a.char_name(), lang=a.lang, mode=a.settings.mode,
            show_thinking=bool(a.settings.show_thinking),
            restored=restored, opening=opening, opening_text=opening_text,
        )
        a.presence.mark_met()
        self._greeted = True
        # Persist the opener NOW, server-side — not on a frontend round-trip.
        # `restored` was captured above (pre-greeting), so this never double-shows
        # on this attach; on any reconnect the greeting rides `restored` from the
        # transcript. record_greeting is idempotent, so the frontend's later
        # `greet` call is a harmless no-op.
        if opening == "greeting" and opening_text:
            self.record_greeting(opening_text)
        if present:
            self._begin_visit()
        return info

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

    def _begin_visit(self) -> None:
        self._visit_spoke = False
        self._visit_announced = False

    def _presence_marker(self, kind: str) -> str:
        """The neutral '<user> entered/left the conversation' FACT. Names the
        operator by {{user}}; the bundled default is English, and a card MAY
        override the wording (in any language) via extensions.lunamoth.on_attach
        / on_detach (see presence.marker_text). A passive context line — never a
        roleplay turn."""
        a = self._agent
        return presence.marker_text(a.character, kind, a.char_name(), a.settings.user_name)

    def record_greeting(self, text: str) -> None:
        """Commit the card greeting (first_mes) to the conversation, exactly once.

        attach() calls this server-side the moment it decides to greet, so the
        opener reaches the transcript (and thus the board preview + any reconnect)
        even if the frontend never completes its `greet` round-trip. The latch
        makes that frontend call — and any double-fire — a no-op."""
        if self._greeting_committed or not text:
            return
        self._greeting_committed = True
        if self._session is None:
            self._session = self._agent.make_session()
        self._session.context.add("assistant", text)

    def detach(self) -> None:
        """Presence bookkeeping on the way out (idempotence is the caller's job).

        A wordless visit leaves NO trace. A visit where the operator spoke queues
        a single neutral departure marker — NON-BLOCKING: it is NOT injected into
        the live context now. Injecting immediately would interrupt work in flight
        (the common case: you assign a task, then leave it to run). Instead the
        chara finishes the current turn, then picks the marker up at its next own
        cycle (stream_think flushes the queue) or when another process adopts it —
        so it registers 'you left' AFTER wrapping up, then goes fully autonomous."""
        if self._visit_spoke:
            marker = self._presence_marker("left")
            self._agent.presence.queue_event(marker)  # deferred; flushed at the next cycle
            self._agent.audit.write("presence_event", kind="left", text=marker[:120])
        self._visit_spoke = False
        self._visit_announced = False
        self._agent.state.set_present(False)

    def set_present(self, present: bool) -> None:
        if present and self._session is not None:
            self._begin_visit()
        self._agent.state.set_present(present)

    # ---- conversation (generators of protocol events) ---------------------------

    def stream_user(self, text: str, attachments=None) -> "Iterator[Event]":
        # The first words of a visit insert a neutral "operator entered" fact
        # before the message — entering was silent, engaging is what the chara
        # registers. Once per visit.
        if self._session is not None and not self._visit_announced:
            self._visit_announced = True
            marker = self._presence_marker("entered")
            self._session.context.add("system", marker)
            self._agent.audit.write("presence_event", kind="entered", text=marker[:120])
        self._visit_spoke = True
        return self._agent.stream_handle(text, self._session, attachments)

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
            rest_until=float(status.get("rest_until", 0.0) or 0.0),
            quiet=int(getattr(a.settings, "quiet", 300)),
            patience=float(a.effective_patience()),
            embodiment=a.effective_embodiment(),
            context_tokens=self._session.context.token_count() if self._session else 0,
            context_max=self._session.context.max_tokens if self._session else a.context_limit(),
            memory_chars=mem.chars("memory") + mem.chars("user"),
            memory_max=(mem.limits.memory_chars + mem.limits.user_chars) or 1,
            memory_text=mem.render(),
            memory_path=str(mem.root),
            sandbox_root=str(a.sandbox.root),
            workspace_root=str(a.sandbox.root / "workspace"),
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
