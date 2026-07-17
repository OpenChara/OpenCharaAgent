"""Interaction modes — the ONE per-chara control over how it behaves.

The chara's context and behavior are INDEPENDENT of whether a human is attached.
There is no presence fact, no enter/leave conversation marker, no attach/detach
awareness in the prompt: a chara lives the same way whether or not someone is
watching. What it can be controlled by is its MODE (Settings.mode, `/mode` to
flip), with exactly two values — see prompts.py:

    live   (default) keeps living its own loop; you can interject anytime.
    chat   attends to you only — no self-talk; it speaks only in reply.

Detached life is NOT a mode: `chara start/stop` is that switch, and a running
daemon always self-runs. The supervisor's idle gate keys off the last user
MESSAGE (not attach), so a chara sets its work aside while you talk and resumes
after you fall quiet — entirely speech-driven.
"""
from .prompts import normalize_mode

__all__ = [
    "normalize_mode",
]
