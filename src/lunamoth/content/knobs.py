"""Card/user-facing chara knobs: patience and embodiment.

Pure helpers live in content so core, protocol and frontends can agree on the
same parsing/formatting without importing each other.
"""
from __future__ import annotations

import math
from typing import Any

EMBODIMENT_STANCES = {"literal", "actor"}

# Optional prompt MODULES (skill-like add-ons, toggled at wake, editable→next start).
# force_roleplay is the actor embodiment stance (kept on the embodiment axis for
# back-compat); personal_website is an independent on/off knob.
WEBSITE_VALUES = {"on", "off"}

MODULE_COPY = {
    "en": {
        "force_roleplay": (
            "Force roleplay: the model embodies the character; tools work backstage so "
            "the fiction stays whole. Best for characters whose world has no computers."
        ),
        "personal_website": (
            "Personal website: the character keeps its own homepage (home/index.html) "
            "shown in a website tab — a place to gather and link its work, in its own style."
        ),
    },
    "zh": {
        "force_roleplay": "强制角色扮演：模型化身为角色，工具在后台运作、戏不破。适合世界观里没有计算机的角色。",
        "personal_website": "个人主页：角色维护自己的主页（home/index.html），在主页 tab 展示——汇集并串联它的作品，自成一派。",
    },
}

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


# The bare default base-pause between spontaneous cycles (seconds). A numeric
# patience that differs from this is treated as an explicit operator setting, so a
# card default can still win when the operator never touched it. The ONE definition
# of both the default and the "is it explicit?" rule — agent.patience_resolved and
# settings.load both read these instead of hard-coding the literal / the abs() test.
DEFAULT_PATIENCE = 3600.0

# Engagement window (seconds): while you're actively talking, the chara sets self-work
# aside and resumes after this much silence. The ONE default — the dataclass field + the
# supervisor/frontend snapshot fallbacks read this instead of re-hardcoding 300.
DEFAULT_QUIET = 300


def patience_is_explicit(value: float) -> bool:
    """True when a numeric patience differs from the bare DEFAULT_PATIENCE — i.e.
    the operator intentionally chose a non-default value (so it outranks a card
    default). The companion `patience_override` flag separately records an explicit
    set of the default value ITSELF; this catches an explicit non-default at load."""
    return value > 0 and abs(value - DEFAULT_PATIENCE) > 1e-9


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


def normalize_force_roleplay(value: Any) -> bool | None:
    """Normalize a card's `force_roleplay` field to True | False | None (unset).

    Accepts booleans and the strings true/false/on/off/1/0/yes/no. Anything
    else (incl. unset/invalid) → None. The card FIELD is a boolean; the engine's
    internal stance value stays "literal"/"actor" (bridged in
    effective_embodiment): True ≡ "actor", False ≡ "literal".
    """
    if isinstance(value, bool):
        return value
    v = str(value or "").strip().lower()
    if v in {"true", "on", "1", "yes"}:
        return True
    if v in {"false", "off", "0", "no"}:
        return False
    return None


def normalize_website(value: Any) -> str:
    """Normalize a personal_website override to 'on' | 'off' | '' (unset).

    Accepts booleans (card hook `extensions.lunamoth.website`) and the strings
    on/off/true/false/1/0/yes/no. Anything else (incl. unset) → ''.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    v = str(value or "").strip().lower()
    if v in {"on", "true", "1", "yes"}:
        return "on"
    if v in {"off", "false", "0", "no"}:
        return "off"
    return ""


def embodiment_copy(stance: str, lang: str = "en") -> str:
    return EMBODIMENT_COPY[_lang(lang)][stance if stance in EMBODIMENT_STANCES else "literal"]
