"""The contract layer between the backend and every frontend.

Frontends import events/codec from here and NOTHING deeper — the agent's
internals must stay invisible to them (hermes keeps TUI/web/Telegram behind
the same seam; that is the whole secret of its frontend independence)."""
from .codec import PROTOCOL_VERSION, from_dict, from_json, to_dict, to_json
from .events import (
    MUSE, SAY, Attachment, Event, Notice, TextDelta, ThinkDelta, ToolEnd, ToolStart,
)

__all__ = [
    "PROTOCOL_VERSION", "from_dict", "from_json", "to_dict", "to_json",
    "MUSE", "SAY", "Attachment", "Event", "Notice", "TextDelta", "ThinkDelta", "ToolEnd", "ToolStart",
]
