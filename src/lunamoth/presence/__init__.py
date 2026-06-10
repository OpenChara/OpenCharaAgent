"""Presence awareness — the character knows when the operator comes and goes.

A persistent chara runs forever in the background, but it should *feel* the
operator attach and detach. Two card-driven prompts (SillyTavern macros apply):

    extensions.lunamoth.on_attach   injected when the operator connects
    extensions.lunamoth.on_detach   injected when the operator leaves

The prompts live in the card and ONLY in the card — the engine ships no default
text and knows nothing about any specific character. A card that declares
neither simply gets no presence events: attach and detach pass silently.

One global mode (Claude-Code-style, in Settings.presence, `/presence` to flip):

    auto    (default) on attach the character greets, then the forever loop is
            HELD until the operator's first message; it resumes only if forever
            was already configured on.
    always  greet on attach, never wait — the loop keeps running immediately.
    off     no presence events; the character never self-starts (the operator
            can still `/forever on` explicitly).

Cross-process handoff: when a TUI detaches, the detach event is queued in a
small state file inside the session sandbox; the background daemon consumes it
on startup so the chara's loop continues *knowing* the operator left. The same
file remembers whether the chara has ever met the operator (first boot shows
the card's first_mes; later attaches get a live arrival turn instead).

Presence also gates the `request_permission` tool: while the operator is
attached the character may ask for network / writable paths / more resources
and wait for an answer (timeout = deny); while the operator is away every
request is auto-denied and merely logged.
"""
from .prompts import DEFAULT_MODE, MODES, attach_text, detach_text, normalize_mode
from .state import PresenceState

__all__ = [
    "DEFAULT_MODE",
    "MODES",
    "PresenceState",
    "attach_text",
    "detach_text",
    "normalize_mode",
]
