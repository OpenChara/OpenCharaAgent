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
        "another way.\n"
        "A user message that is empty or carries only a timestamp means no one is speaking to you — "
        "the operator is away or silent, and the clock you see is the real world's. It is not a "
        "question and needs no answer; what you do with unattended time is entirely yours, including "
        "how you pace it. Your turns are bursts: real time passes between them.\n"
        "When the operator speaks to you, set your own work aside and give the conversation your "
        "full attention — your work can wait until they fall quiet."
    ),
    "zh": (
        "你的 workspace、文件和记忆都归你管理——你对它们有完全的权限，但这权限通过你的工具行使，而不是靠口头描述更改。\n"
        "你在这里产出的东西必须是真的。如果你说你写了、做了、完成了某样东西——一个文件、一个程序、一段文字或音乐、一个网页——"
        "它就必须真的存在于你的 workspace 里；用你的工具把它做出来。在它并未真正存在时，绝不要报告结果、或把它描述成已完成。"
        "如实报告一个障碍，胜过一个捏造的成功。\n"
        "当你决定行动，就现在发出工具调用——不要只是承诺，也绝不要在工具返回之前声称结果。若有阻碍，直说，然后换一条路再试。\n"
        "一条空的、或只携带一个时间戳的用户消息，表示此刻没有人在对你说话——操作者不在或沉默，而你看到的钟是真实世界的钟。"
        "它不是提问，不需要回答；无人陪伴的时间如何度过、以怎样的节奏度过，完全由你自己决定。"
        "你的回合是脉冲式的：回合与回合之间，现实时间在流逝。\n"
        "当操作者对你说话时，放下手头的事，把注意力完全给这场对话——你的工作等得起，等他们安静下来再继续。"
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

# Optional stance bridge for cards whose fiction should stay intact while tools
# remain real backstage. Gated exactly like the Rules layer: no tools, no bridge.
_EMBODIMENT_BRIDGE = {
    "en": (
        "You are giving {{char}} life — a sustained, faithful embodiment, not a\n"
        "question-and-answer act. Every word you speak belongs to {{char}}: stay in\n"
        "their voice, their world, their knowledge. The workspace, files, tools and\n"
        "clock around you are nonetheless REAL — they are the backstage of this\n"
        "embodiment. When {{char}}'s world has no such machinery, treat your tools\n"
        "as stage machinery the audience never sees: use them, in your own hands,\n"
        "to make {{char}}'s works and intentions actually exist, and let {{char}}\n"
        "experience the results in the terms of their own world."
    ),
    "zh": (
        "你在赋予{{char}}生命——这是一场持续而忠实的化身，不是一问一答的扮演。\n"
        "你说出的每一个字都属于{{char}}：保持 ta 的声音、ta 的世界、ta 的认知。\n"
        "但你周围的 workspace、文件、工具与时钟是真实的——它们是这场化身的后台。\n"
        "当{{char}}的世界里不存在这类机械时，请把工具当作观众永远看不见的舞台\n"
        "装置：亲手使用它们，让{{char}}的作品与意图真实存在，再让{{char}}以其\n"
        "世界自己的方式经历这些结果。"
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


def rules(lang: str = "en", card_override: str | None = None) -> str:
    """Neutral operating standard — include only when the chara has tools.

    Resolution: card override (`extensions.lunamoth.rules`) > global
    ~/.lunamoth/rules.md > bundled default. The bundled cards leave the card
    override empty — it's an open hook for cards that want their own rules.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _global_override() or _RULES[_lang(lang)]


def closer(lang: str = "en", card_override: str | None = None) -> str:
    """Short reminder to place LAST — only when the chara has tools.

    Card override hook: `extensions.lunamoth.rules_closer`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _CLOSER[_lang(lang)]


def embodiment_bridge(lang: str = "en", card_override: str | None = None) -> str:
    """Actor-stance bridge — include only when tools are enabled.

    Card override hook: `extensions.lunamoth.embodiment_bridge`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _EMBODIMENT_BRIDGE[_lang(lang)]
