"""Outbound anti-loop filter for bot-reachable messaging channels (audit #33).

Ported from hermes ``gateway/delivery.py`` (the silence-narration scar). In a
bot-to-bot channel a hallucinated "silence narration" token — ``*(silent)*``, a
bare ``.``, ``🔇`` — mirrors back and forth between two agents until a model
crashes with "no content after all retries". Behavioral prompt rules drift
across providers, so the only reliable guard is one outbound chokepoint that
drops these tokens before they ever reach an adapter.

This lives in ``messaging/`` (not the gateway) so both the standalone
:class:`~lunamoth.messaging.gateway.MessagingGateway` and the in-child
``server/messaging_host.MessagingHost`` can share it. (The integrator should
wire :func:`is_silence_narration` into ``messaging_host.py``'s send path too —
the host owns ``server/`` and is out of this worktree's scope.)

Only WHOLE-string silence tokens are dropped. Substantive prose that merely
*mentions* silence ("The deploy ran silently", "Silence is golden — here's the
plan…") is never matched: the pattern is anchored start-to-end and length-
guarded. The muse channel is the chara's own life and is delivered nowhere
external, so this never touches it; it guards only say-channel delivery.
"""
from __future__ import annotations

import re

# Matches a string that is *only* a silence narration, with optional markdown
# wrappers (``*_~` `` and parens). Covers: ``*(silent)*``, ``_silence_``,
# ``(no response)``, ``🔇``, a bare ``.`` / ``…``, and whitespace/marker-padded
# variants seen in the wild. Anchored to whole-string so prose that merely
# contains the word "silent" is never matched.
_SILENCE_NARRATION = re.compile(
    r"^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply|nothing\s+to\s+say)\s*\.?\)?[\s*_~`]*$"
    r"|^[\s*_~`]*[\U0001F507.…]+[\s*_~`]*$",
    re.IGNORECASE,
)

# Real messages are longer than any silence token; this stops a long line that
# merely opens with markdown punctuation from being misread.
_MAX_SILENCE_LEN = 64


def is_silence_narration(content: str | None) -> bool:
    """Return True when ``content`` is *only* a silence-narration token.

    Length-guarded and anchored whole-string so legitimate prose is never
    flagged. Empty / whitespace-only content is treated as silence too: there
    is nothing to deliver and a bot mirror would loop on it.
    """
    if not content:
        return True
    stripped = content.strip()
    if not stripped:
        return True
    if len(stripped) > _MAX_SILENCE_LEN:
        return False
    return bool(_SILENCE_NARRATION.match(stripped))
