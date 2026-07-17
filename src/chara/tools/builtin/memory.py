"""The `memory` tool — hermes-identical single function-calling entry point.

Apple-to-apple port of hermes-agent ``tools/memory_tool.py``'s ``memory_tool``
dispatcher + ``MEMORY_SCHEMA``. Dispatches add/replace/remove against the
per-chara two-store ``MemoryStore`` (``ctx.memory``) and returns the hermes-shaped
JSON (``{success, target, entries, usage, entry_count, message}`` on success, or
``{success: False, error: …}`` with ``current_entries``/``usage`` on over-limit).

There is NO ``read`` action in the schema: live entries surface through every
mutation's success response and the frozen system-prompt snapshot.
"""
from __future__ import annotations

import json

from ..registry import registry, tool_error


def memory(args: dict, ctx) -> str:
    store = getattr(ctx, "memory", None)
    if store is None:
        return tool_error(
            "Memory is not available. It may be disabled in config or this environment.",
            success=False,
        )

    action = str(args.get("action") or "")
    target = str(args.get("target") or "memory")
    content = args.get("content")
    old_text = args.get("old_text")

    if target not in {"memory", "user"}:
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    if action == "add":
        if not content:
            return tool_error("Content is required for 'add' action.", success=False)
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return tool_error("old_text is required for 'replace' action.", success=False)
        if not content:
            return tool_error("content is required for 'replace' action.", success=False)
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return tool_error("old_text is required for 'remove' action.", success=False)
        result = store.remove(target, old_text)

    else:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)

    return json.dumps(result, ensure_ascii=False)


def _check_memory_requirements() -> bool:
    """Memory tool has no external requirements — always available."""
    return True


MEMORY_SCHEMA = {
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later.\n\n"
        "WHEN TO SAVE (do this proactively, don't wait to be asked):\n"
        "- User corrects you or says 'remember this' / 'don't do that again'\n"
        "- User shares a preference, habit, or personal detail (name, role, timezone, coding style)\n"
        "- You discover something about the environment (OS, installed tools, project structure)\n"
        "- You learn a convention, API quirk, or workflow specific to this user's setup\n"
        "- You identify a stable fact that will be useful again in future sessions\n\n"
        "PRIORITY: User preferences and corrections > environment facts > procedural knowledge. "
        "The most valuable memory prevents the user from having to repeat themselves.\n\n"
        "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
        "state to memory; use session_search to recall those from past transcripts.\n"
        "If you've discovered a new way to do something, solved a problem that could be "
        "necessary later, save it as a skill with the skill tool.\n\n"
        "Write memories as DECLARATIVE FACTS, not instructions to yourself. "
        "'User prefers concise responses' ✓ -- 'Always respond concisely' ✗. "
        "'Project uses pytest with xdist' ✓ -- 'Run tests with pytest -n 4' ✗. "
        "Memory is injected every turn, so an imperative gets re-read as a standing directive "
        "in later sessions and can quietly override what your user actually asked for now -- "
        "or compete with your own character. Procedures and workflows belong in skills, not memory.\n\n"
        "TWO TARGETS:\n"
        "- 'user': who the user is -- name, role, preferences, communication style, pet peeves\n"
        "- 'memory': your notes -- environment facts, project conventions, tool quirks, lessons learned\n\n"
        "ACTIONS: add (new entry), replace (update existing -- old_text identifies it), "
        "remove (delete -- old_text identifies it).\n\n"
        "SKIP: trivial/obvious info, things easily re-discovered, raw data dumps, and temporary task state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "string",
                "description": "Short unique substring identifying the entry to replace or remove.",
            },
        },
        "required": ["action", "target"],
    },
}


registry.register(
    "memory", "memory", MEMORY_SCHEMA, memory,
    check_fn=_check_memory_requirements, emoji="🧠",
)
