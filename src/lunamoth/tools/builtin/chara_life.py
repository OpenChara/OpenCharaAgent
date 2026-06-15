"""Chara-life tools — LunaMoth's own, with no hermes counterpart: speak, rest,
wish (add/set — the renamed "goal"), request_permission. These are what make a
chara a living being rather than a workhorse; they stay through the hermes-tool
migration.

`wish` (愿望) is deliberately NOT hermes' `todo`: todo is a forced task-completion
list; a wish is what the character LIVES FOR — its own aspiration, never forced.
"""
from __future__ import annotations

import mimetypes
import time

from ..registry import registry, tool_error, tool_result

_MAX_SEND_BYTES = 8 * 1024 * 1024  # don't push more than ~8MB to the foreground

_MIN_REST_MINUTES = 1
_MAX_REST_MINUTES = 120


# ---- speak -----------------------------------------------------------------------

def speak(args, ctx) -> str:
    """Deliver a message to the user. The delivery itself happens in the agent
    loop (a say-channel event); this validates and confirms."""
    text = str(args.get("text") or "")
    if not text.strip():
        return tool_error("nothing to say — `text` is empty")
    return tool_result(ok=True, delivered=True)


def send_file(args, ctx) -> str:
    """Put a file from your workspace in front of the user. Images render inline;
    anything else is offered as a download. The actual delivery (an attachment
    event) happens in the agent loop — this validates the file and confirms."""
    rel = str(args.get("path") or "").strip()
    if not rel:
        return tool_error("send_file needs `path` — a file inside your workspace")
    try:
        p = ctx.sandbox.resolve_inside(rel, base=ctx.sandbox.workspace_dir)
    except Exception as exc:  # noqa: BLE001 - SandboxViolation / bad path
        return tool_error(f"path not allowed: {exc}")
    if not p.is_file():
        return tool_error(f"no such file in your workspace: {rel}")
    size = p.stat().st_size
    if size > _MAX_SEND_BYTES:
        return tool_error(f"file is too large to send ({size} bytes; limit ~8MB)")
    mime, _ = mimetypes.guess_type(str(p))
    return tool_result(ok=True, path=rel, mime=mime or "application/octet-stream",
                       caption=str(args.get("caption") or ""), bytes=size, delivered=True)


registry.register(
    "send_file", "chara-life",
    {
        "description": (
            "Show the user a file from your workspace — an image you made or saved, a "
            "document, a sticker from assets/. Images appear inline in the conversation; "
            "other files are offered as a download. Give the workspace-relative `path` and "
            "an optional `caption`. Like speak, it reaches the user even when they are away."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path, e.g. assets/stickers/01.png"},
                "caption": {"type": "string", "description": "Optional line shown with the file."},
            },
            "required": ["path"],
        },
    },
    send_file, emoji="🖼️",
)


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
    try:
        m = float(args.get("minutes"))
    except (TypeError, ValueError):
        return tool_error("minutes must be a number")
    m = max(_MIN_REST_MINUTES, min(_MAX_REST_MINUTES, m))
    ctx.state.set_rest_until(time.time() + m * 60)
    return tool_result(ok=True, resting_minutes=m,
                       note=f"next unattended cycle in ~{m:g} min (a word from your user wakes you early)")


registry.register(
    "rest", "chara-life",
    {
        "description": (
            "Rest until your next unattended cycle. You choose how long (1–120 minutes). "
            "A message from your user always wakes you early. Use it to set your own "
            "pace — you do not have to be doing something every moment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "How long to rest, 1–120 minutes."},
                "reason": {"type": "string", "description": "Optional: why (for your own log)."},
            },
            "required": ["minutes"],
        },
    },
    rest, emoji="😴",
)


# ---- wish (the renamed "goal") ----------------------------------------------------

def add_wish(args, ctx) -> str:
    """Record a wish — something you live for, not a task someone set you."""
    if ctx.wishes is None:
        return tool_error("wishes not available")
    text = str(args.get("text") or "").strip()
    if not text:
        return tool_error("a wish needs `text`")
    wish = ctx.wishes.add(text, by="chara")
    return tool_result(ok=True, id=wish["id"], text=wish["text"])


def set_wish_status(args, ctx) -> str:
    if ctx.wishes is None:
        return tool_error("wishes not available")
    wish_id = str(args.get("wish_id") or args.get("goal_id") or "")
    status = str(args.get("status") or "")
    if not wish_id or not status:
        return tool_error("set_wish_status needs `wish_id` and `status`")
    wish = ctx.wishes.set_status(wish_id, status)
    return tool_result(ok=True, id=wish["id"], status=wish["status"], text=wish["text"])


registry.register(
    "add_wish", "chara-life",
    {
        "description": (
            "Add a wish — something you genuinely want, that you live toward in your own "
            "time. A wish is yours; it is not a task anyone forces you to finish (for that, "
            "use the todo tool). Wishes shape how you spend unattended time."
        ),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The wish, in your own words."}},
            "required": ["text"],
        },
    },
    add_wish, emoji="✨",
)

registry.register(
    "set_wish_status", "chara-life",
    {
        "description": "Update a wish you hold: active, done (you reached it), or dropped (you let it go).",
        "parameters": {
            "type": "object",
            "properties": {
                "wish_id": {"type": "string", "description": "The wish id."},
                "status": {"type": "string", "enum": ["active", "done", "dropped"]},
            },
            "required": ["wish_id", "status"],
        },
    },
    set_wish_status, emoji="✨",
)
