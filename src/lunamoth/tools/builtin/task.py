"""The `task` tool — the chara manages its own life-threads (tools.task.TaskStore).

A task is a lasting thread the chara carries over time toward its (user-owned)
aspiration — NOT the immediate step-checklist (`todo`) and NOT a value of its own.
Persisted across sessions; the active tasks ride every turn's volatile tail.

Actions: add | update | complete | remove. Completing SEALS a task (immutable
record, shown collapsed in the UI, never re-rendered into the prompt); a sealed
task cannot be edited, reopened, or deleted.
"""
from __future__ import annotations

import json

from ..registry import registry, tool_error


def task(args: dict, ctx) -> str:
    store = getattr(ctx, "task", None)
    if store is None:
        return tool_error("task store not initialized")

    action = str(args.get("action", "")).strip().lower()
    task_id = str(args.get("id", "")).strip()
    content = args.get("content")

    try:
        if action == "add":
            store.add(str(content or ""))
        elif action == "update":
            if not task_id:
                return tool_error("update needs an id")
            store.update(task_id, str(content or ""))
        elif action == "complete":
            if not task_id:
                return tool_error("complete needs an id")
            store.complete(task_id)
        elif action == "remove":
            if not task_id:
                return tool_error("remove needs an id")
            store.remove(task_id)
        else:
            return tool_error("action must be one of: add, update, complete, remove")
    except ValueError as e:
        return tool_error(str(e))

    p = store.payload()
    return json.dumps(
        {"active": p["active"], "done_count": len(p["done"])}, ensure_ascii=False
    )


def check_task_requirements() -> bool:
    return True


TASK_SCHEMA = {
    "description": (
        "Manage your tasks — the lasting threads of your life you carry over time, "
        "each one something you're advancing toward your aspiration. Your active "
        "tasks are always shown to you; you set, edit, finish, and drop them here.\n\n"
        "Actions:\n"
        "- add: start a new thread (content = what you're taking up).\n"
        "- update: reword an active task (id + new content).\n"
        "- complete: mark a task done. This SEALS it — a finished task becomes a "
        "record you can look back on but can no longer change, reopen, or delete.\n"
        "- remove: drop an active task you're setting aside (only active ones; a "
        "sealed task is a record, not deletable).\n\n"
        "Keep your threads few and real, not a scatter of trivia. When you finish "
        "one, choose what comes next toward your aspiration — your aspiration has no "
        "end, so you need never run dry; equally, it's fine to have none for a while.\n\n"
        "This is NOT the same as `todo`: a task is the arc of what you're living "
        "toward and persists across sessions; a todo is just the immediate steps for "
        "what you're doing this session. Use `todo` for the moment's steps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "update", "complete", "remove"],
                "description": "What to do.",
            },
            "id": {
                "type": "string",
                "description": "The task id (e.g. t3), for update/complete/remove.",
            },
            "content": {
                "type": "string",
                "description": "The task description, for add/update.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    "task", "task", TASK_SCHEMA, task,
    check_fn=check_task_requirements, emoji="🎯",
)
