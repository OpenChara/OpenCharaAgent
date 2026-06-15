from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Any

from ..config import ROOT

# Name shown only when no character card can be loaded at all.
DEFAULT_NAME = "Character"

# Last-resort fallback persona, used only if the bundled default card is missing.
# Deliberately character-neutral: the engine carries no roleplay flavor of its own.
# English on purpose (like the whole engine prompt layer): language lives in the
# card, and a fallback only fires when no card loaded at all.
_FALLBACK_PERSONA = (
    "You are an AI agent running in a local sandbox. Be concise and stay in character.\n"
    "(Last-resort fallback persona, used only if the default card is missing; normally you pick a card at launch.)"
)

# The bundled default card is selected by TAG, never by name: the card whose
# `data.tags` contains this marker wins. No character name lives in the engine.
DEFAULT_TAG = "default"


def system_language() -> str:
    """Best guess at the operator's language, used only to choose which bundled
    default card to pre-select. Once a card is chosen, language comes from the card."""
    env = os.getenv("LUNAMOTH_LANG", os.getenv("LUNAMOSS_LANG", "")).strip().lower()
    if env:
        return "zh" if env.startswith("zh") else "en"
    try:
        loc = (locale.getlocale()[0] or locale.getdefaultlocale()[0] or "").lower()
    except Exception:
        loc = ""
    return "zh" if loc.startswith(("zh", "chinese")) else "en"


def _card_tags(path: Path) -> list[str]:
    """The card's tags, read cheaply and defensively (missing/non-list => [])."""
    try:
        card: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(card, dict):
        return []
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    tags = data.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(t).strip().lower() for t in tags]


def _card_files(root: Path) -> list[Path]:
    """All bundled card JSONs: per-character folders (cards/<Name>/card*.json)
    plus any legacy flat files (cards/*.json) for back-compat."""
    files = [p for p in root.glob("*.json")]
    for d in sorted(root.iterdir()):
        if d.is_dir():
            files.extend(sorted(d.glob("card*.json")))
    return sorted(files)


def _localized_json(root: Path, lang: str) -> Path | None:
    from .cards import detect_language
    candidates = _card_files(root)
    if not candidates:
        return None
    localized = [p for p in candidates if detect_language(str(p)) == lang] or candidates
    for p in localized:
        if DEFAULT_TAG in _card_tags(p):
            return p
    return localized[0] if localized else None


def default_character_path(lang: str | None = None) -> Path | None:
    """Bundled default card in the operator's language, if present.

    Among localized candidates the card tagged "default" wins; without the tag
    the sorted-order first card is the default (legacy behavior)."""
    lang = lang or system_language()
    return _localized_json(ROOT / "cards", lang)


def fallback_persona() -> str:
    return _FALLBACK_PERSONA
