"""Plain-terminal setup wizard, in the style of `hermes setup`.

Runs in the normal terminal BEFORE the full-screen chat TUI takes over — so you
meet your chara (pick a model, pick a character) without the screen ever being
hijacked, exactly like Hermes setup. Sequential numbered prompts on stdin/stdout,
so it works over SSH and is trivially debuggable. The full-screen settings screen
is only for hot-swapping mid-session (`/settings`), once you're already inside.

Must be imported only after the CLI has exported the session env vars, because
`settings` resolves its config path at import time.
"""
from __future__ import annotations

import getpass
import sys

from ..config import content_dir
from ..content.knobs import embodiment_copy, normalize_embodiment
from ..session.settings import (
    PRESETS,
    Settings,
    load_settings,
    save_global_key,
    save_settings,
)


def _say(text: str = "") -> None:
    print(text, flush=True)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def _choose(prompt: str, options: list[str], default_index: int = 0) -> int:
    _say(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        marker = "*" if (i - 1) == default_index else " "
        _say(f"   {marker} {i}) {opt}")
    while True:
        raw = _ask("choice", str(default_index + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        _say(f"  please enter 1-{len(options)}")


def _discover_characters() -> list[tuple[str, str]]:
    """(label, path) for the bundled cards. Label is the card's name, else stem.

    Resolved via config.content_dir (like content/persona.py): ROOT/"cards" exists
    only in a dev checkout — on the wheel channel the cards live in the packaged
    lunamoth/_bundled/cards, and scanning ROOT left this menu EMPTY."""
    out: list[tuple[str, str]] = []
    base = content_dir("cards")
    if not base.is_dir():
        return out
    from ..content.cards import CharacterCard

    for p in sorted(base.iterdir()):
        if p.suffix.lower() not in (".json", ".png") or p.name.startswith("."):
            continue
        try:
            name = CharacterCard.load(p).name
        except Exception:
            name = p.stem
        lang = "zh" if any(s in p.stem.lower() for s in (".zh", "-zh", "_zh")) else "en"
        out.append((f"{name}  ({lang})", str(p)))
    return out


def _choose_character(settings: Settings) -> None:
    from ..content.persona import default_character_path

    cards = _discover_characters()
    default = str(default_character_path() or "")
    default_name = next((lbl for lbl, p in cards if p == default), "")
    labels = [f"default · {default_name}" if default_name else "default (bundled)"]
    labels += [lbl for lbl, _ in cards]
    idx = _choose("Character:", labels, 0)
    # Empty path = bundled default (the card tagged "default", language by locale).
    settings.character_path = "" if idx == 0 else cards[idx - 1][1]
    # Let the card's own tools / limits pair fresh (world is inside the card).
    settings.toolpack = ""


def _choose_embodiment(settings: Settings) -> None:
    """Imported cards may not know LunaMoth's stance field; ask the operator."""
    from ..content.cards import CharacterCard
    from ..content.persona import default_character_path, system_language

    path = settings.character_path or str(default_character_path() or "")
    if not path:
        return
    try:
        card = CharacterCard.load(path)
    except Exception:
        return
    if normalize_embodiment(card.defaults().get("embodiment")):
        return
    lang = card.language or system_language()
    stances = ["literal", "actor"]
    default = stances.index(normalize_embodiment(settings.embodiment_override) or "literal")
    idx = _choose("Embodiment stance:", [embodiment_copy(s, lang) for s in stances], default)
    settings.embodiment_override = stances[idx]


def _test(settings: Settings) -> bool:
    from ..protocol.api import test_connection

    if not settings.is_live():
        _say("  (offline/mock provider — nothing to test)")
        return True
    _say("  testing connection ...")
    ok, msg = test_connection(settings)
    _say(f"  {'✓' if ok else '✗'} {msg}")
    return ok


def run_wizard(non_interactive_ok: bool = True) -> Settings:
    """Collect provider/model/persona settings interactively and persist them."""
    settings = load_settings()

    if not sys.stdin.isatty():
        if non_interactive_ok:
            _say("non-interactive terminal: keeping existing/env settings.")
            _say("configure manually via env vars or edit the session config.json.")
            save_settings(settings)
            return settings
        raise RuntimeError("setup wizard needs an interactive terminal")

    _say("LunaMoth setup — press Enter to accept the [default] of any question.")

    preset_names = list(PRESETS.keys()) + ["Custom OpenAI-compatible endpoint"]
    idx = _choose("Model provider:", preset_names, 0)
    if idx < len(PRESETS):
        preset = PRESETS[preset_names[idx]]
        settings.provider = preset.get("provider", settings.provider)
        settings.base_url = preset.get("base_url", "")
        settings.api_key = preset.get("api_key", settings.api_key)
        settings.model = preset.get("model", settings.model)
    else:
        settings.provider = "openai_compatible"

    if settings.provider != "mock":
        settings.base_url = _ask("base_url", settings.base_url)
        try:
            key = getpass.getpass(f"  api_key [{'set' if settings.api_key else 'empty'}]: ").strip()
        except EOFError:
            key = ""
        if key:
            settings.api_key = key
        settings.model = _ask("model", settings.model)
        if not _test(settings) and _choose("Connection failed. Continue anyway?", ["re-enter model/key", "continue"], 0) == 0:
            return run_wizard()

    settings.user_name = _ask("Your name ({{user}})", settings.user_name)

    # Meet the chara — a plain numbered menu. World (embedded book) / tools /
    # limits all come from the card itself.
    _choose_character(settings)
    _choose_embodiment(settings)

    # The keyring is the ONE key store — save_settings never persists api_key, so a
    # CLI-entered key (typed or preset-provided, e.g. Ollama's "ollama") is routed
    # here. No-op on an empty key / the mock provider.
    save_global_key(settings.provider, settings.base_url, settings.api_key, model=settings.model)
    save_settings(settings)
    _say("\nentering the cocoon …")
    return settings
