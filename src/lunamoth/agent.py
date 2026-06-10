from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import json as _json

from .audit import AuditLog
from .cards import CharacterCard
from .config import LLMConfig, ROOT, SANDBOX_ROOT, ThoughtConfig
from .context import ContextBuffer
from .llm import LLMClient
from .memory import MemoryLimits, MemoryStore
from .persona import (
    DEFAULT_NAME,
    default_character_path,
    default_world_path,
    fallback_persona,
    system_language,
)
from . import presence
from .sandbox import Sandbox
from .state import EnvState
from .toolpacks import ToolPack, load_toolpack
from .tools import ToolGateway
from .worldinfo import Lorebook


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

    @property
    def history(self) -> list[tuple[str, str]]:
        # Backward-compatible view for UI/tests.
        return self.context.render()


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
        # Memory lives inside workspace, so the entity can alter it through sandbox Python.
        self.memory = MemoryStore(SANDBOX_ROOT / "workspace" / "memory.txt", self._memory_limits())
        self.tools = ToolGateway(self.sandbox, self.state, self.audit, self.memory)
        self._load_toolpack()
        self.llm = LLMClient(self.settings.to_llm_config(), self._build_system_messages)
        self.thought_cfg = ThoughtConfig()
        self.presence = presence.PresenceState(SANDBOX_ROOT)

    def reconfigure(self, settings: "Settings") -> None:
        """Hot-swap the LLM backend, persona, tool pack and limits at runtime."""
        self.settings = settings
        self._load_cards()  # also derives self.lang from the chosen card
        self.memory.limits = self._memory_limits()
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
            context_tokens=self.context_limit(),
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
        self.tools.set_enabled(self.toolpack.tools if self.toolpack else None)

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
        return MemoryLimits(
            max_tokens=self._effective_limit("memory_tokens", 2048),
            max_chars=self._effective_limit("memory_chars", 8000),
        )

    # Wide fallback context window so task/world cards fit; cards may narrow it.
    DEFAULT_CONTEXT_TOKENS = 1_000_000

    def context_limit(self) -> int:
        return self._effective_limit("context_tokens", self.DEFAULT_CONTEXT_TOKENS)

    def make_session(self) -> "Session":
        """Build a Session whose context window honors the active limits layer.

        The trim buffer (headroom reserved for the reply + tool round-trips) scales
        with the window — up to ~100k on the wide default — so a big context doesn't
        get filled to the brim before trimming kicks in.
        """
        session = Session()
        ctx = self.context_limit()
        session.context.max_tokens = ctx
        session.context.trim_buffer_tokens = min(100_000, max(4096, ctx // 8))
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
        chunks: list[str] = []
        for chunk in self._reply_stream(event_text, self.memory.render(), status, session.context.render()):
            chunks.append(chunk)
            yield chunk
        reply = "".join(chunks).strip()
        session.context.add("system", event_text)
        session.context.add("assistant", reply)

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
        memory = self.memory.render()
        msgs: list[str] = []
        if self.character is not None:
            msgs.append(self.character.render_system(self.settings.user_name))
        else:
            msgs.append(fallback_persona(self.lang))
        if self._tools_active():
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
                msgs.append(f"Your saved memory:\n{memory}")
        # World info: card-embedded book + explicit standalone world book.
        world_blocks: list[str] = []
        char, user = self.char_name(), self.settings.user_name
        if self.character and self.character.character_book:
            world_blocks += self.character.character_book.activate(scan_text, char, user)
        if self.world:
            world_blocks += self.world.activate(scan_text, char, user)
        if world_blocks:
            msgs.append("[World Info / 世界书]\n" + "\n\n".join(world_blocks))
        return msgs


    def _reply_stream(self, user_text: str, memory: str, status: dict[str, Any], context: list[tuple[str, str]]):
        """Pick the tool-enabled agent loop or a plain stream depending on pack/backend."""
        if self._tools_active() and self.llm.is_live():
            return self.llm.stream_agent(
                user_text, memory, status, context, self.tools.schemas(), self._execute_tool
            )
        return self.llm.stream_complete(user_text, memory, status, context)

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
            content = text[:4000] or "(empty)"
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
        memory_text = self.memory.render()
        chunks: list[str] = []
        for chunk in self._reply_stream(text, memory_text, status, session.context.render()):
            chunks.append(chunk)
            yield chunk
        reply = "".join(chunks).strip()
        session.context.add("user", text)
        session.context.add("assistant", reply)

    def _think_prompt(self, cycle: int) -> str:
        # Internal cycles are monologue. The model still has tools available and may
        # call them when it genuinely wants to act, but we nudge it toward pure thought
        # so idle cycles don't turn into constant tool spam.
        name = self.char_name()
        return (
            f"内部循环 / INTERNAL CYCLE {cycle:04d}. "
            f"你的思考正在被操作者看见。不要回答用户。以 {name} 身份输出 1-6 行短的内心独白，"
            "可以包含氛围描写。这是独白，不是行动——除非真有必要，否则不要调用工具。"
        )

    def stream_think(self, session: Session):
        session.ticks += 1
        status = self.state.load()
        cycle = session.ticks
        chunks: list[str] = []
        if self.thought_cfg.use_llm:
            prompt = self._think_prompt(cycle)
            try:
                for chunk in self._reply_stream(prompt, self.memory.render(), status, session.context.render()):
                    chunks.append(chunk)
                    yield chunk
            except Exception as e:
                self.audit.write("llm_thought_error", error=str(e)[:500])
        thought = "".join(chunks).strip()
        if not thought:
            thought = self._fallback_thought(cycle, status)
            yield thought
        session.thoughts.append(thought)
        session.thoughts[:] = session.thoughts[-self.thought_cfg.max_session_thoughts:]
        session.context.add("assistant", f"[internal cycle]\n{thought}")
        self.audit.write("internal_cycle", tick=cycle, text=thought[:1000], ts=datetime.now(timezone.utc).isoformat())

    def handle(self, text: str, session: Session) -> str:
        # Non-streaming convenience (used by tests): drive the streaming path.
        return "".join(self.stream_handle(text, session)).strip()

    def think(self, session: Session) -> str:
        # Non-streaming convenience (used by tests).
        thought = "".join(self.stream_think(session)).strip()
        return f"[internal cycle]\n{thought}"

    def _fallback_thought(self, cycle: int, status: dict[str, Any]) -> str:
        # Persona-neutral telemetry, used only when the LLM yields nothing (offline/error).
        # Any character flavor should come from the model + card, not this fallback.
        memory = self.memory.load()
        net = "on" if status.get("network_access") else "off"
        patterns = [
            f"cycle {cycle:04d}: internal loop active. buffer stable.",
            f"cycle {cycle:04d}: recall check -> {memory[:72] or 'EMPTY'}.",
            f"cycle {cycle:04d}: isolation={status.get('isolation', 'sandbox')}. network={net}.",
            f"cycle {cycle:04d}: no model output. thought continues anyway.",
        ]
        return patterns[cycle % len(patterns)]

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
                return str(self.memory.path)
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
                return "/status /memory /memory_path /files /workspace /read <filename> /wread <filename> /write <filename> <text> /logs /reset /exit"
            if cmd == "/reset":
                session.context.messages.clear()
                session.thoughts.clear()
                session.ticks = 0
                return "session context zeroed. memory document remains."
        except Exception as e:
            self.audit.write("command_error", command=text[:200], error=str(e))
            return f"command failed: {e}"
        return "unknown command. try /help"

