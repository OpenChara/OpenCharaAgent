"""Typed stream events — what the backend says happened, never how to draw it.

Modeled on hermes-agent's gateway/stream_events.py (frozen dataclasses, a small
closed vocabulary). This module must keep ZERO project-internal imports so the
events stay trivially serializable (codec.py) and usable by any client.

Rendering is each frontend's own business: the TUI dims tool chatter and hides
thinking behind its ✶ indicator, the plain terminal prints ANSI dim, a future
messaging adapter delivers only what is addressed to the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

# TextDelta channels. `say` is addressed to the user — every frontend delivers
# it. `muse` is the chara to itself (unattended cycles, work narration) — only
# panoramic frontends (TUI / desktop work view) show it; messaging adapters
# drop it. The speak-tool design builds on this split.
SAY = "say"
MUSE = "muse"


@dataclass(frozen=True)
class TextDelta:
    """A chunk of the chara's streamed prose."""
    text: str
    channel: str = SAY


@dataclass(frozen=True)
class ThinkDelta:
    """A chunk of model reasoning. Hidden by default in every frontend."""
    text: str


@dataclass(frozen=True)
class ToolStart:
    """A tool call is about to run (drives status/activity displays)."""
    name: str
    preview: str = ""
    index: int = 0


@dataclass(frozen=True)
class ToolEnd:
    """A tool call finished; `summary` is a compact preformatted result line."""
    name: str
    ok: bool = True
    duration: float = 0.0
    summary: str = ""
    index: int = 0


@dataclass(frozen=True)
class Notice:
    """Engine-originated side note (kind: retry | truncation | ...).
    Always shown, always visually secondary to the chara's prose."""
    kind: str
    text: str = ""


# Files are NOT a protocol event: like hermes, the chara surfaces a file by writing
# a MEDIA:<path> marker in its say text, which each surface extracts at its render/
# delivery edge (see protocol/media.py). There is no Attachment event.

Event = Union[TextDelta, ThinkDelta, ToolStart, ToolEnd, Notice]
