"""The welcome / settings screen: pick a provider + character, test, enter.

Self-contained: everything the boot/settings flow needs (content discovery,
preset prefill, connection test via the protocol seam) lives here so app.py
stays the conversation surface only."""
from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

from .. import art
from ...config import ROOT
from ...content.themes import TuiTheme, load_theme
from ...protocol.api import test_connection
from ...session.settings import PRESETS, Settings


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
            # Context window is the model's real window (read from the provider),
            # not a knob. Only durable-memory size is tunable here.
            yield Label("Memory chars (0 = card default)", classes="field-label")
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
        from ...content.cards import CharacterCard
        from ...content.persona import default_character_path, default_world_path

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
            ok, msg = test_connection(settings)
            self.app.call_from_thread(self._show_result, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _show_result(self, ok: bool, msg: str) -> None:
        color = "#00ff66" if ok else "#ff4040"
        mark = "✓" if ok else "✗"
        self.query_one("#conn_status", Static).update(f"[{color}]{mark} {msg}[/]")
