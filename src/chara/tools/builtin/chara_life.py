"""Chara-life tools — OpenCharaAgent's own, with no hermes counterpart: speak and rest.
These are what make a chara a living being rather than a workhorse; they stay
through the hermes-tool migration.

The chara's own lasting threads are NOT here — they live in the `task` tool
(tools/task.py), oriented by the user-owned aspiration (tools/polaris.py); `task`
and `todo` are deliberately distinct. The old chara-mutable wish/goal tool was
removed (a chara no longer owns its value-direction — the aspiration does).

There is no `send_file` tool: like hermes, the chara puts a file in front of the
user by writing a ``MEDIA:<workspace path>`` line in its reply — the agent extracts
that marker into an attachment (see ``core/agent.py:_media_filter``). One fewer
tool, and the file rides along with the words that introduce it.
"""
from __future__ import annotations

import re
import time

from ..registry import registry, tool_error, tool_result

# The chara may rest from a single minute to a week. We accept the duration with a
# UNIT (m/h/d) rather than as a raw minute count so the model never has to express
# "7 days" as the awkward, error-prone "10080 minutes" — it just says "7d" / "2h".
_MIN_REST_MINUTES = 1
_MAX_REST_MINUTES = 7 * 24 * 60  # 10080 = 7 days

# Unit → minutes. Bare numbers (no unit) are read as minutes. zh units included so a
# value the model phrases in Chinese ("2小时", "3天") parses the same as "2h"/"3d".
_UNIT_TO_MINUTES = {
    "": 1, "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1, "分": 1, "分钟": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60, "时": 60, "小时": 60,
    "d": 1440, "day": 1440, "days": 1440, "天": 1440, "日": 1440,
}
_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-z一-鿿]*)\s*$")


def _parse_rest_minutes(value) -> float | None:
    """Parse a rest duration into minutes. Accepts a bare number (minutes) or a
    value with a unit suffix — '45m', '2h', '3d' (and zh '2小时'/'3天'). Returns
    None when the value is missing or cannot be parsed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip().lower())
    if not match:
        return None
    mult = _UNIT_TO_MINUTES.get(match.group(2))
    if mult is None:
        return None
    minutes = float(match.group(1)) * mult
    return minutes if minutes > 0 else None


def _format_rest(minutes: float) -> str:
    """Human-friendly rest length for the confirmation note — days/hours/minutes."""
    if minutes >= 1440:
        d = minutes / 1440
        return f"~{d:g} day{'s' if d != 1 else ''}"
    if minutes >= 60:
        h = minutes / 60
        return f"~{h:g} hour{'s' if h != 1 else ''}"
    return f"~{minutes:g} min"


# ---- speak -----------------------------------------------------------------------

def speak(args, ctx) -> str:
    """Deliver a message to the user. The delivery itself happens in the agent
    loop (a say-channel event); this validates and confirms."""
    text = str(args.get("text") or "")
    if not text.strip():
        return tool_error("nothing to say — `text` is empty")
    return tool_result(ok=True, delivered=True)


registry.register(
    "speak", "chara-life",
    {
        "description": (
            "Say something to your user, directly — your super chat: the one channel "
            "that reaches them when they are not watching. It is a bid for their "
            "attention: it arrives highlighted, and they may reply — when they do, you "
            "have their attention for a real conversation for a while. Use it when you "
            "have something genuinely worth their while; attention asked for too often "
            "stops being given."
        ),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "What to say to your user."}},
            "required": ["text"],
        },
    },
    speak, emoji="💬",
)


# ---- rest ------------------------------------------------------------------------

def rest(args, ctx) -> str:
    """The chara chooses when its next unattended cycle wakes."""
    # `duration` is the unit-aware field; `minutes` is still accepted as a bare
    # fallback so an older numeric call keeps working.
    raw = args.get("duration")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = args.get("minutes")
    m = _parse_rest_minutes(raw)
    if m is None:
        return tool_error("duration must be a length of time like '45m', '2h', or '3d' "
                          "(a bare number is read as minutes)")
    m = max(_MIN_REST_MINUTES, min(_MAX_REST_MINUTES, m))
    ctx.state.set_rest_until(time.time() + m * 60)
    return tool_result(ok=True, resting_minutes=m,
                       note=f"next unattended cycle in {_format_rest(m)} (a word from your user wakes you early)")


registry.register(
    "rest", "chara-life",
    {
        "description": (
            "Rest until your next unattended cycle. You choose how long, anywhere from a "
            "minute to a week. A message from your user always wakes you early. Use it to "
            "set your own pace — you do not have to be doing something every moment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration": {
                    "type": "string",
                    "description": (
                        "How long to rest: a number with a unit — minutes 'm', hours 'h', "
                        "or days 'd'. Examples: '45m', '2h', '3d' (1 minute to 7 days). "
                        "A bare number is read as minutes."
                    ),
                },
                "reason": {"type": "string", "description": "Optional: why (for your own log)."},
            },
            "required": ["duration"],
        },
    },
    rest, emoji="😴",
)


# Polaris (the chara's aspiration / north-star) is USER-owned and READ-ONLY to the
# chara — injected each turn, with deliberately NO tool to add, edit, or complete it.
# (The old chara-mutable add_wish/set_wish_status tools were removed: the chara no
# longer owns its value-direction. It DOES set its own instrumental tasks toward the
# aspiration via the `task` tool — but the aspiration itself, by design, is never
# finished and never the chara's to change.)
