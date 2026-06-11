from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings

import json as _json

from .audit import AuditLog
from .cards import CharacterCard
from .config import ROOT, SANDBOX_ROOT, ThoughtConfig
from .context import ContextBuffer
from .goals import GoalStore
from .llm import LLMClient, strip_dim
from .memory import MemoryLimits, MemoryStore
from .persona import (
    DEFAULT_NAME,
    default_character_path,
    default_world_path,
    fallback_persona,
    system_language,
)
from . import presence
from . import providers
from . import rules as rules_layer
from .mcp import McpManager
from .sandbox import Sandbox
from .skills import SkillStore
from .state import EnvState
from .toolpacks import ToolPack, load_toolpack
from .tools import ToolGateway
from .transcript import TranscriptStore
from .worldinfo import Lorebook, apply_macros


def _abbrev(text: str, limit: int) -> str:
    """Collapse a tool result to a single short line for compact display."""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


@dataclass
class Session:
    context: ContextBuffer = field(default_factory=lambda: ContextBuffer(
        max_tokens=int(os.getenv("LUNAMOTH_CONTEXT_TOKENS", os.getenv("LUNAMOSS_CONTEXT_TOKENS", "65536"))),
        trim_buffer_tokens=int(os.getenv("LUNAMOTH_CONTEXT_BUFFER_TOKENS", os.getenv("LUNAMOSS_CONTEXT_BUFFER_TOKENS", "4096"))),
    ))
    thoughts: list[str] = field(default_factory=list)
    ticks: int = 0


class LunaMothAgent:
    def __init__(self, settings: "Settings | None" = None):
        from .settings import load_settings

        self.settings = settings or load_settings()
        self.lang = system_language()  # derived from the active card in _load_cards()
        self.sandbox = Sandbox(SANDBOX_ROOT)
        self.audit = AuditLog(SANDBOX_ROOT / "logs" / "audit.jsonl")
        self.state = EnvState(SANDBOX_ROOT / "env_status.json")
        self.character: CharacterCard | None = None
        self.world: Lorebook | None = None
        self.toolpack: "ToolPack | None" = None
        # Persona/card must load before memory so card-declared limits apply.
        self._load_cards()
        # Durable memory (Hermes-style two-store: memory + user). Edited via the
        # `memory` tool; a frozen snapshot (self._memory_snapshot) is injected into
        # the system prompt so mid-session writes don't churn the prompt cache.
        self.memory = MemoryStore(SANDBOX_ROOT / "memory", self._memory_limits())
        self._memory_snapshot: dict[str, list[str]] = self.memory.snapshot()
        self._memory_warnings: list[str] = []  # last limit-shrink warnings (for the frontend)
        # Charas are goal-driven: a persistent goal list (operator's ⭑ + its own)
        # steers every turn — and gives unattended time (empty user messages) a
        # direction without any engine-authored prompt.
        self.goals = GoalStore(SANDBOX_ROOT / "goals.json")
        # Skills: know-how the chara reads on demand AND writes for itself
        # (workspace/skills/ shadows user + bundled — hermes's local-first rule).
        self.skills = SkillStore()
        # MCP: operator-configured external tool servers (mcp.json); packs opt in.
        self.mcp = McpManager(config_dir=Path(os.getenv("LUNAMOTH_CONFIG_DIR", "")) if os.getenv("LUNAMOTH_CONFIG_DIR") else None)
        self.tools = ToolGateway(
            self.sandbox, self.state, self.audit, self.memory, self.goals,
            skills=self.skills, mcp=self.mcp,
        )
        self._load_toolpack()
        self.llm = LLMClient(self.settings.to_llm_config(), self._build_system_messages)
        self.thought_cfg = ThoughtConfig()
        self.presence = presence.PresenceState(SANDBOX_ROOT)
        # Durable conversation log: every context line lands here as it happens,
        # so the chara keeps its conversation across detach/attach and daemons.
        self.transcript = TranscriptStore(SANDBOX_ROOT / "transcript.db")

    def reconfigure(self, settings: "Settings") -> None:
        """Hot-swap the LLM backend, persona, tool pack and limits at runtime."""
        self.settings = settings
        self._load_cards()  # also derives self.lang from the chosen card
        # Apply (possibly changed) memory limits; shrinking discards excess + warns.
        # Warnings are stashed for the frontend to surface (and audited below).
        self._memory_warnings = self.memory.set_limits(self._memory_limits())
        for w in self._memory_warnings:
            self.audit.write("memory_shrunk", detail=w)
        self._freeze_memory()  # a reconfigure starts a fresh prompt — reload the snapshot
        self._load_toolpack()
        self.llm = LLMClient(settings.to_llm_config(), self._build_system_messages)
        self.audit.write(
            "reconfigure",
            provider=settings.provider,
            model=settings.model,
            base_url=settings.base_url,
            character=self.char_name(),
            world=(self.world.name if self.world else None),
            toolpack=(self.toolpack.name if self.toolpack else None),
            context_window=self.context_limit(),
        )

    # ---- persona / tool pack / limits (independent composable layers) -------------

    def _load_cards(self) -> None:
        """Load the persona card + its paired world book.

        An empty character path means the bundled default character (LunaMoth
        月蛾). Language is taken from the chosen card (a .zh card speaks zh, a
        .en card speaks en) — not from a separate toggle. The world book is
        the card's declared default (extensions.lunamoth.world), then the
        same-stem convention, unless the operator picked one explicitly.
        """
        self.character = None
        self.world = None
        path = (self.settings.character_path or "").strip()
        using_default_character = not path
        if using_default_character:
            default_card = default_character_path()
            path = str(default_card) if default_card else ""
        if path:
            try:
                self.character = CharacterCard.load(path)
            except Exception as e:
                self.audit.write("character_load_error", path=path, error=str(e)[:300])

        # Language follows the card — it is not a setting. Used only to pick the
        # fallback persona / the default world for the bundled default character.
        self.lang = self.character.language if self.character else system_language()

        wpath = (self.settings.world_path or "").strip()
        if not wpath and self.character is not None:
            declared = self.character.defaults().get("world")
            if declared:
                cand = (ROOT / declared) if not os.path.isabs(declared) else Path(declared)
                wpath = str(cand) if cand.exists() else ""
        if not wpath and using_default_character and self.character is not None:
            default_world = default_world_path(self.lang)
            wpath = str(default_world) if default_world else ""
        if wpath:
            try:
                self.world = Lorebook.load(wpath)
            except Exception as e:
                self.audit.write("world_load_error", path=wpath, error=str(e)[:300])

    def _load_toolpack(self) -> None:
        """Load the tool pack (the 'what it can do' layer) and apply it to the gateway.

        Empty toolpack setting falls back to the card's declared default, then to
        the safe built-in 'sandbox' pack — so a plain SillyTavern card that
        carries no tools/limits still gets a sensible, safe capability set.
        """
        self.toolpack = None
        choice = (self.settings.toolpack or "").strip()
        if not choice and self.character is not None:
            choice = str(self.character.defaults().get("toolpack", "")).strip()
        if not choice:
            choice = "sandbox"
        try:
            self.toolpack = load_toolpack(choice)
        except Exception as e:
            self.audit.write("toolpack_load_error", path=choice, error=str(e)[:300])
        self.tools.set_enabled(
            self.toolpack.tools if self.toolpack else None,
            mcp_servers=self.toolpack.mcp_servers if self.toolpack else None,
        )

    def _card_limit(self, key: str) -> int | None:
        """A limit declared by the card, in extensions.lunamoth or top-level extensions."""
        if not self.character:
            return None
        for source in (self.character.defaults(), self.character.extensions):
            v = source.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
            if isinstance(v, str) and v.strip().isdigit():
                return int(v)
        return None

    def _effective_limit(self, key: str, default: int) -> int:
        """Precedence: explicit Overdrive in settings (>0) > card default > built-in fallback."""
        override = int(getattr(self.settings, key, 0) or 0)
        if override > 0:
            return override
        card = self._card_limit(key)
        if card is not None:
            return card
        return default

    def _memory_limits(self) -> MemoryLimits:
        # Both stores are card-settable (extensions.lunamoth.{memory_chars,user_chars})
        # and overridable at runtime via settings; 079's tiny memory is characterful.
        return MemoryLimits(
            memory_chars=self._effective_limit("memory_chars", 4000),
            user_chars=self._effective_limit("user_chars", 2000),
        )

    def _freeze_memory(self) -> None:
        """Snapshot memory for the system prompt. Called when a fresh prompt/session
        begins (init, reconfigure, new session, /reset) — NOT per turn, so mid-session
        `memory` tool writes don't mutate the cached prompt prefix."""
        self._memory_snapshot = self.memory.snapshot()

    def _memory_text(self) -> str:
        """The frozen snapshot rendered as the system-prompt memory block (bilingual)."""
        snap = getattr(self, "_memory_snapshot", None) or {}
        mem, usr = snap.get("memory") or [], snap.get("user") or []
        if not mem and not usr:
            return ""
        zh = str(self.lang).startswith("zh")
        parts: list[str] = []
        if mem:
            head = "你为自己留存的记忆：" if zh else "Your memory (notes you've kept for yourself):"
            parts.append(head + "\n" + "\n".join(f"- {e}" for e in mem))
        if usr:
            head = "关于操作者：" if zh else "About the operator:"
            parts.append(head + "\n" + "\n".join(f"- {e}" for e in usr))
        return "\n\n".join(parts)

    # Attach restores only the transcript tail; the full history stays on disk.
    RESTORE_MAX_MESSAGES = 400

    def context_limit(self) -> int:
        """The model's REAL context window — read from the provider, never set by
        the operator or a card. Cached per (provider, base_url, model); a model
        swap via reconfigure refetches it."""
        s = self.settings
        key = (s.provider, s.base_url, s.model)
        if getattr(self, "_ctx_window_key", None) != key:
            self._ctx_window_key = key
            self._ctx_window = providers.context_window(s.provider, s.base_url, s.model, s.api_key)
        return self._ctx_window

    def make_session(self) -> "Session":
        """Build a Session whose context window honors the active limits layer.

        The trim buffer (headroom reserved for the reply + tool round-trips) scales
        with the window — up to ~100k on the wide default — so a big context doesn't
        get filled to the brim before trimming kicks in.
        """
        session = Session()
        self._freeze_memory()  # a new session = a fresh prompt → reload the memory snapshot
        ctx = self.context_limit()
        session.context.max_tokens = ctx
        session.context.trim_buffer_tokens = min(100_000, max(4096, ctx // 8))
        # Durable conversation: restore the TAIL of the current transcript epoch
        # (a long-lived chara's full history would be loaded only to be trimmed),
        # then persist every new message back — conversations survive restarts.
        session.context.restore(self.transcript.load(max_messages=self.RESTORE_MAX_MESSAGES))
        session.context.persist = self.transcript.append_message
        return session

    def char_name(self) -> str:
        return self.character.name if self.character else DEFAULT_NAME

    def _tools_active(self) -> bool:
        """Whether any tools are available — driven by the selected pack, not the persona."""
        return self.tools.has_tools()

    def greeting(self) -> str | None:
        """Opening message shown without an LLM call (SillyTavern first_mes), if any."""
        if self.character:
            g = self.character.greeting(self.settings.user_name)
            return g or None
        return None

    # ---- presence (operator attach/detach awareness) -------------------------------

    def attach_event_text(self) -> str:
        """The card's arrival prompt ('' when the card declares none)."""
        return presence.attach_text(self.character, self.char_name(), self.settings.user_name)

    def detach_event_text(self) -> str:
        """The card's departure prompt ('' when the card declares none)."""
        return presence.detach_text(self.character, self.char_name(), self.settings.user_name)

    def stream_event(self, event_text: str, session: Session):
        """Stream the character's reaction to a presence event.

        The event is an engine-injected context line (role: system), not operator
        speech — it is audited as a presence event, never as a user message.
        """
        self.audit.write("presence_event", kind="attach", text=event_text[:300])
        status = self.state.load()
        # Commit the event line BEFORE streaming (interrupt-safe).
        session.context.add("system", event_text)
        agent_loop = self._agent_loop_active()
        chunks: list[str] = []
        committed = False
        try:
            stream = self._reply_stream(
                event_text, self._memory_text(), status, self._context_view(session),
                in_context=True, record=session.context.add_message,
            )
            for chunk in stream:
                chunks.append(chunk)
                yield chunk
            committed = True
            if not agent_loop:
                reply = strip_dim("".join(chunks)).strip()
                if reply:
                    session.context.add("assistant", reply)
        finally:
            if not committed and not agent_loop:
                partial = strip_dim("".join(chunks)).strip()
                if partial:
                    session.context.add("assistant", partial + self.llm.INTERRUPT_MARK)

    def note_detach(self, session: Session) -> None:
        """Record the operator leaving: context line, audit, and a handoff event
        queued for whichever process adopts this chara next (e.g. the daemon)."""
        text = self.detach_event_text()
        if not text:
            return
        self.audit.write("presence_event", kind="detach", text=text[:300])
        session.context.add("system", text)
        self.presence.queue_event(text)

    def _build_system_messages(self, scan_text: str) -> list[str]:
        status = self.state.load()
        memory = self._memory_text()  # FROZEN snapshot (see _freeze_memory), not live — cache-stable
        char, user = self.char_name(), self.settings.user_name
        tools_on = self._tools_active()
        msgs: list[str] = []
        # A card MAY override the rules / closer via extensions.lunamoth.{rules,
        # rules_closer}. Bundled cards leave these empty — it's just an open hook.
        card_ext = self.character.defaults() if self.character else {}
        card_rules = str(card_ext.get("rules", "") or "")
        card_closer = str(card_ext.get("rules_closer", "") or "")

        # 1) Who it is — the character card IS the soul. Identity, voice and
        #    autonomy all come from the card; the engine adds no identity of its own.
        if self.character is not None:
            msgs.append(self.character.render_system(self.settings.user_name))
        else:
            msgs.append(fallback_persona(self.lang))

        # 2) Rules — a neutral, character-agnostic operating standard (agency over
        #    your sandbox + your work must be real + act through tools). ONLY when
        #    the chara actually has tools; a tool-less chara is free to narrate.
        if tools_on:
            msgs.append(apply_macros(rules_layer.rules(self.lang, card_rules), char, user))
            # Native tool schemas already describe each tool, so no prose tool spec —
            # just a short, neutral nudge + the live env facts.
            net = "on" if status.get("network_access") else "off"
            who = "present" if status.get("user_present") else "away"
            msgs.append(
                "You have tools available via native function calling. Call them directly when "
                "you want to act; never paste code in prose or claim a result before the tool returns.\n"
                f"Environment: isolation={status.get('isolation', 'sandbox')}, network={net}, "
                f"operator={who}, workspace is your read/write directory."
            )
            if self.toolpack and self.toolpack.note.strip():
                msgs.append(self.toolpack.note.strip())
            if memory.strip():
                msgs.append(memory)  # already headed (memory / user blocks)
            # Goals steer every turn — and give unattended time its direction.
            goals_block = self.goals.render_block()
            if goals_block:
                msgs.append(goals_block)
            # Skill index: names + one-liners only (progressive disclosure —
            # the full text is a read_skill call away).
            skills_block = self.skills.render_block()
            if skills_block:
                msgs.append(skills_block)

        # World info: card-embedded book + explicit standalone world book.
        world_blocks: list[str] = []
        if self.character and self.character.character_book:
            world_blocks += self.character.character_book.activate(scan_text, char, user)
        if self.world:
            world_blocks += self.world.activate(scan_text, char, user)
        if world_blocks:
            msgs.append("[World Info / 世界书]\n" + "\n\n".join(world_blocks))

        # 3) Closer — the last, strongest steer (SillyTavern post-history style),
        #    only when tools are on. Placed last so it weighs most before generation.
        if tools_on:
            msgs.append(apply_macros(rules_layer.closer(self.lang, card_closer), char, user))
        return msgs


    def _agent_loop_active(self) -> bool:
        """True when turns run through the tool-calling loop (which commits its
        own messages via `record`); False = plain stream, the caller commits."""
        return self._tools_active() and self.llm.is_live()

    def _context_view(self, session: Session) -> list[dict]:
        """The API view of the context — reasoning echoed back only for providers
        that demand it (DeepSeek thinking mode). Compaction runs first so the view
        never overflows the model's real window."""
        self._maybe_compact(session)
        return session.context.render(include_reasoning=self.llm.reasoning_echoback_required())

    def _maybe_compact(self, session: Session, *, force: bool = False) -> bool:
        """Summarize the old part of the window when it nears the model's real
        context limit (compaction.py). Runs on the streaming worker thread, so a
        blocking summary call is fine. Best-effort; never raises."""
        try:
            from . import compaction

            if force or compaction.should_compact(session.context, self.llm):
                changed = compaction.compact(session.context, self.lang, self.llm, force=force)
                if changed:
                    self.audit.write("compacted", tokens=session.context.token_count())
                return changed
        except Exception as e:  # compaction must never break a turn
            self.audit.write("compact_error", error=str(e)[:200])
        return False

    def _reply_stream(
        self, user_text: str, memory: str, status: dict[str, Any], context: list[dict],
        *, in_context: bool = True, record=None, reasoning: "str | None" = None,
    ):
        """Pick the tool-enabled agent loop or a plain stream depending on pack/backend."""
        if self._agent_loop_active():
            return self.llm.stream_agent(
                user_text, memory, status, context, self.tools.schemas(), self._execute_tool,
                record=record, in_context=in_context, reasoning=reasoning,
            )
        return self.llm.stream_complete(user_text, memory, status, context, in_context=in_context, reasoning=reasoning)

    def _execute_tool(self, tool_call: dict[str, Any]) -> dict[str, str]:
        """Run one native tool call; return a compact display line + the result fed back to the model."""
        fn = tool_call.get("function", {})
        name = fn.get("name", "")
        raw = fn.get("arguments") or "{}"
        try:
            args = _json.loads(raw) if raw.strip() else {}
        except _json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        result = self.tools.call(name, **args)
        if result.get("ok"):
            data = result.get("data", "")
            text = data if isinstance(data, str) else _json.dumps(data, ensure_ascii=False)
            head = f"⚙ {name} ✓ ({len(text)} chars)" if name == "terminal" else f"⚙ {name} ✓"
            snippet = _abbrev(text, 200)
            display = f"{head}\n  {snippet}" if snippet else head
            content = text[:6000] or "(empty)"
            if len(text) > 6000:
                # Truncation must be EXPLICIT — silent cuts read as complete output
                # and send the model down wrong paths (hermes does the same).
                content += f"\n[output truncated — {len(text)} chars total; read the rest in pieces if needed]"
        else:
            err = str(result.get("error", ""))
            display = f"⚙ {name} ✗ {_abbrev(err, 160)}"
            content = f"ERROR: {err}"
        return {"display": display, "content": content}

    def stream_handle(self, text: str, session: Session):
        text = text.strip()
        self.audit.write("user_message", text=text[:1000], streaming=True)
        if not text:
            yield "..."
            return
        if text.startswith("/"):
            yield self._command(text, session)
            return
        status = self.state.load()
        memory_text = self._memory_text()
        # Commit the operator's message BEFORE streaming: an interrupted reply
        # must never lose the instruction that caused it.
        session.context.add("user", text)
        agent_loop = self._agent_loop_active()
        chunks: list[str] = []
        committed = False
        try:
            stream = self._reply_stream(
                text, memory_text, status, self._context_view(session),
                in_context=True, record=session.context.add_message,
            )
            for chunk in stream:
                chunks.append(chunk)
                yield chunk
            committed = True
            if not agent_loop:
                reply = strip_dim("".join(chunks)).strip()
                if reply:
                    session.context.add("assistant", reply)
        finally:
            # Operator interrupt (the UI abandoned this generator): in the plain
            # path WE must keep the partial; the agent loop keeps its own.
            if not committed and not agent_loop:
                partial = strip_dim("".join(chunks)).strip()
                if partial:
                    session.context.add("assistant", partial + self.llm.INTERRUPT_MARK)

    def _record_think(self, session: Session):
        """record() wrapper for idle cycles: monologue text is tagged kind='think'
        (so old cycles age out of the API view — see ContextBuffer.render), while
        tool calls/results the chara makes stay untagged: real actions are worth
        remembering at full strength."""

        def record(msg: dict) -> None:
            if msg.get("role") == "assistant" and not msg.get("tool_calls") and msg.get("content"):
                msg = {**msg, "kind": "think"}
            session.context.add_message(msg)

        return record

    def stream_think(self, session: Session):
        session.ticks += 1
        status = self.state.load()
        cycle = session.ticks
        agent_loop = self._agent_loop_active()
        chunks: list[str] = []
        committed = False

        def commit(interrupted: bool) -> None:
            nonlocal committed
            if committed:
                return
            committed = True
            thought = strip_dim("".join(chunks)).strip()
            if thought:
                session.thoughts.append(thought)
                session.thoughts[:] = session.thoughts[-self.thought_cfg.max_session_thoughts:]
                if not agent_loop:
                    mark = self.llm.INTERRUPT_MARK if interrupted else ""
                    session.context.add("assistant", f"{thought}{mark}", kind="think")
            self.audit.write("internal_cycle", tick=cycle, text=thought[:1000], ts=datetime.now(timezone.utc).isoformat())

        try:
            if self.thought_cfg.use_llm:
                # No invented "internal cycle" instruction: an idle tick is an
                # EMPTY user message — the documented convention (rules layer)
                # for "no one is speaking to you; time is passing". What the
                # chara does with unattended time is the card's business, not
                # ours. The empty message is ephemeral (in_context=False).
                #
                # NO failure fallback: if the request fails (after the client's
                # own retries) the error propagates to the UI as an error — a
                # failed request is a failed request, never fabricated output.
                stream = self._reply_stream(
                    "", self._memory_text(), status, self._context_view(session),
                    in_context=False, record=self._record_think(session),
                )
                try:
                    for chunk in stream:
                        chunks.append(chunk)
                        yield chunk
                except Exception as e:
                    self.audit.write("llm_thought_error", error=str(e)[:500])
                    raise
            commit(False)
        finally:
            commit(True)  # no-op unless the generator was abandoned mid-stream

    def handle(self, text: str, session: Session) -> str:
        # Non-streaming convenience (used by tests): drive the streaming path.
        return "".join(self.stream_handle(text, session)).strip()

    def think(self, session: Session) -> str:
        # Non-streaming convenience (used by tests).
        thought = "".join(self.stream_think(session)).strip()
        return thought

    def _command(self, text: str, session: Session) -> str:
        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()
        try:
            if cmd == "/status":
                data = self.tools.call("inspect_env")
                data["context_tokens_est"] = session.context.token_count()
                return self.tools.as_json(data)
            if cmd == "/memory":
                return self.memory.render()
            if cmd == "/memory_path":
                return str(self.memory.root)
            if cmd == "/files":
                return self.tools.as_json(self.tools.call("list_files"))
            if cmd == "/workspace":
                return self.tools.as_json(self.tools.call("list_workspace"))
            if cmd == "/read":
                if len(parts) < 2:
                    return "usage: /read <filename>"
                return self.tools.as_json(self.tools.call("read_file", filename=parts[1]))
            if cmd == "/wread":
                if len(parts) < 2:
                    return "usage: /wread <filename>"
                return self.tools.as_json(self.tools.call("read_workspace_file", filename=parts[1]))
            if cmd == "/write":
                if len(parts) < 3:
                    return "usage: /write <filename> <text>"
                return self.tools.as_json(self.tools.call("write_file", filename=parts[1], text=parts[2]))
            if cmd == "/logs":
                return self.tools.as_json(self.audit.tail(20))
            if cmd == "/help":
                return "/status /memory /memory_path /files /workspace /read <filename> /wread <filename> /write <filename> <text> /logs /compact /reset /exit"
            if cmd == "/compact":
                before = session.context.token_count()
                if not self.llm.is_live():
                    return "compaction needs a live model (offline/mock can't summarize)."
                if self._maybe_compact(session, force=True):
                    after = session.context.token_count()
                    return f"compacted: ~{before} → ~{after} tokens (older turns folded into a summary; full history stays on disk)."
                return "nothing to compact yet (the window isn't long enough to be worth summarizing)."
            if cmd == "/reset":
                session.context.messages.clear()
                session.thoughts.clear()
                session.ticks = 0
                # New transcript epoch: old history stays on disk but is no
                # longer reloaded on attach.
                self.transcript.reset()
                return "session context zeroed (new transcript epoch). durable memory remains."
        except Exception as e:
            self.audit.write("command_error", command=text[:200], error=str(e))
            return f"command failed: {e}"
        return "unknown command. try /help"

