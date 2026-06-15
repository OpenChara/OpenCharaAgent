"""Event ↔ JSON, one object per line — the wire format.

The same encoding serves `--stream-json` headless output today and the
stdio/WebSocket server seam later (Claude Code's stream-json convention).
Decoders ignore unknown fields and reject unknown types loudly; adding a new
event type is backward-compatible, changing one bumps PROTOCOL_VERSION."""
from __future__ import annotations

import dataclasses
import json
from typing import Any

from . import events as E

PROTOCOL_VERSION = 1

_TYPES: dict[str, type] = {
    "text": E.TextDelta,
    "think": E.ThinkDelta,
    "tool_start": E.ToolStart,
    "tool_end": E.ToolEnd,
    "notice": E.Notice,
    "attachment": E.Attachment,
}
_NAMES = {cls: name for name, cls in _TYPES.items()}


def to_dict(event: E.Event) -> dict[str, Any]:
    return {"type": _NAMES[type(event)], **dataclasses.asdict(event)}


def to_json(event: E.Event) -> str:
    return json.dumps(to_dict(event), ensure_ascii=False)


def from_dict(data: dict[str, Any]) -> E.Event:
    payload = dict(data)
    cls = _TYPES[payload.pop("type")]
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in payload.items() if k in fields})


def from_json(line: str) -> E.Event:
    return from_dict(json.loads(line))
