from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT


def language() -> str:
    lang = os.getenv("LUNAMOSS_LANG", os.getenv("SCP079_LANG", "zh")).strip().lower()
    return "en" if lang.startswith("en") else "zh"


# Name used for display when no character card could be loaded at all.
DEFAULT_NAME = "LunaMoss"


def default_character_path() -> Path | None:
    """Bundled default character (LunaMoss 月蛾) for the active language, if present."""
    p = ROOT / "characters" / f"LunaMoss.{language()}.json"
    return p if p.exists() else None


def default_world_path() -> Path | None:
    """World book that pairs with the default character, if present."""
    p = ROOT / "worlds" / f"LunaMoss.{language()}.json"
    return p if p.exists() else None


def persona_path() -> Path:
    return ROOT / "prompts" / f"default_persona_{language()}.md"


def tools_path() -> Path:
    return ROOT / "prompts" / f"tools_{language()}.md"


def load_persona() -> str:
    return persona_path().read_text(encoding="utf-8")


def load_tool_spec() -> str:
    return tools_path().read_text(encoding="utf-8")
