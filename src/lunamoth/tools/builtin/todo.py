"""todo — the FORCED task list, ported apple-to-apple from hermes-agent
(reference/hermes-agent/tools/todo_tool.py).

This is the model's scratchpad for GETTING WORK DONE: a forced, checkable,
completion-oriented step list for ONE session. It is DISTINCT from the chara's
`task` store (tools.task) — a task is a lasting thread of the chara's life it
advances over time toward its aspiration; a todo is just the immediate steps for
what it's doing right now. Separate tools, separate stores (todo is ephemeral and
in-memory; task is persisted and rendered into the prompt); never conflate them.

State is in-memory, one ``TodoStore`` per session, stashed on the ToolContext.
It survives across tool calls and is rendered back into the conversation after
context compaction via ``format_for_injection`` (only pending/in_progress items
carry through, so the model never re-does finished work).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..registry import registry, tool_error

# Valid status values for todo items
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# Bounds on persisted todo state (hermes todo_tool.py:31-33). The list rides
# through context compaction via format_for_injection, so unbounded content or
# count defeats the compression it rides through.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
_TRUNCATION_MARKER = "… [truncated]"


class TodoStore:
    """In-memory todo list — one instance per session.

    Items are ordered (list position = priority). Each item has:
      - id: unique string identifier (chara-chosen)
      - content: task description
      - status: pending | in_progress | completed | cancelled
    """

    def __init__(self):
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """Write todos. Returns the full current list after writing.

        merge=False replaces the entire list; merge=True updates existing items
        by id (only the provided fields) and appends new ones, preserving order.
        """
        if not merge:
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue  # Can't merge without an id
                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = self._cap_content(str(t["content"]).strip())
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # Rebuild _items preserving order for existing items.
            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        # Bound total item count (keep the highest-priority head).
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """Return a copy of the current list."""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """Render the active task list for post-compaction injection.

        Only pending/in_progress items — completed/cancelled ones cause the
        model to re-do finished work after compression. Returns None when there
        is nothing active to inject.
        """
        if not self._items:
            return None
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }
        active_items = [
            item for item in self._items
            if item["status"] in {"pending", "in_progress"}
        ]
        if not active_items:
            return None
        lines = ["[Your active task list was preserved across context compression]"]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")
        return "\n".join(lines)

    @staticmethod
    def _cap_content(content: str) -> str:
        if len(content) > MAX_TODO_CONTENT_CHARS:
            keep = MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)
            return content[:keep] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """Validate and normalise a todo item to {id, content, status}."""
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"
        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"
        else:
            content = TodoStore._cap_content(content)
        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]


def _store_for(ctx) -> TodoStore:
    """The per-session TodoStore, lazily created and stashed on the context.

    Kept on ``ctx._scratch`` so it survives across tool calls within one
    session without colliding with the ``ctx.todo`` list field's type.
    """
    store = ctx._scratch.get("todo_store")
    if not isinstance(store, TodoStore):
        store = TodoStore()
        ctx._scratch["todo_store"] = store
    return store


def todo(args: dict, ctx) -> str:
    """Read or write the session task list, depending on params."""
    store = _store_for(ctx)
    if store is None:  # defensive — _store_for always returns one
        return tool_error("TodoStore not initialized")

    todos = args.get("todos")
    merge = bool(args.get("merge", False))

    if todos is not None:
        if not isinstance(todos, list):
            return tool_error("todos must be an array of {id, content, status} items")
        items = store.write(todos, merge)
    else:
        items = store.read()

    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """No external requirements — always available."""
    return True


TODO_SCHEMA = {
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "This is your scratchpad for getting ONE job done in THIS session: it is "
        "in-memory and disappears when the session ends. It is distinct from `task` "
        "— your tasks are the lasting threads of your life you carry across sessions "
        "toward your aspiration; a todo is just the immediate steps for what you're "
        "doing right now. Use todo for the moment's steps, `task` for what you're "
        "living toward. Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier",
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


registry.register(
    "todo", "todo", TODO_SCHEMA, todo,
    check_fn=check_todo_requirements, emoji="📋",
)
