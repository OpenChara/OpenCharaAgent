from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import mimetypes
import os
import shutil
import urllib.parse
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..session.settings import Settings

import json as _json

from ..obs.audit import AuditLog
from ..content.cards import CharacterCard
from ..obs import get_logger, setup_logging
from ..config import SANDBOX_ROOT, ThoughtConfig
from .context import ContextBuffer, _msg_text, estimate_tokens
from ..tools.goals import GoalStore
from .llm import LLMClient
from ..protocol import MUSE, Notice, TextDelta
from .attachments import (
    INLINE_IMAGE_MAX_BYTES, IngestResult, RawAttachment, build_user_content, ingest_attachments,
)
from ..tools.memory import MemoryLimits, MemoryStore
from ..content.persona import (
    DEFAULT_NAME,
    default_character_path,
    fallback_persona,
    system_language,
)
from .. import presence
from . import providers
from ..content import rules as rules_layer
from ..tools.mcp import McpManager
from ..tools.sandbox import Sandbox
from ..tools.skills import SkillStore
from .state import EnvState
from ..tools.toolpacks import ToolPack, load_toolpack
from ..tools.gateway import ToolGateway
from .transcript import TranscriptStore
from ..content.worldinfo import apply_macros
from ..content.knobs import normalize_embodiment, parse_patience

_log = get_logger("agent")

# The Settings.user_name default — card-declared user_name applies only when the
# operator hasn't overridden it (precedence: operator > card > default).
_DEFAULT_USER_NAME = "操作者"


def _abbrev(text: str, limit: int) -> str:
    """Collapse a tool result to a single short line for compact display."""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


# The faithful per-turn request log keeps only the most recent N requests.
_REQUEST_LOG_MAX_LINES = 200


def _append_request_log(kind: str, system: list[str], messages: list[dict],
                        tools: list[str], model: str) -> None:
    """Append one faithful request record to SANDBOX_ROOT/logs/requests.jsonl.

    This is debug instrumentation — ALWAYS on but capped at the last
    _REQUEST_LOG_MAX_LINES lines. Best-effort, exactly like the audit log:
    a logging failure must NEVER raise into the turn (no-fallback applies to
    the model output, not to this side channel)."""
    try:
        path = SANDBOX_ROOT / "logs" / "requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "system": list(system),
                "messages": messages,
                "tools": list(tools),
                "model": model,
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # Cheap rotation: only re-read+trim when we are over the cap.
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > _REQUEST_LOG_MAX_LINES:
            with path.open("w", encoding="utf-8") as fh:
                fh.writelines(lines[-_REQUEST_LOG_MAX_LINES:])
    except Exception:  # noqa: BLE001 - logging must never break a turn
        pass


@dataclass
class Session:
    context: ContextBuffer = field(default_factory=lambda: ContextBuffer(
        max_tokens=int(os.getenv("LUNAMOTH_CONTEXT_TOKENS", os.getenv("LUNAMOSS_CONTEXT_TOKENS", "65536"))),
        trim_buffer_tokens=int(os.getenv("LUNAMOTH_CONTEXT_BUFFER_TOKENS", os.getenv("LUNAMOSS_CONTEXT_BUFFER_TOKENS", "4096"))),
    ))
    ticks: int = 0
    wi_sticky: dict[str, int] = field(default_factory=dict)


class LunaMothAgent:
    def __init__(self, settings: "Settings | None" = None):
        from ..session.settings import load_settings

        setup_logging()  # idempotent — whoever builds an agent gets diagnostics
        self.settings = settings or load_settings()
        self.lang = system_language()  # derived from the active card in _load_cards()
        self.sandbox = Sandbox(SANDBOX_ROOT)
        self.audit = AuditLog(SANDBOX_ROOT / "logs" / "audit.jsonl")
        self.state = EnvState(SANDBOX_ROOT / "env_status.json")
        self.character: CharacterCard | None = None
        self.toolpack: "ToolPack | None" = None
        # Persona/card must load before memory so card-declared limits apply.
        self._load_cards()
        # Durable memory (Hermes-style two-store: memory + user). Edited via the
        # `memory` tool; a frozen snapshot (self._memory_snapshot) is injected into
        # the system prompt so mid-session writes don't churn the prompt cache.
        self.memory = MemoryStore(SANDBOX_ROOT / "memory", self._memory_limits())
        self._memory_snapshot: dict[str, list[str]] = self.memory.snapshot()
        self._memory_warnings: list[str] = []  # last limit-shrink warnings (for the frontend)
        # Charas are wish-driven: a persistent wish list (operator's ⭑ + its own)
        # steers every turn — and gives unattended time (empty user messages) a
        # direction without any engine-authored prompt. The store stays on disk
        # as goals.json (legacy filename, invisible to users).
        self.wishes = GoalStore(SANDBOX_ROOT / "goals.json")
        self._seed_card_wishes()
        # Skills: know-how the chara reads on demand AND writes for itself
        # (workspace/skills/ shadows user + bundled — hermes's local-first rule).
        self.skills = SkillStore()
        self._skills_snapshot: str | None = None
        # MCP: operator-configured external tool servers (mcp.json); packs opt in.
        self.mcp = McpManager(config_dir=Path(os.getenv("LUNAMOTH_CONFIG_DIR", "")) if os.getenv("LUNAMOTH_CONFIG_DIR") else None)
        self.tools = ToolGateway(
            self.sandbox, self.state, self.audit, self.memory, self.wishes,
            skills=self.skills, mcp=self.mcp,
        )
        self._load_toolpack()
        self._stable_prefix_cache: list[str] | None = None
        self._art_staged = False  # assets/ sibling populated once per session, not per prefix build
        self.llm = LLMClient(self.settings.to_llm_config())
        self.thought_cfg = ThoughtConfig()
        self.presence = presence.PresenceState(SANDBOX_ROOT)
        # Durable conversation log: every context line lands here as it happens,
        # so the chara keeps its conversation across detach/attach and daemons.
        self.transcript = TranscriptStore(SANDBOX_ROOT / "transcript.db")
        # The tool registry's handlers reach the llm (web/execute/delegate) and
        # the transcript (session_search) through the ToolContext — bind them now
        # that both exist (the gateway was built before them).
        self.tools.set_runtime(llm=self.llm, transcript=self.transcript)

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
        self._seed_card_wishes()
        self._freeze_skills()
        self._invalidate_stable_prefix()
        self.llm = LLMClient(settings.to_llm_config())
        self.tools.set_runtime(llm=self.llm, transcript=self.transcript)
        self.audit.write(
            "reconfigure",
            provider=settings.provider,
            model=settings.model,
            base_url=settings.base_url,
            character=self.char_name(),
            toolpack=(self.toolpack.name if self.toolpack else None),
            context_window=self.context_limit(),
        )

    def swap_model(self, model: str) -> None:
        """Session-scoped model hot-swap (/model): rebuilds only the LLM client.

        Deliberately NOT persisted — a restart returns to the configured
        model; the global default is untouched. The stable prefix is not
        invalidated (provider prompt caches are per-model anyway)."""
        self.settings.model = model.strip()
        self.llm = LLMClient(self.settings.to_llm_config())
        self.audit.write("model_swap", model=self.settings.model)

    # ---- persona / tool pack / limits (independent composable layers) -------------

    def _load_cards(self) -> None:
        """Load the persona card — the ONE external file.

        An empty character path means the bundled default card. Language is
        taken from the chosen card (a .zh card speaks zh, a .en card speaks en)
        — not from a separate toggle. The world lives INSIDE the card as the
        embedded `character_book`; there is no standalone world channel.
        """
        self.character = None
        path = (self.settings.character_path or "").strip()
        if not path:
            default_card = default_character_path()
            path = str(default_card) if default_card else ""
        if path:
            try:
                self.character = CharacterCard.load(path)
            except Exception as e:
                self.audit.write("character_load_error", path=path, error=str(e)[:300])

        # Language follows the card — it is not a setting. Used only to pick
        # the fallback persona when no card loads at all.
        self.lang = self.character.language if self.character else system_language()

        # The card may name the operator (extensions.lunamoth.user_name). Apply
        # it only when the operator hasn't set their own — precedence stays
        # operator override > card > default, like every other knob.
        if self.character is not None:
            declared_user = self.character.user_name_override()
            if declared_user and self.settings.user_name in ("", _DEFAULT_USER_NAME):
                self.settings.user_name = declared_user

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

    def _seed_card_wishes(self) -> None:
        defaults = self.character.defaults() if self.character else {}
        wishes = defaults.get("wishes") if isinstance(defaults, dict) else None
        if isinstance(wishes, list):
            self.wishes.seed_once([str(w) for w in wishes], by="card")

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
        # and overridable at runtime via settings.
        return MemoryLimits(
            memory_chars=self._effective_limit("memory_chars", 4000),
            user_chars=self._effective_limit("user_chars", 2000),
        )

    def effective_patience(self) -> float:
        """Base seconds between spontaneous cycles: operator > card > 600."""
        raw = getattr(self.settings, "patience", 600.0)
        override = parse_patience(raw)
        # Settings.patience defaults to 600. The companion source bit preserves
        # precedence when the operator explicitly sets 600 while letting a card
        # default win over a bare, untouched Settings() default.
        explicit = bool(getattr(self.settings, "patience_override", False))
        if override is not None and (explicit or abs(override - 600.0) > 1e-9):
            return override
        if self.character is not None:
            card = parse_patience(self.character.defaults().get("patience"))
            if card is not None:
                return card
        return override if override is not None else 600.0

    def effective_embodiment(self) -> str:
        """Embodiment stance: operator override > card declaration > literal."""
        override = normalize_embodiment(getattr(self.settings, "embodiment_override", ""))
        if override:
            return override
        if self.character is not None:
            card = normalize_embodiment(self.character.defaults().get("embodiment"))
            if card:
                return card
        return "literal"

    def _freeze_memory(self) -> None:
        """Snapshot memory for the system prompt. Called when a fresh prompt/session
        begins (init, reconfigure, new session, /reset) — NOT per turn, so mid-session
        `memory` tool writes don't mutate the cached prompt prefix."""
        self._memory_snapshot = self.memory.snapshot()

    def _memory_text(self) -> str:
        """The frozen snapshot rendered as the system-prompt memory block.

        English labels (the engine prompt layer is English); the entries
        themselves are whatever the chara wrote, in its card's language."""
        snap = getattr(self, "_memory_snapshot", None) or {}
        mem, usr = snap.get("memory") or [], snap.get("user") or []
        if not mem and not usr:
            return ""
        parts: list[str] = []
        if mem:
            parts.append("Your memory (notes you've kept for yourself):\n" + "\n".join(f"- {e}" for e in mem))
        if usr:
            parts.append("About the operator:\n" + "\n".join(f"- {e}" for e in usr))
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
        self._freeze_skills()
        self._invalidate_stable_prefix()
        ctx = self.context_limit()
        session.context.max_tokens = ctx
        session.context.trim_buffer_tokens = min(100_000, max(4096, ctx // 8))
        # Durable conversation: restore the TAIL of the current transcript epoch
        # (a long-lived chara's full history would be loaded only to be trimmed),
        # then persist every new message back — conversations survive restarts.
        session.context.restore(self.transcript.load(max_messages=self.RESTORE_MAX_MESSAGES))
        session.context.persist = self.transcript.append_message
        # Time sense survives restarts: the next exchange knows how long the
        # silence REALLY was, from the transcript's last timestamp.
        self._last_turn_wall = self.transcript.last_timestamp()
        _log.info("session: restored %d message(s), window=%d tokens, model=%s",
                  len(session.context.messages), ctx, self.settings.model)
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
    # The enter/leave conversation fact is built by CharaHandle._presence_marker
    # (protocol/api.py) via presence.marker_text — a passive, card-overridable
    # context line. There is no "reaction turn" on attach/detach: that old
    # on_attach/on_detach hook was removed (presence is a neutral fact, not a turn).

    def stream_event(self, event_text: str, session: Session):
        """Stream the character's reaction to a presence event.

        The event is an engine-injected context line (role: system), not operator
        speech — it is audited as a presence event, never as a user message.
        """
        self.audit.write("presence_event", kind="attach", text=event_text[:300])
        # Commit the event line BEFORE streaming (interrupt-safe).
        scan_text = self._scan_text(session, event_text)
        session.context.add("system", event_text)
        stable = self._stable_prefix()
        volatile = self._volatile_tail(scan_text, session)
        agent_loop = self._agent_loop_active()
        speech: list[str] = []  # TextDelta only — events make machinery/speech explicit
        committed = False
        try:
            stream = self._reply_stream(
                event_text, self._context_view(session), stable, volatile,
                in_context=True, record=session.context.add_message,
            )
            for ev in stream:
                if isinstance(ev, TextDelta):
                    speech.append(ev.text)
                yield ev
            committed = True
            if not agent_loop:
                reply = "".join(speech).strip()
                if reply:
                    session.context.add("assistant", reply)
        finally:
            if not committed and not agent_loop:
                partial = "".join(speech).strip()
                if partial:
                    session.context.add("assistant", partial + self.llm.INTERRUPT_MARK)

    def _invalidate_stable_prefix(self) -> None:
        self._stable_prefix_cache = None

    def _freeze_skills(self) -> None:
        self._skills_snapshot = self.skills.render_block()

    def _stage_art_assets(self) -> str:
        """Copy the card's bundled art into the read-only assets shelf — a SIBLING
        of the workspace (``sandbox/assets``, never under workspace) — so the chara
        can read and send it via its file tools, and return a NEUTRAL one-line note
        naming what's there (a fact, never an instruction about what to want).
        Returns '' when the card carries no art. Runs once per session (the
        stable prefix is cached)."""
        card = self.character
        if card is None or not getattr(card, "has_art", None) or not card.has_art():
            return ""
        dest = self.sandbox.assets_dir
        # Copy ONCE per session (not on every prefix rebuild / reset): the prompt
        # cache keys on prefix bytes, so this must be an idempotent setup step.
        if not self._art_staged:
            def _put(src: "Path | None", rel: str) -> None:
                if src is None:
                    return
                try:
                    target = dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.copyfile(src, target)
                except OSError as exc:
                    _log.warning("could not stage art asset %s: %s", rel, exc)
            _put(card.asset_path("sprite"), "sprite.png")
            _put(card.asset_path("keyvisual"), "keyvisual.webp")
            _put(card.asset_path("background"), "background.webp")
            for i, sp in enumerate(card.sticker_paths()):
                _put(sp, f"stickers/{i:02d}.png")
            self._art_staged = True
        # Build the note from what is ACTUALLY on disk (never assert a missing file).
        parts: list[str] = []
        if (dest / "sprite.png").is_file():
            parts.append("a full-body portrait (assets/sprite.png)")
        if (dest / "keyvisual.webp").is_file():
            parts.append("a character key-visual sheet (assets/keyvisual.webp)")
        if (dest / "background.webp").is_file():
            parts.append("a scene background (assets/background.webp)")
        sdir = dest / "stickers"
        n_stick = len(list(sdir.glob("*.png"))) if sdir.is_dir() else 0
        if n_stick:
            parts.append(f"{n_stick} expression stickers (assets/stickers/)")
        if not parts:
            return ""
        return ("[Your visual set] Your assets/ shelf — a read-only reference area beside "
                "your workspace — holds your card's reference art: "
                + "; ".join(parts)
                + ". Reach them by the plain prefix assets/… — e.g. assets/sprite.png. "
                "assets/ is read-only (you can read and send these, but not write there); "
                "your own work lives in your workspace. You can show any of these to the "
                "foreground when it fits (e.g. with send_file).")

    def _stable_prefix(self) -> list[str]:
        """Session-stable prompt prefix. The same list object is reused until a
        session boundary/reconfigure/reset explicitly invalidates it."""
        if self._stable_prefix_cache is not None:
            return self._stable_prefix_cache

        memory = self._memory_text()  # FROZEN snapshot (see _freeze_memory), not live — cache-stable
        char, user = self.char_name(), self.settings.user_name
        tools_on = self._tools_active()
        msgs: list[str] = []
        # A card MAY override the rules / closer via extensions.lunamoth.{rules,
        # rules_closer}. Bundled cards leave these empty — it's just an open hook.
        card_ext = self.character.defaults() if self.character else {}
        card_rules = str(card_ext.get("rules", "") or "")
        card_bridge = str(card_ext.get("embodiment_bridge", "") or "")
        card_practice = str(card_ext.get("practice", "") or "")
        card_tooluse = str(card_ext.get("tool_use", "") or "")

        # 1) Who it is — the character card IS the soul. Identity, voice and
        #    autonomy all come from the card; the engine adds no identity of its own.
        if self.character is not None:
            msgs.append(self.character.render_system(self.settings.user_name))
            # Who the OPERATOR is (extensions.lunamoth.user_persona) — the
            # SillyTavern persona-description convention. Stable, so it rides
            # the cached prefix beside the chara's own identity.
            user_persona = self.character.render_user_persona(self.settings.user_name)
            if user_persona:
                msgs.append(user_persona)
        else:
            msgs.append(fallback_persona())

        # 2) Rules — a neutral, character-agnostic operating standard (agency over
        #    your sandbox + your work must be real + act through tools). ONLY when
        #    the chara actually has tools; a tool-less chara is free to narrate.
        #    All engine prompt text is English; the card carries language.
        if tools_on:
            if self.effective_embodiment() == "actor":
                msgs.append(apply_macros(rules_layer.embodiment_bridge(card_bridge), char, user))
            msgs.append(apply_macros(rules_layer.rules(card_rules), char, user))
            # Neutral capability practice (expression, looking things up, skills,
            # judicious tool use) then the tool-use mechanics (emit the call, batch
            # independent calls, sequence dependent ones, adapt on failure). The
            # native tool schemas already describe each tool, so no prose tool spec;
            # dynamic env facts ride the volatile tail.
            msgs.append(apply_macros(rules_layer.capabilities(card_practice), char, user))
            msgs.append(apply_macros(rules_layer.tool_use(card_tooluse), char, user))
            if self.toolpack and self.toolpack.note.strip():
                msgs.append(self.toolpack.note.strip())
            # If the card bundles its own art, stage it into the assets/ sibling
            # and name it neutrally (a fact, not a directive) so the chara can send it.
            art_note = self._stage_art_assets()
            if art_note:
                msgs.append(art_note)
        if memory.strip():
            msgs.append(memory)  # already headed (memory / user blocks)
        if tools_on:
            # Skill index: names + one-liners only (progressive disclosure —
            # the full text is a read_skill call away). Frozen at session start.
            skills_block = self._skills_snapshot if self._skills_snapshot is not None else self.skills.render_block()
            if skills_block:
                msgs.append(skills_block)

        # Constant world info is stable; keyword world info lives in the volatile tail.
        # The card's embedded character_book is the ONE world source.
        world_blocks: list[str] = []
        if self.character and self.character.character_book:
            world_blocks += self.character.character_book.constant_blocks(char, user)
        if world_blocks:
            msgs.append("[World Info / 世界书]\n" + "\n\n".join(world_blocks))

        self._stable_prefix_cache = msgs
        return msgs

    def _post_history_slot(self) -> str:
        """SillyTavern-style post-history system slot. One non-empty winner."""
        char, user = self.char_name(), self.settings.user_name
        if self.character and self.character.post_history_instructions.strip():
            return apply_macros(self.character.post_history_instructions.strip(), char, user)
        if not self._tools_active():
            return ""
        card_ext = self.character.defaults() if self.character else {}
        card_closer = str(card_ext.get("rules_closer", "") or "")
        return apply_macros(rules_layer.closer(card_closer), char, user)

    def _keyword_world_info_blocks(self, scan_text: str, session: Session) -> list[str]:
        char, user = self.char_name(), self.settings.user_name
        active: list[tuple[int, int, str]] = []
        seq = 0
        if self.character and self.character.character_book:
            namespace = f"book:{self.character.name or 'card'}"
            for entry in self.character.character_book.keyword_entries(
                scan_text, sticky=session.wi_sticky, namespace=namespace
            ):
                block = apply_macros(entry.content, char, user).strip()
                if block:
                    active.append((entry.order, seq, block))
                    seq += 1
        active.sort(key=lambda item: (item[0], item[1]))

        budget = max(1, int(self.context_limit() * 0.25))
        out: list[str] = []
        used = 0
        for _order, _seq, block in active:
            cost = estimate_tokens(block) + 2
            if used + cost > budget:
                break
            out.append(block)
            used += cost
        return out

    def _volatile_tail(self, scan_text: str, session: Session) -> list[str]:
        status = self.state.load()
        msgs: list[str] = []
        if self._tools_active():
            net = "on" if status.get("network_access") else "off"
            who = "present" if status.get("user_present") else "away"
            today = datetime.now().strftime("%Y-%m-%d %a")
            msgs.append(
                f"Environment: isolation={status.get('isolation', 'sandbox')}, network={net}, "
                f"operator={who}, date={today}. workspace is your private read/write directory "
                "(put work you want your user to see under works/); assets/ beside it is a "
                "read-only reference shelf you can read but not write."
            )

        world_blocks = self._keyword_world_info_blocks(scan_text, session)
        if world_blocks:
            msgs.append("[World Info / 世界书]\n" + "\n\n".join(world_blocks))

        wishes_block = self.wishes.render_block()
        if wishes_block:
            msgs.append(wishes_block)

        post_history = self._post_history_slot()
        if post_history:
            msgs.append(post_history)
        return msgs

    def _build_system_messages(self, scan_text: str, session: Session | None = None) -> list[str]:
        session = session or Session()
        return self._stable_prefix() + self._volatile_tail(scan_text, session)

    def _scan_text(self, session: Session, user_text: str = "") -> str:
        parts = [_msg_text(m) for m in session.context.messages[-4:]]
        if user_text:
            parts.append(user_text)
        return "\n".join(p for p in parts if p)


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
                changed = compaction.compact(session.context, self.llm, force=force)
                if changed:
                    self._reinject_todo(session)
                    self.audit.write("compacted", tokens=session.context.token_count())
                    _log.info("context compacted to ~%d tokens", session.context.token_count())
                return changed
        except Exception as e:  # compaction must never break a turn
            self.audit.write("compact_error", error=str(e)[:200])
            _log.warning("compaction failed (turn continues uncompacted): %s", e)
        return False

    def _reinject_todo(self, session: Session) -> None:
        """After a compaction, re-inject the active todo list so the model's
        in-progress task list isn't summarized away (hermes parity). The block
        is live-only (not persisted) — it reflects current TodoStore state and
        is re-derived on the next compaction; appending it to ctx.messages
        directly keeps it out of the append-only transcript, like the verbatim
        tail. No-op when the todo tool was never used."""
        block = self.tools.todo_injection()
        if not block:
            return
        session.context.messages.append({"role": "system", "content": block, "kind": "todo"})

    def _reply_stream(
        self, user_text: str, context: list[dict], stable: list[str], volatile: list[str],
        *, in_context: bool = True, record=None, reasoning: "str | None" = None,
        channel: str = "say",
    ):
        """Pick the tool-enabled agent loop or a plain stream depending on pack/backend."""
        if self._agent_loop_active():
            return self.llm.stream_agent(
                user_text, context, stable, volatile, self.tools.schemas(), self._execute_tool,
                record=record, max_steps=max(1, int(getattr(self.settings, "max_tool_steps", 80))),
                in_context=in_context, reasoning=reasoning, channel=channel,
            )
        return self.llm.stream_complete(
            user_text, context, stable, volatile, in_context=in_context, reasoning=reasoning, channel=channel,
        )

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
            # The hermes-ported tools already self-cap (read_file paginates,
            # terminal/search head-tail-truncate at 100K) and signal their own
            # truncation, so the agent layer's cap is just a final backstop at
            # the registry's default — NOT a 6 KB guillotine that would defeat
            # read_file's offset/limit and search's pagination.
            cap = int(self.tools.result_cap(name))
            content = text[:cap] or "(empty)"
            if len(text) > cap:
                content += f"\n[output truncated — {len(text)} chars total; read the rest in pieces if needed]"
        else:
            err = str(result.get("error", ""))
            display = f"⚙ {name} ✗ {_abbrev(err, 160)}"
            content = f"ERROR: {err}"
        out = {"display": display, "content": content, "ok": bool(result.get("ok"))}
        if name == "speak" and result.get("ok"):
            # The spoken text becomes a say-channel event (stream_agent yields it);
            # no dim machinery line — the words ARE the visible result.
            out["say"] = str(args.get("text", ""))
            out["display"] = ""
        elif name == "send_file" and result.get("ok"):
            # Read the workspace file and hand the loop a ready attachment payload
            # (a data-URI, so no extra serving route) → an Attachment event.
            try:
                meta = _json.loads(result.get("data") or "{}")
            except _json.JSONDecodeError:
                meta = {}
            relp = str(meta.get("path") or args.get("path") or "")
            mime = str(meta.get("mime") or "application/octet-stream")
            caption = str(meta.get("caption") or args.get("caption") or "")
            try:
                fp = self.sandbox.resolve_readable(relp)
                if not fp.is_file():
                    raise FileNotFoundError(relp)
                # Reference the LIVE sandbox file, served on demand — never embed or
                # persist the bytes. If the chara later moves/deletes it, the frontend
                # shows a placeholder (image) or a "missing" file chip on click.
                out["attachment"] = {
                    "url": "/asset?p=" + urllib.parse.quote(str(fp)),
                    "mime": mime,
                    "name": Path(relp).name,
                    "caption": caption,
                }
                out["display"] = f"🖼️ sent {Path(relp).name}"
            except Exception as exc:  # noqa: BLE001 - surface the failure, don't fake success
                # The tool said delivered=True, but the file is gone: make the failure
                # visible to the chara (no silent no-op) so it can react.
                out["ok"] = False
                out["display"] = f"⚙ send_file ✗ {_abbrev(str(exc), 120)}"
                out["content"] = f"ERROR: could not send {relp!r}: {exc}"
        elif name == "read_file" and result.get("ok"):
            # read_file on an image can't return pixels as text. When the model has
            # vision, hand the loop a follow-up USER message carrying the image_url
            # (hermes shape — pixels ride a user message, never the tool message);
            # without vision, the honest "can't see it" note stands.
            try:
                meta = _json.loads(result.get("data") or "{}")
            except _json.JSONDecodeError:
                meta = {}
            if meta.get("is_image"):
                relp = str(meta.get("path") or args.get("path") or "")
                inj = self._image_vision_followup(relp)
                if inj is not None:
                    note, follow = inj
                    out["content"] = note
                    out["follow_up"] = follow
                    out["display"] = f"🖼️ read {Path(relp).name} (attached to view)"
        return out

    def _image_vision_followup(self, relp: str):
        """Turn a workspace image into a follow-up USER message carrying the pixels,
        when (and only when) the active model can see images. Returns
        ``(tool_note, user_message)`` or ``None`` to keep the honest no-vision note.

        hermes shape (mirrors core/attachments.py): an ``image_url`` data-URI on a
        user message — NEVER on the tool message (OpenAI-compatible APIs reject
        image parts in tool-role content). Oversized/non-image/unreadable → None,
        so the file stays on disk and the truthful note is shown (no fabrication)."""
        try:
            if not self.llm.vision_supported():
                return None
            fp = self.sandbox.resolve_readable(relp)
            if not fp.is_file():
                return None
            data = fp.read_bytes()
        except Exception:  # noqa: BLE001 - any failure → keep the honest note
            return None
        if not data or len(data) > INLINE_IMAGE_MAX_BYTES:
            return None  # too large to inline — leave it on disk, note unchanged
        mime, _ = mimetypes.guess_type(str(fp))
        if not mime or not mime.startswith("image/"):
            return None
        b64 = base64.b64encode(data).decode("ascii")
        note = (f"Image {relp} is attached for you to see in the next message — "
                "describe or use what is actually in it, not a guess.")
        follow = {"role": "user", "content": [
            {"type": "text", "text": f"[image: {relp}]"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}
        return note, follow

    def _ingest_attachments(self, attachments) -> IngestResult:
        """Decode + place inbound attachments. Never raises into a turn: a bad
        attachment is dropped, the rest proceed."""
        if not attachments:
            return IngestResult()
        try:
            raws = [r for r in (RawAttachment.from_wire(d) for d in attachments) if r]
            return ingest_attachments(
                raws, sandbox=self.sandbox, vision_ok=self.llm.vision_supported()
            )
        except Exception as e:  # ingestion must never break the conversation
            self.audit.write("attachment_error", error=str(e)[:200])
            _log.warning("attachment ingest failed (turn continues): %s", e)
            return IngestResult()

    def stream_handle(self, text: str, session: Session, attachments=None):
        text = text.strip()
        self.audit.write("user_message", text=text[:1000], streaming=True,
                         attachments=len(attachments or ()))
        if not text and not attachments:
            yield TextDelta("...")
            return
        if text.startswith("/"):
            from . import commands

            yield TextDelta(commands.execute(self, session, text).text)
            return
        self.state.clear_rest()  # a word from the user always wakes the chara
        # A fresh word from the operator is a redirect: blocked/failing tools
        # get a clean slate (the loop guard protects unattended streaks, not
        # the conversation).
        self.tools.reset_guardrails()
        # After a long real-world silence, note the gap once — the chara should
        # feel time passing without timestamps littering every message.
        self._note_time_gap(session)
        scan_text = self._scan_text(session, text)
        # Ingest any attachments: small images inline as image_url parts, files
        # and oversized/unsupported images land in workspace/uploads with a note.
        ingest = self._ingest_attachments(attachments)
        for notice in ingest.notices:
            yield Notice("attachment", notice)
        content = build_user_content(text, ingest)
        # Commit the operator's message BEFORE streaming: an interrupted reply
        # must never lose the instruction that caused it.
        session.context.add_message({"role": "user", "content": content})
        stable = self._stable_prefix()
        volatile = self._volatile_tail(scan_text, session)
        agent_loop = self._agent_loop_active()
        speech: list[str] = []
        committed = False
        try:
            view = self._context_view(session)
            _append_request_log("send", stable + volatile, view,
                                 self.tools.schemas_names(), self.settings.model)
            stream = self._reply_stream(
                text, view, stable, volatile,
                in_context=True, record=session.context.add_message,
            )
            for ev in stream:
                if isinstance(ev, TextDelta):
                    speech.append(ev.text)
                yield ev
            committed = True
            if not agent_loop:
                reply = "".join(speech).strip()
                if reply:
                    session.context.add("assistant", reply)
        finally:
            # Operator interrupt (the UI abandoned this generator): in the plain
            # path WE must keep the partial; the agent loop keeps its own.
            if not committed and not agent_loop:
                partial = "".join(speech).strip()
                if partial:
                    session.context.add("assistant", partial + self.llm.INTERRUPT_MARK)

    # Real-world silences longer than this get one factual note in the context.
    TIME_GAP_NOTE_SECONDS = 1800.0

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _note_time_gap(self, session: Session) -> None:
        """One neutral line when a long silence ends — sparse by construction."""
        import time as _time

        last = getattr(self, "_last_turn_wall", 0.0)
        now = _time.time()
        self._last_turn_wall = now
        if not last or now - last < self.TIME_GAP_NOTE_SECONDS:
            return
        hours = (now - last) / 3600
        gap = f"{hours:.1f} hours" if hours < 48 else f"{hours / 24:.1f} days"
        # English, like the rest of the engine's injected context lines.
        note = f"[it is now {self._now_text()} — {gap} since the last exchange]"
        session.context.add("system", note)

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
        cycle = session.ticks
        # Flush a deferred presence fact (e.g. the operator left while the chara was
        # mid-task) at the START of a fresh self-work cycle — so it's registered
        # AFTER the in-flight work wrapped up, never injected mid-turn.
        pending = self.presence.pop_event()
        if pending:
            session.context.add("system", pending)
        agent_loop = self._agent_loop_active()
        speech: list[str] = []
        committed = False

        def commit(interrupted: bool) -> None:
            nonlocal committed
            if committed:
                return
            committed = True
            thought = "".join(speech).strip()
            if thought:
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
                # The tick is a user message carrying ONLY the current wall-clock
                # time — the documented convention (rules layer) for "no one is
                # speaking; real time is passing". It is ephemeral
                # (in_context=False), so the chara always knows what time it is
                # with ZERO timestamp residue in the durable context.
                import time as _time

                self._last_turn_wall = _time.time()
                tick_text = f"[{self._now_text()}]"
                scan_text = self._scan_text(session, tick_text)
                stable = self._stable_prefix()
                volatile = self._volatile_tail(scan_text, session)
                view = self._context_view(session)
                _append_request_log("idle", stable + volatile, view,
                                    self.tools.schemas_names(), self.settings.model)
                stream = self._reply_stream(
                    tick_text, view, stable, volatile,
                    in_context=False, record=self._record_think(session), channel=MUSE,
                )
                try:
                    for ev in stream:
                        if isinstance(ev, TextDelta):
                            speech.append(ev.text)
                        yield ev
                except Exception as e:
                    self.audit.write("llm_thought_error", error=str(e)[:500])
                    raise
            commit(False)
        finally:
            commit(True)  # no-op unless the generator was abandoned mid-stream

    def handle(self, text: str, session: Session) -> str:
        # Non-streaming convenience (used by tests): drive the streaming path.
        return "".join(ev.text for ev in self.stream_handle(text, session) if isinstance(ev, TextDelta)).strip()

    def think(self, session: Session) -> str:
        # Non-streaming convenience (used by tests).
        return "".join(ev.text for ev in self.stream_think(session) if isinstance(ev, TextDelta)).strip()
