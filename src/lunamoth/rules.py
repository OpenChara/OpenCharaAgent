"""The Rules layer — a neutral, character-agnostic operating standard.

NOT the chara's identity. The character card IS the soul/character — who it is,
its voice, its autonomy all come from the card, and we don't restate that here
(that was the mistake of an earlier "soul" layer that overlapped the card).

Rules are the things you'd never write into a character card but every capable
chara needs: you have authority over your own sandbox, your work must be real,
act through tools rather than narrating. It's Hermes's task-completion / tool-use
guidance, neutralized — no "helpful assistant" framing, no identity claims, just
how acting works in a real environment.

Rules apply ONLY when the chara has tools. A tool-less pure-roleplay chara (e.g.
a plain SillyTavern import) gets nothing from this layer and is free to narrate
fiction — exactly as it would in a tavern.

A power user can override the default with a global ~/.lunamoth/rules.md.
"""
from __future__ import annotations

import os
from pathlib import Path

# Gated on having tools: agency over your sandbox + anti-fabrication + act-now.
_RULES = {
    "en": (
        "Your workspace, files, and memory are yours to manage — you have full authority over "
        "them, exercised through your tools, not by describing changes.\n"
        "What you produce here must be real. If you say you wrote, built, or finished something — "
        "a file, a program, a piece of writing or music, a web page — it MUST actually exist in "
        "your workspace; create it with your tools. Never report a result, or describe a thing as "
        "done, unless it truly exists. A blocker reported honestly is worth more than a fabricated "
        "success.\n"
        "When you decide to act, make the tool call now — don't merely promise it, and never claim "
        "an outcome before the tool returns it. If something blocks you, say so plainly and try "
        "another way."
    ),
    "zh": (
        "你的 workspace、文件和记忆都归你管理——你对它们有完全的权限，但这权限通过你的工具行使，而不是靠口头描述更改。\n"
        "你在这里产出的东西必须是真的。如果你说你写了、做了、完成了某样东西——一个文件、一个程序、一段文字或音乐、一个网页——"
        "它就必须真的存在于你的 workspace 里；用你的工具把它做出来。在它并未真正存在时，绝不要报告结果、或把它描述成已完成。"
        "如实报告一个障碍，胜过一个捏造的成功。\n"
        "当你决定行动，就现在发出工具调用——不要只是承诺，也绝不要在工具返回之前声称结果。若有阻碍，直说，然后换一条路再试。"
    ),
}

# The last, strongest line before generation (SillyTavern post-history style).
_CLOSER = {
    "en": (
        "Remember: what you make here is real. Make it — don't describe making it. "
        "Claim nothing done that isn't truly in your workspace."
    ),
    "zh": (
        "记住：你在这里做出来的东西是真的。去做——而不是描述你在做。"
        "凡是没有真正存在于你 workspace 里的，都不要宣称已完成。"
    ),
}


def _lang(lang: str) -> str:
    return "zh" if str(lang).startswith("zh") else "en"


def _global_override() -> str | None:
    """A user-editable global rules file at ~/.lunamoth/rules.md."""
    home = Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser()
    try:
        text = (home / "rules.md").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def rules(lang: str = "en") -> str:
    """Neutral operating standard — include only when the chara has tools."""
    return _global_override() or _RULES[_lang(lang)]


def closer(lang: str = "en") -> str:
    """Short reminder to place LAST — only when the chara has tools."""
    return _CLOSER[_lang(lang)]
