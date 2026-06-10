from __future__ import annotations

import argparse
import asyncio
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Select, Static

# Console commands, used both for inline autocomplete (ghost text) and the visible
# suggestion line under the input.
SLASH_COMMANDS = [
    "/help", "/status", "/memory", "/memory_path", "/files", "/workspace",
    "/read", "/wread", "/write", "/logs", "/reset",
    "/mode live", "/mode chat", "/patience", "/theme", "/net on", "/net off",
    "/allow-dir", "/settings", "/clear", "/exit",
]

from . import art
from .agent import LunaMothAgent
from .cleanup import clean_runtime_sandbox
from .config import ROOT
from .llm import DIM_OFF, DIM_ON, LLMClient
from .presence import MODES, normalize_mode
from .settings import PRESETS, Settings, config_path, load_settings, save_settings
from .themes import TuiTheme, load_theme

# Splits streamed text on the in-band dim markers (see llm.py).
_DIM_SPLIT = re.compile("(\x01|\x02)")


def _st_dir() -> Path | None:
    # External scanning is OPT-IN. By default we only look inside the project folder
    # (no links outside it). Set LUNAMOTH_ST_DIR to also scan a SillyTavern data dir.
    d = os.getenv("LUNAMOTH_ST_DIR", os.getenv("LUNAMOSS_ST_DIR", "")).strip()
    if not d:
        return None
    p = Path(d).expanduser()
    return p if p.exists() else None


def _discover(subdir: str, suffixes: tuple[str, ...]) -> list[tuple[str, str]]:
    """Find persona files in the project dir (and an opt-in external SillyTavern dir)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    bases = [(ROOT / subdir, "")]
    st = _st_dir()
    if st:
        bases.append((st / subdir, " [ST]"))
    for base, tag in bases:
        if not base.exists():
            continue
        for p in sorted(base.iterdir()):
            if p.is_file() and p.suffix.lower() in suffixes and not p.name.startswith("."):
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                out.append((p.stem + tag, rp))
    return out


def _picker_options(discovered: list[tuple[str, str]], current: str, blank_label: str) -> list[tuple[str, str]]:
    options = [(blank_label, "")] + discovered
    cur = (current or "").strip()
    if cur and cur not in {v for _, v in options}:
        options.append((Path(cur).name + " (configured)", cur))
    return options


@dataclass
class StreamJob:
    kind: str
    text: str | None = None


def _preset_for(settings: Settings) -> str:
    for name, preset in PRESETS.items():
        if preset.get("provider") == settings.provider and preset.get("base_url", "") == settings.base_url:
            return name
    return "Custom"


class WelcomeScreen(Screen):
    """Welcome / boot screen: pick a provider, set the API, then enter.

    Dismisses with the chosen Settings, or None if the operator backs out.
    """

    CSS = """
    WelcomeScreen {
        align: center middle;
        background: #050505;
    }
    #welcome {
        width: 84;
        height: auto;
        max-height: 90%;
        border: heavy #2f5468;
        background: #0a0a0a;
        padding: 1 2;
    }
    #banner {
        width: auto;
        content-align: center middle;
    }
    #title {
        color: #9fd9ff;
        text-style: bold italic;
        margin-top: 1;
        content-align: center middle;
        width: 100%;
    }
    #lore {
        color: #888888;
        margin-bottom: 1;
    }
    .field-label {
        color: #6db8e8;
        margin-top: 1;
    }
    #conn_status {
        margin-top: 1;
        height: auto;
        color: #d8d8d8;
    }
    #welcome-buttons {
        height: auto;
        margin-top: 1;
    }
    #welcome-buttons Button {
        margin-right: 2;
    }
    Input { background: #050505; }
    """

    def __init__(self, settings: Settings, mid_session: bool = False):
        super().__init__()
        self.draft = replace(settings)
        self.mid_session = mid_session
        self.skin = load_theme(settings.tui_theme_path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="welcome"):
            yield Static(art.wordmark(compact=True), id="banner")
            yield Static(self.skin.subtitle, id="title")
            yield Static(
                "Pick a model and a character, then enter. Choosing a character fills in its "
                "world / tools / limits — change them below if you like. Language follows the card.",
                id="lore",
            )
            yield Label("Provider preset", classes="field-label")
            options = [(name, name) for name in PRESETS] + [("Custom", "Custom")]
            yield Select(options, value=_preset_for(self.draft), allow_blank=False, id="preset")
            yield Label("Base URL", classes="field-label")
            yield Input(self.draft.base_url, placeholder="https://openrouter.ai/api/v1", id="base_url")
            yield Label("API key", classes="field-label")
            yield Input(self.draft.api_key, placeholder="sk-or-... (blank for local/mock)", password=True, id="api_key")
            yield Label("Model", classes="field-label")
            yield Input(self.draft.model, placeholder="meta-llama/llama-3.3-70b-instruct", id="model")
            with Horizontal():
                with Vertical():
                    yield Label("Temperature", classes="field-label")
                    yield Input(str(self.draft.temperature), id="temperature")
                with Vertical():
                    yield Label("Max tokens", classes="field-label")
                    yield Input(str(self.draft.max_tokens), id="max_tokens")
            chars = _discover("characters", (".png", ".json"))
            worlds = _discover("worlds", (".json",))
            yield Label("Character card (persona)", classes="field-label")
            yield Select(
                _picker_options(chars, self.draft.character_path, "(default · LunaMoth 月蛾)"),
                value=self.draft.character_path or "",
                allow_blank=False,
                id="character",
            )
            yield Label("World book (optional)", classes="field-label")
            yield Select(
                _picker_options(worlds, self.draft.world_path, "(auto · pairs with default character)"),
                value=self.draft.world_path or "",
                allow_blank=False,
                id="world",
            )
            yield Label("Tool pack (capabilities)", classes="field-label")
            packs = _discover("toolpacks", (".json",))
            pack_options = [("(none / pure roleplay)", "")] + [(stem, stem) for stem, _ in packs]
            cur_pack = (self.draft.toolpack or "").strip()
            if cur_pack and cur_pack not in {v for _, v in pack_options}:
                pack_options.append((cur_pack, cur_pack))
            yield Select(pack_options, value=self.draft.toolpack or "", allow_blank=False, id="toolpack")
            with Horizontal():
                with Vertical():
                    yield Label("Overdrive: context tokens (0=auto)", classes="field-label")
                    yield Input(str(self.draft.context_tokens), id="context_tokens")
                with Vertical():
                    yield Label("Overdrive: memory chars (0=auto)", classes="field-label")
                    yield Input(str(self.draft.memory_chars), id="memory_chars")
            themes = _discover("themes", (".json",))
            yield Label("TUI theme (cosmetic skin)", classes="field-label")
            yield Select(
                _picker_options(themes, self.draft.tui_theme_path, "(default · LunaMoth 月蛾)"),
                value=self.draft.tui_theme_path or "",
                allow_blank=False,
                id="theme",
            )
            yield Label("Your name ({{user}})", classes="field-label")
            yield Input(self.draft.user_name, id="user_name")
            yield Static("", id="conn_status")
            with Horizontal(id="welcome-buttons"):
                yield Button("Test connection", id="test", variant="primary")
                enter_label = "Apply & resume" if self.mid_session else "Enter"
                yield Button(enter_label, id="enter", variant="success")
                if self.mid_session:
                    yield Button("Cancel", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self._paint_theme(self.skin)
        self.query_one("#base_url", Input).focus()

    def _paint_theme(self, t: TuiTheme) -> None:
        self.query_one("#welcome").styles.border = ("heavy", t.display_border)
        self.query_one("#banner", Static).styles.color = t.display_title_color
        self.query_one("#title", Static).styles.color = t.accent

    def _collect(self) -> Settings:
        def _txt(wid: str) -> str:
            return self.query_one(f"#{wid}", Input).value.strip()

        try:
            temperature = float(_txt("temperature"))
        except ValueError:
            temperature = self.draft.temperature
        try:
            max_tokens = int(_txt("max_tokens"))
        except ValueError:
            max_tokens = self.draft.max_tokens
        try:
            context_tokens = int(_txt("context_tokens"))
        except ValueError:
            context_tokens = self.draft.context_tokens
        try:
            memory_chars = int(_txt("memory_chars"))
        except ValueError:
            memory_chars = self.draft.memory_chars
        character = self.query_one("#character", Select).value
        world = self.query_one("#world", Select).value
        theme = self.query_one("#theme", Select).value
        toolpack = self.query_one("#toolpack", Select).value
        self.draft = replace(
            self.draft,
            base_url=_txt("base_url"),
            api_key=_txt("api_key"),
            model=_txt("model"),
            temperature=temperature,
            max_tokens=max_tokens,
            context_tokens=context_tokens,
            memory_chars=memory_chars,
            character_path=character if isinstance(character, str) else "",
            world_path=world if isinstance(world, str) else "",
            tui_theme_path=theme if isinstance(theme, str) else "",
            toolpack=toolpack if isinstance(toolpack, str) else "",
            user_name=_txt("user_name") or self.draft.user_name,
        )
        return self.draft

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "theme":
            # Live-preview the chosen skin's banner/colors right in the welcome screen.
            path = event.value if isinstance(event.value, str) else ""
            self.skin = load_theme(path)
            self.query_one("#title", Static).update(self.skin.subtitle)
            self._paint_theme(self.skin)
            return
        if event.select.id == "character":
            self._prefill_from_character(event.value if isinstance(event.value, str) else "")
            return
        if event.select.id != "preset":
            return
        name = event.value
        if name == "Custom" or name not in PRESETS:
            # Custom: keep current fields, but ensure a live provider so a pasted endpoint works.
            if self.draft.provider == "mock":
                self.draft = replace(self.draft, provider="openai_compatible")
            return
        preset = PRESETS[name]
        self.draft = replace(
            self.draft,
            provider=preset.get("provider", self.draft.provider),
            base_url=preset.get("base_url", ""),
            api_key=preset.get("api_key", self.draft.api_key),
            model=preset.get("model", self.draft.model),
        )
        self.query_one("#base_url", Input).value = self.draft.base_url
        self.query_one("#api_key", Input).value = self.draft.api_key
        self.query_one("#model", Input).value = self.draft.model

    def _prefill_from_character(self, char_path: str) -> None:
        """Pick a character → pre-fill its declared world / tool pack / limits.

        The fields stay editable, so this is "here are the card's defaults, change
        them if you want" rather than a hard binding. Empty path = bundled default.
        """
        from .cards import CharacterCard
        from .persona import default_character_path, default_world_path

        path = char_path or (str(default_character_path() or ""))
        if not path:
            return
        try:
            card = CharacterCard.load(path)
        except Exception:
            return
        defaults = card.defaults()
        # World: card's declared default, else same-language bundled world for the default char.
        world = str(defaults.get("world", ""))
        if world and not Path(world).is_absolute():
            world = str(ROOT / world)
        if not world and not char_path:
            dw = default_world_path(card.language)
            world = str(dw) if dw else ""
        self._set_select("#world", world)
        self._set_select("#toolpack", str(defaults.get("toolpack", "") or ""))
        if defaults.get("context_tokens"):
            self.query_one("#context_tokens", Input).value = str(int(defaults["context_tokens"]))
        if defaults.get("memory_chars"):
            self.query_one("#memory_chars", Input).value = str(int(defaults["memory_chars"]))
        lang_label = "中文" if card.language == "zh" else "English"
        self.query_one("#conn_status", Static).update(
            f"[#9fd9ff]Loaded {card.name}'s defaults · language: {lang_label}. Adjust below if you like.[/]"
        )

    def _set_select(self, selector: str, value: str) -> None:
        """Set a Select to value; silently ignore if it isn't an available option.

        Bundled cards reference worlds/toolpacks that are already in the scanned
        lists, so this just works. Exotic imported cards can be set manually.
        """
        sel = self.query_one(selector, Select)
        try:
            sel.value = value or ""  # "" is the explicit blank option on these selects
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "test":
            self._run_test()
        elif event.button.id == "enter":
            self.dismiss(self._collect())
        elif event.button.id == "cancel":
            self.dismiss(None)

    def _run_test(self) -> None:
        settings = self._collect()
        status = self.query_one("#conn_status", Static)
        if not settings.is_live():
            status.update("[#ffaa00]Offline/mock provider — nothing to test. Just enter.[/]")
            return
        status.update("[#888888]Testing connection…[/]")

        def work() -> None:
            client = LLMClient(settings.to_llm_config())
            ok, msg = client.test_connection()
            self.app.call_from_thread(self._show_result, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _show_result(self, ok: bool, msg: str) -> None:
        color = "#00ff66" if ok else "#ff4040"
        mark = "✓" if ok else "✗"
        self.query_one("#conn_status", Static).update(f"[{color}]{mark} {msg}[/]")


class LunaMothTUI(App):
    CSS = """
    Screen {
        background: #050505;
    }
    /* Left 3/4 column (character display + console) | right 1/4 telemetry sidebar. */
    #body {
        height: 1fr;
    }
    #main {
        width: 3fr;
    }
    /* Top 2/3: pure persona output. A plain SCROLL container with a Static inside —
       NOT a TextArea. TextArea is an editor (it has a caret that floats up and it
       full-reloads on every token, which caused the violent shaking). A Static has no
       cursor and Textual's compositor only redraws changed cells, so growth is flicker-free. */
    #display {
        height: 2fr;
        border: heavy #2f5468;
        border-title-color: #9fd9ff;
        border-title-style: bold;
        background: #050505;
        scrollbar-size-vertical: 1;
    }
    #transcript {
        height: auto;
        width: 1fr;
        color: #cfcfcf;
        padding: 0 1;
    }
    /* Bottom 1/3: operator console — your input, commands and system notices. */
    #bottom {
        height: 1fr;
        border: heavy #303030;
        border-title-color: #00ff66;
        background: #080808;
    }
    #status {
        height: 1;
        color: #00ff66;
        background: #101010;
    }
    #console {
        height: 1fr;
        background: #080808;
        color: #c8c8c8;
    }
    #suggest {
        height: auto;
        color: #6a6a6a;
        background: #080808;
        padding: 0 1;
    }
    #input {
        background: #050505;
    }
    /* Right 1/4: live telemetry — context/memory/sandbox gauges + the memory doc. */
    #sidebar {
        width: 1fr;
        border: heavy #1f3a1f;
        border-title-color: #00ff66;
        border-title-style: bold;
        background: #060a06;
        color: #cfe6cf;
        padding: 0 1;
    }
    #gauges {
        height: auto;
        margin-bottom: 1;
    }
    #memview {
        height: auto;
        color: #8fae8f;
    }
    """

    # Slash-command driven (like Claude Code / Codex): the only key binding is Ctrl+C to
    # shut down cleanly. Every feature lives behind a /command in the console below.
    BINDINGS = [
        ("ctrl+c", "quit_clean", "Shutdown"),
    ]

    # Spontaneous-cycle activity words, shown in the status line while a self-talk
    # stream runs (replies always show as "talking"; idle shows "waiting").
    ACTIVITIES = ("working", "thinking", "musing", "tinkering", "dreaming")

    def __init__(self, patience: float = 2.0, clean_on_exit: bool = False, mode_override: str = ""):
        super().__init__()
        # `patience` = how long the chara waits after a turn before its next
        # spontaneous cycle (live mode). This is pacing, not model reasoning.
        self.patience = patience
        self.clean_on_exit = clean_on_exit
        self.settings = load_settings()
        # Interaction mode (live = it keeps living while you watch; chat = it
        # attends to you only). Per-chara persisted; a CLI flag may override.
        self.mode = normalize_mode(mode_override or self.settings.mode)
        self.skin = load_theme(self.settings.tui_theme_path)
        self.agent = LunaMothAgent(self.settings)
        self.session = self.agent.make_session()
        self.output: queue.Queue[tuple[str, str]] = queue.Queue()
        self.current_thread: threading.Thread | None = None
        self.interrupt_event = threading.Event()
        self.worker_lock = threading.Lock()
        self.shutdown_requested = False
        self.display_segments: list[tuple[str, str]] = []  # (style, text); "dim" = machinery
        self.next_spont_at = time.monotonic() + 0.2
        # Attach grace: after the arrival greeting the chara leaves you room for
        # the first word; if you stay silent past this it returns to its work.
        self.grace_until = 0.0
        self._activity = "waiting"
        self._session_started = False
        # Operator messages are QUEUED, never dropped: with a live provider a think
        # cycle is almost always streaming, so starting a stream synchronously on submit
        # would silently fail. The pump (scheduler + submit) drains this with priority.
        self.pending_input: str | None = None
        self._detached = False
        # Pending request_permission call: the worker thread blocks on _perm_event
        # while the operator answers (y/n) in the console; timeout = deny.
        self._perm_pending: str | None = None
        self._perm_answer = False
        self._perm_event = threading.Event()
        self.agent.tools.permission_hook = self._permission_request

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                # Top: the persona's terminal — pure display. A scroll container holding a
                # Static (no caret, no editing; mouse-wheel/scrollbar to read back).
                with VerticalScroll(id="display"):
                    yield Static("", id="transcript")
                # Bottom: the operator console. Everything you type + every system notice.
                with Vertical(id="bottom"):
                    yield Static("", id="status")
                    yield RichLog(id="console", wrap=True, auto_scroll=True, markup=False)
                    yield Static("", id="suggest")
                    yield Input(
                        placeholder="operator console — talk to your character, or /help",
                        id="input",
                        suggester=SuggestFromList(SLASH_COMMANDS, case_sensitive=False),
                    )
            # Right 1/4: live telemetry + memory document.
            with VerticalScroll(id="sidebar"):
                yield Static("", id="gauges")
                yield Static("", id="memview")
        yield Footer()

    def on_mount(self) -> None:
        self.display_scroll = self.query_one("#display", VerticalScroll)
        self.transcript = self.query_one("#transcript", Static)
        self.console_log = self.query_one("#console", RichLog)
        self.status = self.query_one("#status", Static)
        self.input = self.query_one("#input", Input)
        self.suggest = self.query_one("#suggest", Static)
        self.gauges = self.query_one("#gauges", Static)
        self.memview = self.query_one("#memview", Static)
        # Display is read-only: it must never grab keyboard focus (so the caret stays in
        # the input below). Mouse wheel still scrolls a non-focusable scroll view.
        self.display_scroll.can_focus = False
        self._display_dirty = False
        self._ws_cache = (0.0, 0, 0)  # (monotonic_ts, bytes, files) — throttle disk walk
        self._apply_theme()
        self._write_banner()
        # Hermes-style boot: if this session is already configured (setup wizard
        # or a previous run), drop straight into the three-card layout; the
        # welcome/settings screen stays one /settings away. First boot still gets it.
        if config_path().exists():
            self._welcome_done(None)
        else:
            self.push_screen(WelcomeScreen(self.settings), self._welcome_done)

    def _apply_theme(self) -> None:
        """Paint the current theme card onto the fixed layout (borders/titles/colors)."""
        t = self.skin
        da = self.display_scroll
        da.styles.border = ("heavy", t.display_border)
        da.styles.border_title_color = t.display_title_color
        da.border_title = t.display_title
        self.transcript.styles.color = t.display_fg
        bottom = self.query_one("#bottom", Vertical)
        bottom.styles.border = ("heavy", t.console_border)
        bottom.styles.border_title_color = t.accent
        bottom.border_title = t.console_title
        self.status.styles.color = t.accent
        sb = self.query_one("#sidebar", VerticalScroll)
        sb.styles.border = ("heavy", t.sidebar_border)
        sb.styles.border_title_color = t.accent
        sb.border_title = t.sidebar_title

    def _welcome_done(self, result: Settings | None) -> None:
        if result is not None:
            theme_changed = result.tui_theme_path != self.settings.tui_theme_path
            self.settings = result
            save_settings(result)
            self.agent.reconfigure(result)
            self.mode = normalize_mode(result.mode)
            # Re-apply the (possibly new) context limit to the live session.
            self.session.context.max_tokens = self.agent.context_limit()
            self.skin = load_theme(result.tui_theme_path)
            self._apply_theme()
            if theme_changed:
                # Old skin's decorative lines linger in the console log — reset so the
                # console reflects the newly selected theme (fixes the "still cute" leak).
                self.console_log.clear()
                self._write_banner()
            persona = self.agent.char_name()
            self._console(
                f"online · persona={persona} · theme={self.skin.name} · "
                f"provider={result.provider} · model={result.model} · ctx={self.agent.context_limit()}",
                "grey50",
            )
        if not self._session_started:
            self._begin_session()
        else:
            self._update_status()
            self.input.focus()

    def _begin_session(self) -> None:
        self._session_started = True
        self.input.focus()
        self.set_interval(0.03, self._drain_output)
        self.set_interval(0.06, self._flush_display)  # ~16fps repaint of the top pane
        self.set_interval(0.1, self._scheduler_tick)
        self.next_spont_at = time.monotonic() + 0.2
        # Presence: the chara now has an operator attached.
        self.agent.state.set_present(True)
        self.agent.presence.pop_event()  # discard any stale handoff line — we're here now
        # Attach grace (live mode): after greeting, leave the operator room for the
        # first word; if they stay silent the chara simply returns to its work.
        self.grace_until = time.monotonic() + max(30.0, 2 * self.patience)
        self._update_status()
        name = self.agent.char_name()
        restored = bool(self.session.context.messages)
        self._render_restored_tail(name)
        greeting = self.agent.greeting()
        first = self.agent.presence.first_meeting() and not restored
        enter = self.agent.attach_event_text()
        self.agent.presence.mark_met()
        if greeting and first:
            # SillyTavern first_mes: the card's designed opener for a first meeting.
            self._append_display(f"{self.skin.reply_pfx(name)}{greeting}\n")
            self.session.context.add("assistant", greeting)
            self.next_spont_at = time.monotonic() + self.patience
        elif enter:
            # The card's on_attach prompt: a live arrival turn — the chara reacts
            # to the operator coming back.
            self._start_stream(StreamJob(kind="event", text=enter), prefix=self.skin.reply_pfx(name))
        elif greeting and not restored:
            # Card without an arrival prompt, fresh session: fall back to first_mes.
            self._append_display(f"{self.skin.reply_pfx(name)}{greeting}\n")
            self.session.context.add("assistant", greeting)
            self.next_spont_at = time.monotonic() + self.patience
        elif not restored:
            probe = "你是谁？只用一句话回答。" if self.agent.lang == "zh" else "Who are you? Answer in one sentence."
            self._start_stream(StreamJob(kind="user", text=probe), prefix=self.skin.reply_pfx(name))
        # Restored history with no arrival prompt: continue silently — the
        # restored tail above already says where things left off.

    def _render_restored_tail(self, name: str, max_lines: int = 8) -> None:
        """Show the tail of the restored transcript so the operator sees what
        happened while they were away (the full history is on disk)."""
        rows = self.session.context.messages
        if not rows:
            return
        # Tool plumbing is noise in a recap — show prose plus a compact tool mark.
        visible = [m for m in rows if not m.get("tool_call_id")]
        tail = visible[-max_lines:]
        if len(visible) > len(tail):
            self._append_display(f"··· {len(visible) - len(tail)} earlier messages (restored) ···\n\n", style="dim")
        for msg in tail:
            role = msg.get("role", "")
            content = str(msg.get("content") or "")
            if msg.get("tool_calls"):
                names = ", ".join(tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"])
                if content:
                    self._append_display(f"{self.skin.reply_pfx(name)}{content}\n")
                self._append_display(f"⚙ ({names})\n\n", style="dim")
                continue
            if not content:
                continue
            if role == "user":
                pfx = self.skin.operator_pfx(self.settings.user_name)
            elif role == "system":
                self._append_display(f"· {content}\n\n", style="dim")
                continue
            else:
                pfx = self.skin.reply_pfx(name)
            self._append_display(f"{pfx}{content}\n\n")
        self._console(f"restored {len(rows)} message(s) from the transcript", "grey50")

    # ---- output routing ----------------------------------------------------------
    # Two surfaces, strictly separated:
    #   _append_display -> top pane (#display): ONLY the character output.
    #   _console        -> bottom pane (#console): operator input + system notices.

    def _append_display(self, text: str, style: str = "") -> None:
        # Accumulate only; the actual repaint is throttled in _flush_display (~16fps).
        # Per-token widget updates are what made the pane thrash; batching one repaint
        # per frame lets Textual's compositor diff just the new cells (no flicker).
        #
        # Text may carry in-band dim markers (llm.DIM_ON/DIM_OFF) around machinery
        # output — reasoning, tool activity. Those spans render dimmed, hermes /
        # Claude-Code style, so they never read as character speech.
        if not text:
            return
        current = style
        for part in _DIM_SPLIT.split(text):
            if part == DIM_ON:
                current = "dim"
                continue
            if part == DIM_OFF:
                current = style
                continue
            if part:
                self.display_segments.append((current, part))
        total = sum(len(t) for _, t in self.display_segments)
        while total > 60000 and self.display_segments:  # bound UI memory
            total -= len(self.display_segments.pop(0)[1])
        self._display_dirty = True

    def _display_tail(self, n: int = 2) -> str:
        """The last n characters currently on the display (for spacing checks)."""
        out = ""
        for _, t in reversed(self.display_segments):
            out = t + out
            if len(out) >= n:
                break
        return out[-n:]

    def _flush_display(self) -> None:
        if not self._display_dirty:
            return
        self._display_dirty = False
        # Follow the tail only if the operator is already at the bottom; if they scrolled
        # up to read history, don't yank them back down.
        at_bottom = self.display_scroll.scroll_offset.y >= self.display_scroll.max_scroll_y - 1
        rendered = Text()
        for style, chunk in self.display_segments:
            rendered.append(chunk, style="dim" if style == "dim" else None)
        self.transcript.update(rendered)
        if at_bottom:
            self.display_scroll.scroll_end(animate=False)

    def _console(self, text: str, style: str = "grey70") -> None:
        # Render as a Rich Text object (not markup) so JSON/brackets never break parsing.
        self.console_log.write(Text(text, style=style))

    def _write_banner(self) -> None:
        self._console(self.skin.tagline, self.skin.tagline_color)
        self._console("Top pane = persona output. This console = your input. Enter sends a message.", "grey50")
        self._console("Type /help for commands. /mode live = it keeps creating while you watch; /mode chat = it waits for you.", "grey50")
        self._update_status()

    def _update_status(self) -> None:
        mem_chars = len(self.agent.memory.load())
        ctx = self.session.context.token_count()
        model = self.agent.settings.model
        provider = self.agent.settings.provider
        persona = self.agent.char_name()
        activity = self._activity if self._is_streaming() else "waiting"
        self.status.update(
            f"persona={persona} | mode={self.mode} | {activity} | patience={self.patience:.2f}s | "
            f"memory={mem_chars} chars/{self.agent.memory.limits.max_tokens} tok | "
            f"ctx≈{ctx}/{self.session.context.max_tokens} | {provider}:{model}"
        )
        self._render_sidebar()

    # ---- telemetry sidebar -------------------------------------------------------

    @staticmethod
    def _bar(frac: float, width: int = 16, color: str = "#00d75f") -> Text:
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        filled = int(round(frac * width))
        t = Text()
        t.append("█" * filled, style=color)
        t.append("░" * (width - filled), style="#2a3a2a")
        t.append(f" {int(round(frac * 100)):3d}%", style="grey62")
        return t

    @staticmethod
    def _human_bytes(n: int) -> str:
        f = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if f < 1024 or unit == "GB":
                return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
            f /= 1024
        return f"{f:.1f}GB"

    def _workspace_usage(self) -> tuple[int, int]:
        # Throttle the disk walk to ~1s; it runs off the frequent status refresh.
        now = time.monotonic()
        if now - self._ws_cache[0] < 1.0:
            return self._ws_cache[1], self._ws_cache[2]
        total = files = 0
        try:
            ws = self.agent.memory.path.parent  # SANDBOX_ROOT/workspace
            for p in ws.rglob("*"):
                if p.is_file():
                    files += 1
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except Exception:
            pass
        self._ws_cache = (now, total, files)
        return total, files

    def _render_sidebar(self) -> None:
        if not hasattr(self, "gauges"):
            return
        ctx = self.session.context.token_count()
        ctx_max = self.session.context.max_tokens or 1
        mem = self.agent.memory.load()
        mem_chars = len(mem)
        mem_max = self.agent.memory.limits.max_chars or 1
        ws_bytes, ws_files = self._workspace_usage()
        ws_cap = 1_000_000  # soft 1 MB display cap (sandbox isn't hard-limited yet)

        t = self.skin
        head = f"bold {t.accent}"
        g = Text()
        g.append("CONTEXT\n", style=head)
        g.append(self._bar(ctx / ctx_max, color=t.gauge_context))
        g.append(f"\n{ctx:,} / {ctx_max:,} tok\n\n", style="grey70")
        g.append("MEMORY\n", style=head)
        g.append(self._bar(mem_chars / mem_max, color=t.gauge_memory))
        g.append(f"\n{mem_chars} / {mem_max} chars\n\n", style="grey70")
        g.append("SANDBOX  (soft)\n", style=head)
        g.append(self._bar(ws_bytes / ws_cap, color=t.gauge_sandbox))
        g.append(f"\n{self._human_bytes(ws_bytes)} · {ws_files} files\n", style="grey70")
        net_on = self.agent.state.load().get("network_access", False)
        g.append(
            f"isolation {self.agent.settings.py_backend} · net {'ON' if net_on else 'off'} · "
            f"mode {self.mode}\n",
            style="grey50",
        )
        self.gauges.update(g)

        m = Text()
        m.append("─ MEMORY DOC ─\n", style=head)
        m.append(mem if mem.strip() else "(empty)", style="grey70")
        self.memview.update(m)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Show matching /commands under the input as you type (autocomplete hint)."""
        val = (event.value or "").lower()
        if val.startswith("/"):
            matches = [c for c in SLASH_COMMANDS if c.startswith(val)]
            self.suggest.update(Text("  ".join(matches[:8]), style="#5f875f") if matches else Text(""))
        else:
            self.suggest.update(Text(""))

    def _is_streaming(self) -> bool:
        return self.current_thread is not None and self.current_thread.is_alive()

    def _start_stream(self, job: StreamJob, prefix: str) -> bool:
        with self.worker_lock:
            if self._is_streaming():
                return False
            self.interrupt_event.clear()
            # Hold off the next spontaneous cycle until THIS stream's "done" resets
            # the timer to now+patience. Without this, _pump could fire a fresh think
            # in the gap between the worker thread dying and "done" being drained —
            # the pause would be bypassed and the chara would spam (talkative loop).
            self.next_spont_at = time.monotonic() + 86400
            # Status-line activity word: replies are "talking"; spontaneous cycles
            # rotate through the activity vocabulary.
            self._activity = "talking" if job.kind in ("user", "event") else random.choice(self.ACTIVITIES)
            thread = threading.Thread(target=self._stream_worker, args=(job, prefix), daemon=True)
            self.current_thread = thread
            thread.start()
            self._update_status()
            return True

    def _stream_worker(self, job: StreamJob, prefix: str) -> None:
        chunks: Iterable[str]
        if job.kind == "think":
            chunks = self.agent.stream_think(self.session)
        elif job.kind == "event":
            chunks = self.agent.stream_event(job.text or "", self.session)
        else:
            chunks = self.agent.stream_handle(job.text or "", self.session)
        self.output.put(("prefix", prefix))
        try:
            for chunk in chunks:
                if self.interrupt_event.is_set():
                    self.output.put(("interrupt", "↯ interrupt — operator input overrides current cycle"))
                    break
                self.output.put(("chunk", chunk))
        except Exception as e:
            self.output.put(("error", f"stream error: {e}"))
        finally:
            self.output.put(("done", "\n"))

    def _drain_output(self) -> None:
        wrote = False
        while True:
            try:
                kind, text = self.output.get_nowait()
            except queue.Empty:
                break
            wrote = True
            if kind == "prefix":
                # Blank-line separation between character messages in the top pane.
                if self.display_segments and self._display_tail() != "\n\n":
                    self._append_display("\n")
                self._append_display(text)
            elif kind == "chunk":
                self._append_display(text)
            elif kind == "interrupt":
                # System notice, not character speech -> console, dimmed.
                self._console(text, "grey42")
            elif kind == "error":
                self._console(text, "red")
            elif kind == "perm":
                self._console(text, "yellow")
            elif kind == "done":
                self._append_display(text)
                self.next_spont_at = time.monotonic() + self.patience
        if wrote:
            self._update_status()

    def _pump(self) -> None:
        """Start the next stream when idle. Operator input has priority over self-talk,
        so a queued message is never lost behind a long live-provider spontaneous cycle."""
        if self.shutdown_requested or self._is_streaming():
            return
        if self.pending_input is not None:
            text = self.pending_input
            self.pending_input = None
            self._start_stream(StreamJob(kind="user", text=text), prefix=self.skin.reply_pfx(self.agent.char_name()))
            return
        now = time.monotonic()
        if self.mode == "live" and now >= self.next_spont_at and now >= self.grace_until:
            # live mode = the chara keeps living: spontaneous cycles between your
            # messages, paced by `patience` (plus the post-greeting attach grace).
            self._start_stream(StreamJob(kind="think"), prefix=self.skin.thought_pfx(self.agent.char_name()))

    def _scheduler_tick(self) -> None:
        if self.shutdown_requested:
            return
        self._pump()
        self._update_status()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.input.value = ""
        self.suggest.update(Text(""))
        if not text:
            return
        low = text.lower()
        # ---- pending permission request: this input is the answer ----
        if self._perm_pending is not None:
            if low in {"y", "yes", "allow", "ok", "同意", "允许", "是"}:
                self._perm_answer = True
                self._perm_event.set()
                self._console(f"⚿ {self._perm_pending} → granted", "yellow")
                return
            self._perm_answer = False
            self._perm_event.set()
            self._console(f"⚿ {self._perm_pending} → denied", "yellow")
            if low in {"n", "no", "deny", "拒绝", "否"}:
                return
            # Anything else denies AND is processed as a normal message/command below.
        # ---- console commands: handled locally, output to the console pane only ----
        if low in {"/exit", "/quit"}:
            await self.action_quit_clean()
            return
        if low in {"/settings"}:
            await self.action_open_settings()
            return
        if low in {"/clear", "/cls"}:
            await self.action_clear_display()
            return
        if low.startswith(("/patience", "/cooldown")):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                try:
                    self.patience = max(0.0, float(parts[1]))
                    self._console(f"patience = {self.patience:.2f}s", "grey50")
                    self.next_spont_at = time.monotonic() + self.patience
                    self._update_status()
                except ValueError:
                    self._console("bad patience value", "red")
            else:
                self._console(f"patience = {self.patience:.2f}s (usage: /patience <sec>)", "grey50")
            return
        # Pre-rename muscle memory: forever on/off were the old names for the modes.
        if low in {"/forever off", "/forever", "/pause"}:
            low = "/mode chat"
        elif low in {"/forever on", "/resume"}:
            low = "/mode live"
        if low.startswith(("/mode", "/presence")):
            parts = low.split()
            known = set(MODES) | {"on", "off", "auto", "always"}  # incl. pre-rename spellings
            if len(parts) == 2 and parts[1] in known:
                want = normalize_mode(parts[1])
                self.mode = want
                self.grace_until = 0.0  # mid-session switch: the operator is clearly here
                if want == "live":
                    self.next_spont_at = time.monotonic() + self.patience
                self.settings = replace(self.settings, mode=want)
                save_settings(self.settings)
                self._console(f"mode = {want} (persisted for this chara)", "grey50")
                self._update_status()
            else:
                self._console(
                    f"mode = {self.mode}  (usage: /mode live|chat — live: it keeps creating "
                    "while you watch; chat: it waits and only replies to you)",
                    "grey50",
                )
            return
        if low.startswith("/theme"):
            self._cmd_theme(text)
            return
        if low.startswith("/net"):
            parts = low.split()
            if len(parts) == 2 and parts[1] in {"on", "off"}:
                self.agent.state.set_network(parts[1] == "on")
                self._console(f"network access = {parts[1].upper()} (terminal tool, this session)", "grey50")
                self._update_status()
            else:
                cur = self.agent.state.load().get("network_access", False)
                self._console(f"network access = {'ON' if cur else 'OFF'}  (usage: /net on|off)", "grey50")
            return
        if low.startswith("/allow-dir"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                p = str(Path(parts[1].strip()).expanduser().resolve())
                self.agent.state.add_writable_path(p)
                self._console(f"writable path added (sandbox): {p}", "grey50")
            else:
                paths = self.agent.state.load().get("writable_paths", [])
                self._console("writable paths: " + (", ".join(paths) or "(workspace only)"), "grey50")
            return
        if low in {"/help", "help", "?", "/?"}:
            self._show_help()
            return
        if text.startswith("/"):
            # Remaining agent commands (/status /memory /files /read ...): run inline,
            # echo command + result into the console — never the character pane.
            self._console(text, "green")
            try:
                result = self.agent._command(text, self.session)
            except Exception as e:  # noqa: BLE001 - surface to operator
                result = f"command failed: {e}"
            for line in str(result).splitlines() or [""]:
                self._console(line, "grey62")
            self._update_status()
            return
        # ---- ordinary message: QUEUE it for the persona (never dropped) ----
        # Echo into both panes: the console (your log) and the top transcript (so the
        # reply has your prompt right above it).
        self._console(text, self.skin.operator_color)
        if self.display_segments and self._display_tail() != "\n\n":
            self._append_display("\n")
        self._append_display(f"{self.skin.operator_pfx(self.settings.user_name)}{text}\n")
        # The operator has spoken — the attach grace has served its purpose.
        self.grace_until = 0.0
        self.pending_input = text
        # Interrupt any in-flight cycle so the queued message goes out promptly; the pump
        # then starts it the moment the worker actually stops.
        if self._is_streaming():
            self.interrupt_event.set()
        self._pump()

    def _show_help(self) -> None:
        self._console("operator console commands:", "grey50")
        for line in (
            "  talk        type anything (no slash) — sent to the persona, reply shows in the top pane",
            "  /status     environment + context size            /memory   show loaded memory",
            "  /files      sandbox files     /workspace  workspace files     /logs   recent audit",
            "  /read <f>   read sandbox file      /wread <f>  read workspace file",
            "  /reset      zero session context (memory document stays)",
            "  /mode live|chat   how it behaves while you're here — live: keeps creating while",
            "                    you watch (interject anytime); chat: waits and only replies to you",
            "  /net on|off       allow the terminal tool to reach the network (this session)",
            "  /allow-dir <path> add a writable path outside the workspace (sandbox isolation)",
            "  /patience <sec>   pause between its spontaneous cycles (live mode)",
            "  /theme [name]     list/switch TUI skin",
            "  /settings   reopen config      /clear   clear top pane      /exit   shut down",
        ):
            self._console(line, "grey62")

    def _cmd_theme(self, text: str) -> None:
        """`/theme` lists available skins; `/theme <name>` switches and persists it."""
        themes = _discover("themes", (".json",))  # [(stem, path)]
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            self._console(f"current theme: {self.skin.name}", "grey50")
            names = ", ".join(stem for stem, _ in themes) or "(none in themes/)"
            self._console(f"available: {names}  ·  built-in: default", "grey62")
            self._console("usage: /theme <name>   (or pick one in /settings)", "grey62")
            return
        want = parts[1].strip().lower()
        match = next((p for stem, p in themes if stem.lower() == want), None)
        if want in {"", "default", "builtin"}:
            match = ""  # built-in default
        elif match is None:
            self._console(f"no theme '{parts[1].strip()}'. try /theme to list.", "red")
            return
        self.settings = replace(self.settings, tui_theme_path=match)
        save_settings(self.settings)
        self.skin = load_theme(match)
        self._apply_theme()
        # Reset the console so lingering decorative lines from the old skin are gone.
        self.console_log.clear()
        self._write_banner()
        self._console(f"theme → {self.skin.name}", "grey50")

    # ---- presence: permission requests + detach ------------------------------------

    def _permission_request(self, kind: str, reason: str, detail: str, wait_seconds: int) -> bool:
        """request_permission hook. Runs on the WORKER thread: post the question to
        the console, block until the operator answers in the input or the model's
        own deadline passes (timeout = deny)."""
        if self.shutdown_requested:
            return False
        label = kind + (f" ({detail})" if detail.strip() else "")
        self._perm_answer = False
        self._perm_event.clear()
        self._perm_pending = label
        self.output.put(("perm", f"⚿ {self.agent.char_name()} requests permission: {label}"))
        if reason.strip():
            self.output.put(("perm", f"  reason: {reason.strip()}"))
        self.output.put(("perm", f"  y/yes = allow · anything else denies · auto-deny in {wait_seconds}s"))
        answered = self._perm_event.wait(wait_seconds)
        granted = bool(answered and self._perm_answer)
        self._perm_pending = None
        if not answered:
            self.output.put(("perm", f"⚿ {label} → denied (no answer in {wait_seconds}s)"))
        return granted

    def _note_detach_once(self) -> None:
        """Presence bookkeeping on the way out: tell the chara the operator left,
        queue the handoff line for the daemon, and clear the present flag."""
        if self._detached:
            return
        self._detached = True
        try:
            self.agent.note_detach(self.session)
        except Exception:
            pass
        try:
            self.agent.state.set_present(False)
        except Exception:
            pass

    async def action_open_settings(self) -> None:
        if self._is_streaming():
            self.interrupt_event.set()
            await asyncio.sleep(0.05)
        self.push_screen(WelcomeScreen(self.settings, mid_session=True), self._welcome_done)

    async def action_clear_display(self) -> None:
        self.display_segments: list[tuple[str, str]] = []  # (style, text); "dim" = machinery
        self._display_dirty = False
        self.transcript.update(Text(""))
        self._console("top pane cleared", "grey50")

    async def action_quit_clean(self) -> None:
        self.shutdown_requested = True
        self.interrupt_event.set()
        self._perm_event.set()  # release a worker blocked on a permission question
        self._note_detach_once()
        self._console(self.skin.quit_line, self.skin.tagline_color)
        if self.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
                self._console("cleanup complete · runtime sandbox zeroed", "grey50")
            except Exception as e:
                self._console(f"cleanup failed: {e}", "red")
        self.exit()

    async def on_unmount(self) -> None:
        self.shutdown_requested = True
        self.interrupt_event.set()
        self._perm_event.set()
        self._note_detach_once()
        if self.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LunaMoth single-terminal TUI")
    # `patience` = pause between spontaneous cycles; --cooldown kept as an alias.
    parser.add_argument("--patience", "--cooldown", dest="patience", type=float, default=2.0)
    # Interaction mode (default: the chara's persisted setting). --forever/--think
    # and --no-think kept as pre-rename aliases for live/chat.
    parser.add_argument("--mode", choices=["live", "chat"], default="")
    parser.add_argument("--forever", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--think", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-think", action="store_true", help=argparse.SUPPRESS)
    # Persistence is the default (like Hermes / Claude Code). Opt in to wiping
    # the session sandbox on exit; --no-clean-on-exit kept as a harmless alias.
    parser.add_argument("--clean-on-exit", action="store_true")
    parser.add_argument("--no-clean-on-exit", action="store_true")
    args = parser.parse_args(argv)
    mode_override = args.mode or ("live" if (args.forever or args.think) else ("chat" if args.no_think else ""))
    app = LunaMothTUI(
        patience=args.patience,
        clean_on_exit=args.clean_on_exit,
        mode_override=mode_override,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

