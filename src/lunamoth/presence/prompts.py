"""Interaction modes + the enter/leave conversation marker text.

The mode answers ONE question: how does the chara behave while the operator is
attached? (Detached background life is not a mode — `lunamoth start/stop` is
the on/off switch for that, and the daemon always self-runs.)

    live   it keeps living — greets you on attach, then carries on with its own
           thinking/creating loop while you watch; you can interject anytime.
           After the greeting there is a grace pause so you get the first word
           if you want it; if you stay silent it simply returns to its work.
    chat   it attends to you — greets you on attach, then waits; it only ever
           speaks in reply. No self-talk while you're attached.

Presence is a NEUTRAL FACT, never a forced reaction: entering the room is
silent; the chara registers the operator only when they actually speak (an
"entered" marker is injected before that first message), and a "left" marker is
injected on detach only if the operator spoke. `marker_text` builds that fact.
A card MAY override the wording (extensions.lunamoth.on_attach for entered /
on_detach for left; macros apply) for a worldview-appropriate line; with no
override the bundled neutral default is used. (This REPLACES the old on_attach/
on_detach "reaction turn" hook, which was removed — the marker is a passive
context line the chara reads on its next turn, not a turn of its own.)
"""
from __future__ import annotations

from ..content.worldinfo import apply_macros

MODES = ("live", "chat")
DEFAULT_MODE = "live"

# Pre-rename spellings (presence auto|always|off, forever on|off) seen in old
# config files / muscle memory — map them onto the two modes.
_LEGACY = {"auto": "live", "always": "live", "on": "live", "off": "chat"}

# The card override key for each marker kind, and the bundled neutral default.
_MARKER_KEY = {"entered": "on_attach", "left": "on_detach"}


def normalize_mode(value: str) -> str:
    v = (value or "").strip().lower()
    if v in MODES:
        return v
    return _LEGACY.get(v, DEFAULT_MODE)


def _default_marker(kind: str, user: str) -> str:
    if kind == "entered":
        return f"[{user} joined the conversation.]"
    return f"[{user} left the conversation.]"


def marker_text(card, kind: str, char: str, user: str) -> str:
    """The neutral '<user> entered/left the conversation' FACT (a passive context
    line, NOT a turn). A card MAY override the wording via
    extensions.lunamoth.on_attach (entered) / on_detach (left); macros apply —
    a card written in another language carries it through that override. With no
    override, the bundled neutral default is English, like the whole engine
    prompt layer. Generation never produces these — they're an Advanced
    card-editor field.
    """
    if card is not None:
        override = card.defaults().get(_MARKER_KEY[kind])
        if isinstance(override, str) and override.strip():
            return apply_macros(override.strip(), char, user)
    return _default_marker(kind, user)
