"""Card/user-facing chara knobs: patience and embodiment.

Pure helpers live in content so core, protocol and frontends can agree on the
same parsing/formatting without importing each other.
"""
from __future__ import annotations

import math
from typing import Any

EMBODIMENT_STANCES = {"literal", "actor"}

EMBODIMENT_COPY = {
    "en": {
        "literal": (
            "Literal: the character IS a digital being; the tools are its own hands. "
            "Best for AI/digital-native characters."
        ),
        "actor": (
            "Actor: the model embodies the character; tools work backstage so the "
            "fiction stays whole. Best for characters whose world has no computers."
        ),
    },
    "zh": {
        "literal": "字面存在：角色就是一个数字生命，工具是它自己的手。适合 AI／数字原生角色。",
        "actor": "演员化身：模型化身为角色，工具在后台运作、戏不破。适合世界观里没有计算机的角色。",
    },
}


def _lang(lang: str) -> str:
    return "zh" if str(lang).startswith("zh") else "en"


def parse_patience(value: Any) -> float | None:
    """Parse a card/command patience value in seconds.

    Accepted values are positive numeric values. Returns None for
    missing/invalid input. No presets: patience is ordinary wall seconds.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        patience = float(value)
    elif isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return None
        try:
            patience = float(raw)
        except ValueError:
            return None
    else:
        return None
    if math.isfinite(patience) and patience > 0:
        return patience
    return None


def normalize_embodiment(value: Any) -> str:
    """Return a valid stance, or '' for unset/invalid."""
    v = str(value or "").strip().lower()
    return v if v in EMBODIMENT_STANCES else ""


def embodiment_copy(stance: str, lang: str = "en") -> str:
    return EMBODIMENT_COPY[_lang(lang)][stance if stance in EMBODIMENT_STANCES else "literal"]
