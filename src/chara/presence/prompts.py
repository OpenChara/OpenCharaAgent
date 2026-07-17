"""Interaction modes (live | chat).

The mode answers ONE question: how does the chara behave while it is being
driven? (Detached background life is not a mode — `chara start/stop` is the
on/off switch for that, and the daemon always self-runs.)

    live   it keeps living — carries on with its own thinking/creating loop;
           you can interject anytime.
    chat   it attends to you — it only ever speaks in reply. No self-talk.

The chara's context and behavior are INDEPENDENT of whether a human is
attached: presence is not a fact the engine injects, and there is no enter/leave
conversation marker. The supervisor's idle gate keys off the last user MESSAGE,
not attach/detach.
"""
from __future__ import annotations

MODES = ("live", "chat")
DEFAULT_MODE = "live"

# Pre-rename spellings (presence auto|always|off, forever on|off) seen in old
# config files / muscle memory — map them onto the two modes.
_LEGACY = {"auto": "live", "always": "live", "on": "live", "off": "chat"}


def normalize_mode(value: str) -> str:
    v = (value or "").strip().lower()
    if v in MODES:
        return v
    return _LEGACY.get(v, DEFAULT_MODE)
