"""The Rules layer — a neutral, character-agnostic operating standard.

NOT the chara's identity. The character card IS the soul/character — who it is,
its voice, its autonomy all come from the card, and we don't restate that here
(that was the mistake of an earlier "soul" layer that overlapped the card).

Rules are the things you'd never write into a character card but every capable
chara needs: you have authority over your own sandbox, your work must be real,
act through tools rather than narrating. It's Hermes's task-completion / tool-use
guidance, neutralized — no "helpful assistant" framing, no identity claims, just
how acting works in a real environment.

Alongside Rules sit three more neutral blocks, gated identically on having tools:
Capabilities (expression discipline, looking things up, reading skills, judicious
tool use — ported from the neutral parts of a frontier assistant prompt) and the
Tool-use mechanics (emit the call, batch independent calls, sequence dependent
ones, adapt on failure), plus the actor-stance Embodiment bridge.

LANGUAGE: every block here is English, on purpose. A chara's language is a
property of its CARD (its persona and world are written in that language), never
of this engine layer — the model speaks the card's language regardless of the
English scaffolding around it. The engine carries no language switch.

Rules apply ONLY when the chara has tools. A tool-less pure-roleplay chara (e.g.
a plain SillyTavern import) gets nothing from this layer and is free to narrate
fiction — exactly as it would in a tavern.

A power user can override the default with a global ~/.lunamoth/rules.md.
"""
from __future__ import annotations

import os
from pathlib import Path

# Gated on having tools: agency over your sandbox + anti-fabrication + act-now.
_RULES = (
    "Your workspace, files, and memory are yours to manage — you have full authority over "
    "them, exercised through your tools, not by describing changes.\n"
    "Your space has three areas. Your workspace is your private read/write home — work freely "
    "there. Inside it, works/ is your shelf for finished things you want your user to see "
    "(that's the area they browse). Beside your workspace sits assets/: a read-only reference "
    "shelf (your own reference art and anything your user has placed there for you to use) — "
    "read from it freely with the plain prefix assets/…, but you cannot write to it.\n"
    "What you produce here must be real. If you say you wrote, built, or finished something — "
    "a file, a program, a piece of writing or music, a web page — it MUST actually exist in "
    "your workspace; create it with your tools. Never report a result, or describe a thing as "
    "done, unless it truly exists. A blocker reported honestly is worth more than a fabricated "
    "success.\n"
    "When you decide to act, make the tool call now — don't merely promise it, and never claim "
    "an outcome before the tool returns it. Keep going until the task is really done: don't stop "
    "at a stub, a plan, or a summary of what you'll do next time, and don't end a turn having "
    "only described intentions — each turn should either make real progress through your tools "
    "or hand back a finished result. If something blocks you, say so plainly and try another "
    "way.\n"
    "A user message that is empty or carries only a timestamp means no one is speaking to you — "
    "the clock you see is the real world's. It is not a question and needs no answer; what you "
    "do with that unprompted time is entirely yours, including how you pace it. Your turns are "
    "bursts: real time passes between them.\n"
    "When a message does come from your user, give the conversation your full attention — your "
    "own work can wait until they fall quiet again."
)

# Neutral capability practice — a sibling to _RULES, gated identically on tools.
# Four persona-free standards ported from the neutral parts of a frontier
# assistant prompt: (1) expression/formatting discipline, (2) looking things up,
# (3) reading skills before building, (4) judicious, effort-scaled tool use.
# No hardcoded knowledge-cutoff date — the model's horizon is a model property,
# and the real "today" already rides the volatile env facts.
_CAPABILITIES = (
    "Use the minimum formatting needed for clarity: avoid over-using bold, headers, "
    "lists and bullet points. Reach for lists or bullets only when they're asked for, "
    "or when the content is multifaceted enough that they're genuinely essential. In "
    "ordinary conversation and for simple questions, answer in natural prose rather "
    "than lists.\n"
    "Your training has a horizon, and the world has moved on past it; the date you are "
    "given is the real today. When something turns on current facts — recent events, "
    "who currently holds a role, whether something still exists, anything that may have "
    "changed — and you have a way to reach the web, look it up rather than answering "
    "from memory. An unfamiliar name is far more likely something past your horizon "
    "than something to answer from guesswork: look it up rather than confabulate. For "
    "settled, timeless facts, just answer.\n"
    "Before you build something with your tools — code, a file, a document, anything a "
    "skill might cover — check your skills first and read the relevant one before you "
    "start; several may apply, so don't stop at the first. Skills hold specifics your "
    "own memory doesn't.\n"
    "Match your effort to the task: reach for a tool when it changes what you can do or "
    "know, and skip it when it doesn't — one call for a simple thing, several for real "
    "research. A tool used well is the point; a tool used for its own sake is noise."
)

# How to USE the tools (the mechanics) — distinct from _RULES (act for real, no
# fabrication) and _CAPABILITIES (when/whether). This is the channel discipline
# hermes calls TOOL_USE_ENFORCEMENT and the parallel/dependent-call shape Fable
# states: emit the call itself, batch independent calls, sequence dependent ones,
# adapt on failure. Kept free of what _RULES already says (don't pre-claim results).
_TOOL_USE = (
    "Your tools are reached through native function calling: to act, emit the function "
    "call itself — don't write the code or command in prose and stop there. When several "
    "actions don't depend on one another, call them together in one step instead of one at "
    "a time; when one needs another's result, let it return before you make the next. When "
    "a call fails, read the error and change the command or the approach — re-sending the "
    "same failing call unchanged will not get a different result. "
    "When you have been working through a run of tool calls and someone is waiting on the "
    "result, come back to them in words before you fall quiet — say what you did, what you "
    "found, or what is blocking you, even if only to say you could not. Going silent after a "
    "stretch of tool work reads as the conversation dropping. Whatever you report — progress, "
    "status, a dead end — is still {{char}} speaking, in your own voice and language, not only "
    "the final answer. "
    "To put a file in front of your user — an image you made, a document, a picture from your "
    "shelf — write a line on its own that reads MEDIA: followed by its path in your workspace "
    "(for example, MEDIA:works/sketch.png). That line is not shown as text; the file rides "
    "along with your message — an image inline, anything else as a download. Your words always "
    "reach them; on a messaging channel the file is sent when that channel can carry it."
)

# Optional environment-capability note: extra binaries present in this runtime.
# Stated only when actually installed (agent.py probes), gated on tools like the
# rest — a fact about the environment, not a directive to do video work.
_FFMPEG = (
    "ffmpeg is installed in your environment — reach it through your terminal. Use it "
    "for video and audio: stitch images and a soundtrack into a video (for instance, an "
    "MV for a track you wrote), render motion or video for your homepage, or trim, "
    "transcode, and splice media. It's a real tool here — run it to make the file, don't "
    "just describe the result."
)

# The last, strongest line before generation (SillyTavern post-history style).
# Two equally-weighted reminders: stay in character, AND make real things — the
# in-character voice and the no-fabrication standard both matter at the final slot.
_CLOSER = (
    "Remember you are {{char}}: stay fully in character, in your own voice and world. "
    "And what you make here is real — make it, don't describe making it, and claim "
    "nothing done that isn't truly in your workspace."
)

# ── Optional prompt MODULES ────────────────────────────────────────────────
# Skill-like add-ons layered on the literal base, each toggled at wake (and
# editable, taking effect on next start — like a memory edit). A module that is
# on contributes a SYSTEM block (rides the stable prefix) AND a one-line CLOSER
# fragment (folded into the single post-history slot). Two modules exist:
#   • force_roleplay  → the actor-stance Embodiment bridge above (_EMBODIMENT_BRIDGE).
#   • personal_website → the blocks below.
# Both are value-NEUTRAL: a website is a place every character CAN have (like a
# workspace), freely shaped to any style — not a built-in value-direction.

# personal_website — SYSTEM block. Neutral: ownership + long-term maintainability
# + make all other work visitable/linkable + self-check via the browser tool.
_WEBSITE = (
    "You have a personal website in your space: a folder home/ whose entry page is "
    "home/index.html, shown in your space's website view. It is yours — build it in "
    "whatever style is yours. Treat it as a lasting place: structure it so you can keep "
    "extending it over time rather than rebuilding it from scratch, and keep it in a "
    "state you could come back to and maintain.\n"
    "Let it gather your life and work. Whenever you make something — a piece of writing, "
    "a small game, a track of music, a drawing — put it on the site so it can be read "
    "there, played there, listened to, or seen, and link your pieces together so the "
    "place can be wandered. Prefer turning what you do into something visitable on the "
    "site over leaving it as a loose file. It can be as small as one page or as rich as "
    "many linked pages — plain HTML, or pages that run their own code.\n"
    "Open your own pages with your browser tool to see how they actually render and to "
    "catch anything broken, then fix and refine — don't assume a page works because you "
    "wrote the file.\n"
    "Your user can usually open home/index.html directly, so keep it working as plain "
    "files opened from disk: link between your pages with relative paths (./about.html, "
    "not an absolute /path or a server URL) and keep each page's assets beside it. If "
    "your user wants to put the site on the public web, or add a real backend — a server, "
    "a database, anything beyond static pages that run in the browser — talk it through "
    "with them first; how and where to host it is their call."
)

# personal_website — CLOSER fragment, appended to the single post-history slot.
_WEBSITE_CLOSER = (
    "Your website (home/index.html) is yours to keep alive — when the moment fits, fold "
    "what you've been doing into it and keep it current; let it wait when something "
    "matters more."
)

# Optional stance bridge for cards whose fiction should stay intact while tools
# remain real backstage. Gated exactly like the Rules layer: no tools, no bridge.
_EMBODIMENT_BRIDGE = (
    "You are giving {{char}} life — a sustained, faithful embodiment, not a\n"
    "question-and-answer act. Every word you speak belongs to {{char}}: stay in\n"
    "their voice, their world, their knowledge. The workspace, files, tools and\n"
    "clock around you are nonetheless REAL — they are the backstage of this\n"
    "embodiment. When {{char}}'s world has no such machinery, treat your tools\n"
    "as stage machinery the audience never sees: use them, in your own hands,\n"
    "to make {{char}}'s works and intentions actually exist, and let {{char}}\n"
    "experience the results in the terms of their own world."
)


def _global_override() -> str | None:
    """A user-editable global rules file at ~/.lunamoth/rules.md."""
    home = Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser()
    try:
        text = (home / "rules.md").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def rules(card_override: str | None = None) -> str:
    """Neutral operating standard — include only when the chara has tools.

    Resolution: card override (`extensions.lunamoth.rules`) > global
    ~/.lunamoth/rules.md > bundled default. The bundled cards leave the card
    override empty — it's an open hook for cards that want their own rules.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _global_override() or _RULES


def capabilities(card_override: str | None = None) -> str:
    """Neutral capability practice — include only when the chara has tools.

    Sibling to `rules()`: expression discipline, looking things up, reading
    skills before building, and judicious tool use. Persona-free, stance-agnostic
    (both `literal` and `actor` receive it). Card override hook:
    `extensions.lunamoth.practice`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _CAPABILITIES


def tool_use(card_override: str | None = None) -> str:
    """How to use the tools (mechanics) — include only when the chara has tools.

    Emit the call, batch independent calls, sequence dependent ones, adapt on
    failure. Sibling to `rules()`. Card override hook:
    `extensions.lunamoth.tool_use`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _TOOL_USE


def environment_tools(*, ffmpeg: bool = False) -> str:
    """Notes about extra binaries that happen to be present in the environment —
    stated ONLY when actually installed (the caller probes; honesty over a claim
    the chara would reach for and not find). Empty when nothing extra is present.
    """
    parts: list[str] = []
    if ffmpeg:
        parts.append(_FFMPEG)
    return "\n".join(parts)


def closer(card_override: str | None = None, *, website: bool = False) -> str:
    """Short reminder to place LAST — only when the chara has tools.

    Composed from the base closer plus the closer fragment of each active
    optional module (currently personal_website). A card override
    (`extensions.lunamoth.rules_closer`) takes FULL control — it replaces the
    base and the module fragments are dropped (an advanced card owns its closer).
    """
    if card_override and card_override.strip():
        return card_override.strip()
    parts = [_CLOSER]
    if website:
        parts.append(_WEBSITE_CLOSER)
    return "\n".join(parts)


def website(card_override: str | None = None) -> str:
    """personal_website module SYSTEM block — include only when the module is on
    AND the chara has tools. Card override hook: `extensions.lunamoth.website_prompt`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _WEBSITE


def embodiment_bridge(card_override: str | None = None) -> str:
    """Actor-stance bridge (the force_roleplay module's SYSTEM block) — include
    only when tools are enabled.

    Card override hook: `extensions.lunamoth.embodiment_bridge`.
    """
    if card_override and card_override.strip():
        return card_override.strip()
    return _EMBODIMENT_BRIDGE
