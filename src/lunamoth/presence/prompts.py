"""Presence modes and card-driven enter/leave prompt resolution."""
from __future__ import annotations

from ..worldinfo import apply_macros

MODES = ("auto", "always", "off")
DEFAULT_MODE = "auto"


def normalize_mode(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def _card_prompt(card, key: str) -> str:
    """A presence prompt declared by the card (extensions.lunamoth.<key>), if any."""
    if card is None:
        return ""
    for source in (card.defaults(), card.extensions):
        v = source.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def attach_text(card, char: str, user: str) -> str:
    """The card's arrival prompt, macros applied. Empty when the card declares none."""
    raw = _card_prompt(card, "on_attach")
    return apply_macros(raw, char, user) if raw else ""


def detach_text(card, char: str, user: str) -> str:
    """The card's departure prompt, macros applied. Empty when the card declares none."""
    raw = _card_prompt(card, "on_detach")
    return apply_macros(raw, char, user) if raw else ""
