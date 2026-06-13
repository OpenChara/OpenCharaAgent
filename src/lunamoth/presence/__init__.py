"""Presence awareness — the character knows when the operator comes and goes.

A persistent chara runs forever in the background, but it should *feel* the
operator attach and detach. Two card-driven prompts (SillyTavern macros apply):

    extensions.lunamoth.on_attach   injected when the operator connects
    extensions.lunamoth.on_detach   injected when the operator leaves

The prompts live in the card and ONLY in the card — the engine ships no default
text and knows nothing about any specific character. A card that declares
neither simply gets no presence events: attach and detach pass silently.

How the chara behaves WHILE the operator is attached is one per-chara setting
(Settings.mode, `/mode` to flip) with exactly two values — see prompts.py:

    live   (default) greets you, then keeps living its own loop while you watch
           (with a grace pause after the greeting so you can take the first word).
    chat   greets you, then attends to you only — no self-talk while attached.

Detached life is NOT a mode: `lunamoth start/stop` is that switch, and a
running daemon always self-runs. Being present is a FACT (user_present), not a
setting.

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
from .prompts import attach_text, detach_text, normalize_mode
from .state import PresenceState

__all__ = [
    "PresenceState",
    "attach_text",
    "detach_text",
    "normalize_mode",
]
