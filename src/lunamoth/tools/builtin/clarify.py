"""clarify — ask the user a multiple-choice or open-ended question, ported
apple-to-apple from hermes-agent (reference/hermes-agent/tools/clarify_tool.py).

The schema/validation are hermes-identical. The actual UI interaction is
delegated to a platform-provided callback — in LunaMoth this is
``ctx.clarify_hook(question, choices) -> str``, supplied by whichever frontend
is attached (rendered as an arrow-key picker in the TUI, a numbered list on
say-only messaging frontends).

PRESENCE-GATED, like request_permission: a chara that runs unattended cannot
block on a human. When the user is away (or no hook is attached) the tool
returns a clear error that it cannot ask right now — never a fabricated answer.
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..registry import registry, tool_error

# Maximum number of predefined choices. A 5th "Other (type your answer)"
# option is always appended by the UI.
MAX_CHOICES = 4


def clarify(args: dict, ctx) -> str:
    """Ask the user a question, optionally with multiple-choice options."""
    question = str(args.get("question") or "")
    if not question.strip():
        return tool_error("Question text is required.")
    question = question.strip()

    # Validate and trim choices (hermes clarify_tool.py:47-55).
    choices: Optional[List[str]] = args.get("choices")
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        choices = [str(c).strip() for c in choices if str(c).strip()]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    # Presence gate: a resting/unattended chara cannot block on a human.
    if not ctx.state.load().get("user_present", False):
        return tool_error(
            "Cannot ask right now: the user is away. clarify only works while "
            "they are present — make a reasonable default choice yourself, or ask "
            "again once they are back."
        )

    callback = ctx.clarify_hook
    if callback is None:
        return tool_error("Clarify tool is not available in this execution context.")

    try:
        user_response = callback(question, choices)
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"Failed to get user input: {exc}")

    if user_response is None or not str(user_response).strip():
        return tool_error("No response from the user (they did not answer in time).")

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """No external requirements — always available (presence-gated at call time)."""
    return True


CLARIFY_SCHEMA = {
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to present to the user.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "Up to 4 answer choices. Omit this parameter entirely to "
                    "ask an open-ended question. When provided, the UI "
                    "automatically appends an 'Other (type your answer)' option."
                ),
            },
        },
        "required": ["question"],
    },
}


registry.register(
    "clarify", "clarify", CLARIFY_SCHEMA, clarify,
    check_fn=check_clarify_requirements, emoji="❓",
)
