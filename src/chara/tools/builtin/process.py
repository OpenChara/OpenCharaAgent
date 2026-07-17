"""`process` tool — manage background processes started by terminal(background=true).

Apple-to-apple with hermes-agent (reference/hermes-agent/tools/process_registry.py
PROCESS_SCHEMA + _handle_process): identical schema and action semantics,
dispatching to OpenCharaAgent's process registry (builtin/_process_registry.py).

Actions: list / poll / log / wait / kill / write / submit / close.
"""
from __future__ import annotations

import json

from ..registry import registry, tool_error
from ._process_registry import get_registry

PROCESS_SCHEMA = {
    "description": (
        "Manage background processes started with terminal(background=true). "
        "Actions: 'list' (show all), 'poll' (check status + new output), "
        "'log' (full output with pagination), 'wait' (block until done or timeout), "
        "'kill' (terminate), 'write' (send raw stdin data without newline), "
        "'submit' (send data + Enter, for answering prompts), 'close' (close stdin/send EOF)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"],
                "description": "Action to perform on background processes",
            },
            "session_id": {
                "type": "string",
                "description": "Process session ID (from terminal background output). Required for all actions except 'list'.",
            },
            "data": {
                "type": "string",
                "description": "Text to send to process stdin (for 'write' and 'submit' actions)",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to block for 'wait' action. Returns partial output on timeout.",
                "minimum": 1,
            },
            "offset": {
                "type": "integer",
                "description": "Line offset for 'log' action (default: last 200 lines)",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return for 'log' action",
                "minimum": 1,
            },
        },
        "required": ["action"],
    },
}


def process(args: dict, ctx) -> str:
    reg = get_registry(ctx)
    action = args.get("action", "")
    # Coerce to string — some models send session_id as an integer.
    raw_sid = args.get("session_id")
    session_id = str(raw_sid) if raw_sid is not None else ""

    if action == "list":
        return json.dumps({"processes": reg.list_sessions()}, ensure_ascii=False)

    if action in {"poll", "log", "wait", "kill", "write", "submit", "close"}:
        if not session_id:
            return tool_error(f"session_id is required for {action}")
        if action == "poll":
            return json.dumps(reg.poll(session_id), ensure_ascii=False)
        if action == "log":
            return json.dumps(
                reg.read_log(session_id, offset=args.get("offset", 0), limit=args.get("limit", 200)),
                ensure_ascii=False,
            )
        if action == "wait":
            interrupted = getattr(ctx, "interrupted", None)
            return json.dumps(
                reg.wait(session_id, timeout=args.get("timeout"), interrupted=interrupted),
                ensure_ascii=False,
            )
        if action == "kill":
            return json.dumps(reg.kill_process(session_id), ensure_ascii=False)
        if action == "write":
            return json.dumps(reg.write_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        if action == "submit":
            return json.dumps(reg.submit_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        if action == "close":
            return json.dumps(reg.close_stdin(session_id), ensure_ascii=False)

    return tool_error(
        f"Unknown process action: {action}. Use: list, poll, log, wait, kill, write, submit, close"
    )


registry.register(
    "process",
    "terminal",
    PROCESS_SCHEMA,
    process,
    emoji="⚙️",
)
