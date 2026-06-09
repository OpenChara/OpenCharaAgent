from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT


def language() -> str:
    lang = os.getenv("SCP079_LANG", "zh").strip().lower()
    return "en" if lang.startswith("en") else "zh"


def persona_path() -> Path:
    return ROOT / "prompts" / f"079_personality_{language()}.md"


def tools_path() -> Path:
    return ROOT / "prompts" / f"tools_{language()}.md"


def load_persona() -> str:
    return persona_path().read_text(encoding="utf-8")


def load_tool_spec() -> str:
    return tools_path().read_text(encoding="utf-8")
