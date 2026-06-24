"""AI-assisted card material: inspiration→draft, per-field rewrite, transcribe,
and assembling a draft into a V3 card object.

The LLM helpers (``draft_card_from_inspiration``, ``rewrite_card_field``,
``transcribe_card``) reach ``_complete`` through the hub package (``_pkg``) so a
test patching ``H._complete`` is honored.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ...content.cards import detect_language
from ...content.knobs import normalize_force_roleplay
from ..dispatch import RpcError
from ._common import HubRpcError, _clean_theme


def _pkg():
    from .. import hub
    return hub


# ---- AI-assisted card drafts --------------------------------------------------

_CARD_DRAFT_SYSTEM = """You draft editable SillyTavern/LunaMoth character-card material from a user's inspiration.
The human is the author: preserve their ideas, names, relationships, tone, taboos, and wording where possible.
Do not contradict the inspiration. If a detail is missing, choose conservative, editable placeholder-like detail.
Write the persona and all prose in the SAME LANGUAGE as the user's inspiration.

Reply with STRICT JSON ONLY: one object, no markdown, no comments, no trailing prose.
The object must have exactly these keys:
{
  "name": string,
  "user_name": string,
  "description": string,
  "personality": string,
  "scenario": string,
  "first_mes": string,
  "world_entries": [{"keys": [string, ...], "content": string, "constant": boolean}],
  "polaris": string,
  "tagline": string,
  "theme_color": string,
  "theme_color_2": string
}

Requirements:
- user_name: who "you" — the human who will talk to this character — ARE inside this world: a short name or role and your relationship to the character. Use whatever the inspiration says about the reader / "you". If the inspiration does NOT say who you are, do NOT invent a second protagonist: assign a neutral, moderate role that simply fits the world — name it neutrally (e.g. "friend" / "朋友") and make "you" an ordinary person of this world. Never leave it empty.
- description: the character persona, 150-400 words when the language uses spaces; for CJK, a similarly rich 2-5 paragraphs. Convey the character's goals and motivations, not just appearance.
- personality: a concise distillation of the character's temperament and traits (a phrase or a few sentences).
- scenario: the current situation / setting the character is in right now (1-3 sentences).
- first_mes: an opening message in character — the FIRST thing the character says, in their own voice.
- world_entries: the character's world book — 6-8 lorebook entries that give the character real substance to draw on (a thin world makes a thin character, so be generous and concrete; never return an empty or one-line world). Cover the things that actually shape THIS character: the core setting/premise, key people and relationships, places, history and formative events, factions or groups, signature items, the character's domains of knowledge or interest, and their rules/boundaries. Each entry: `keys` = 3-6 short trigger words or names that would surface it in conversation; `content` = a substantial, self-contained paragraph (roughly 2-5 sentences) written as reference notes in the character's world, not a single short phrase. Mark the 1-2 always-relevant core entries (the central premise, the character's grounding) constant=true; the rest constant=false (keyword-triggered).
- polaris: the character's North Star — ONE grand, somewhat abstract ideal it lives toward but can never fully reach or finish (not a task list, not a small goal). A single sentence in the character's spirit. May be "" if none fits.
- tagline: one line.
- theme_color: the character's PRIMARY signature color, a hex like "#5B9FD4".
- theme_color_2: a SECONDARY accent color (another hex) that pairs with theme_color to form
  the character's two-color gradient — pick a complementary or analogous accent, not the same color.
The avatar is NOT generated here — the human uploads one or generates it on demand later."""

_THEME_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _invalid_draft(message: str) -> HubRpcError:
    return HubRpcError(-32050, f"the model returned an invalid draft: {message}",
                       {"kind": "draft_schema", "detail": message})


def _theme_color(value: Any) -> str:
    if not isinstance(value, str) or not _THEME_RE.match(value.strip()):
        raise _invalid_draft("theme_color must be a #RRGGBB hex color")
    return value.strip().upper()


def _derive_secondary(primary: str) -> str:
    """A pleasing accent paired with `primary` for the two-color gradient — used when the
    model omits theme_color_2 so a character ALWAYS gets two colors. Analogous hue shift
    + a touch lighter (stdlib colorsys, no deps)."""
    import colorsys
    r, g, b = (int(primary[1:3], 16), int(primary[3:5], 16), int(primary[5:7], 16))
    h, lum, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    r2, g2, b2 = colorsys.hls_to_rgb((h + 0.08) % 1.0, min(1.0, lum + 0.12), s)
    return "#%02X%02X%02X" % (round(r2 * 255), round(g2 * 255), round(b2 * 255))


def _theme_color_2(value: Any, primary: str) -> str:
    """The secondary accent: the model's value if it's a valid, DISTINCT hex, else a
    derived accent — so the gradient is never a flat single color."""
    if isinstance(value, str) and _THEME_RE.match(value.strip()):
        v = value.strip().upper()
        if v != primary:
            return v
    return _derive_secondary(primary)


def _string_field(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid_draft(f"{key} must be a non-empty string")
    return value.strip()


_MAX_WORLD_ENTRIES = 10
_MAX_WORLD_CONSTANTS = 3


def _validate_world_entries(value: Any) -> list[dict[str, Any]]:
    """Lenient: keep the well-formed entries (cap 10, a few constants allowed) and
    skip the rest. An empty or odd-sized list is fine — a card may simply have
    little world. Generation must NOT fail because the model returned the wrong
    count; richer is better, so the cap is generous rather than tight."""
    out: list[dict[str, Any]] = []
    constants = 0
    if not isinstance(value, list):
        return out
    for entry in value:
        if len(out) >= _MAX_WORLD_ENTRIES or not isinstance(entry, dict):
            continue
        keys = entry.get("keys")
        clean_keys = [str(k).strip() for k in keys if isinstance(k, str) and str(k).strip()] if isinstance(keys, list) else []
        content = entry.get("content")
        if not clean_keys or not isinstance(content, str) or not content.strip():
            continue
        constant = bool(entry.get("constant")) and constants < _MAX_WORLD_CONSTANTS
        constants += 1 if constant else 0
        out.append({"keys": clean_keys[:6], "content": content.strip(), "constant": constant})
    return out


def _validate_polaris(value: Any) -> str:
    """The character's North Star — ONE grand ideal as a single string. Lenient:
    a list (legacy/loose model output) is folded into one; empty is fine."""
    if isinstance(value, list):
        value = "；".join(str(g).strip() for g in value if isinstance(g, str) and str(g).strip())
    return str(value or "").strip()[:1000]


# Who "you" are in the world, when the model leaves it blank: a neutral, moderate
# ordinary-person role in the card's language (never empty — the operator name is
# fixed at wake and must always resolve to something).
_DEFAULT_USER_BY_LANG = {"zh": "朋友", "en": "friend"}


def _validate_user_name(value: Any, lang: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _DEFAULT_USER_BY_LANG.get(lang, "friend")


def _parse_card_draft(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise HubRpcError(
            -32050,
            f"the model did not return strict JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})",
            {"kind": "draft_json", "detail": str(exc)},
        ) from exc
    if not isinstance(obj, dict):
        raise _invalid_draft("top-level JSON must be an object")
    # Tolerant schema: the essentials must be present (else it's not a card draft),
    # extra keys are rejected (a wholly-wrong/parallel schema), but the rest may be
    # absent and are defaulted — generation should not fail on a small deviation.
    required = {"name", "description"}
    allowed = required | {"user_name", "personality", "scenario", "first_mes",
                          "world_entries", "polaris", "tagline", "theme_color", "theme_color_2"}
    got = set(obj)
    missing = required - got
    extra = got - allowed
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"unexpected: {', '.join(sorted(extra))}")
        raise _invalid_draft("draft keys must match the requested schema (" + "; ".join(parts) + ")")
    name = _string_field(obj, "name")
    description = _string_field(obj, "description")
    opt = lambda k: str(obj.get(k) or "").strip()  # noqa: E731 — soft string field
    # The card's language drives the neutral user_name fallback (朋友 / friend).
    lang = detect_language(text=f"{description} {name}")
    draft = {
        "name": name,
        "user_name": _validate_user_name(obj.get("user_name"), lang),
        "description": description,
        "personality": opt("personality"),
        "scenario": opt("scenario"),
        "first_mes": opt("first_mes"),
        "world_entries": _validate_world_entries(obj.get("world_entries")),
        "polaris": _validate_polaris(obj.get("polaris")),
        "tagline": opt("tagline"),
        "theme_color": _theme_color(obj.get("theme_color")),
        "theme_color_2": _theme_color_2(obj.get("theme_color_2"), _theme_color(obj.get("theme_color"))),
    }
    # No avatar is drafted — it's a manual upload/generate step (stored as a sidecar).
    return draft


def draft_card_from_inspiration(defaults: dict[str, str], inspiration: str, model: str = "") -> dict[str, Any]:
    text = inspiration.strip()
    if not text:
        raise RpcError(-32602, "cards.draft needs inspiration")
    raw = _pkg()._complete(
        defaults,
        _CARD_DRAFT_SYSTEM,
        text,
        model=model,
        max_tokens=4096,
        temperature=0.75,
        response_format={"type": "json_object"},
    )
    if not raw.strip():
        raise HubRpcError(-32050, "the model returned an empty draft", {"kind": "draft_json", "detail": "empty response"})
    return _parse_card_draft(raw)


# ---- standalone world-book generation / expansion -------------------------------

_WORLDBOOK_SYSTEM = """You are a world-book author for a SillyTavern/LunaMoth character card.
A world book is a set of lorebook entries — reference notes the character can draw on, each \
surfaced by trigger keywords during play. Given the character below, write a rich, concrete world book.

Write every entry in the SAME LANGUAGE as the character's description.

Reply with STRICT JSON ONLY — one object, no markdown, no commentary:
{"entries": [{"keys": [string, ...], "content": string, "constant": boolean}]}

Each entry:
- keys: 3-6 short trigger words or names (people, places, terms) that would naturally surface this entry in conversation.
- content: a substantial, self-contained paragraph (roughly 2-5 sentences) of reference notes set in the character's world — not a one-liner.
- constant: true for an always-relevant core entry (the central premise / the character's grounding), false for the rest. Mark only 1-2 as constant.

Cover the things that actually shape THIS character: core setting/premise, key people and relationships, \
places, history and formative events, factions or groups, signature items, the character's domains of \
knowledge or interest, and their rules/boundaries. Be generous and concrete — a thin world makes a thin character."""


def _existing_summary(existing: Any) -> list[str]:
    """One short bullet per existing entry, so an expand call can avoid repeats."""
    out: list[str] = []
    for e in (existing if isinstance(existing, list) else [])[:_MAX_WORLD_ENTRIES]:
        if not isinstance(e, dict):
            continue
        raw_keys = e.get("keys") or ([e.get("key")] if e.get("key") else [])
        keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        content = str(e.get("content") or e.get("desc") or "").strip()
        if keys and content:
            out.append(f"- [{', '.join(keys)}] {content[:160]}")
    return out


def generate_worldbook(defaults: dict[str, str], *, name: str = "", description: str = "",
                       personality: str = "", scenario: str = "", first_mes: str = "",
                       existing: Any = None, mode: str = "fresh", count: int = 8,
                       model: str = "") -> dict[str, Any]:
    """Generate (or, with ``mode="expand"``, extend) a character's world book from its
    persona/scenario. Returns ``{"entries": [{keys, content, constant}]}`` — the same
    entry shape ``cards.draft`` emits. No fallback: an empty/invalid model reply errors."""
    if not any(s.strip() for s in (name, description, personality, scenario, first_mes)):
        raise RpcError(-32602, "card.generate_worldbook needs some character content")
    want = max(1, min(int(count or 8), _MAX_WORLD_ENTRIES))
    parts: list[str] = []
    if name.strip():
        parts.append(f"Name: {name.strip()}")
    if description.strip():
        parts.append(f"Description:\n{description.strip()[:2000]}")
    if personality.strip():
        parts.append(f"Personality:\n{personality.strip()[:800]}")
    if scenario.strip():
        parts.append(f"Scenario:\n{scenario.strip()[:800]}")
    if first_mes.strip():
        parts.append(f"Opening line:\n{first_mes.strip()[:600]}")
    have = _existing_summary(existing)
    if mode == "expand" and have:
        parts.append("Existing entries (do NOT repeat these — write DIFFERENT ones):\n" + "\n".join(have))
        parts.append(f"\nWrite about {want} NEW world-book entries that expand this world without duplicating the existing ones.")
    else:
        parts.append(f"\nWrite a world book of about {want} entries for this character.")
    raw = _pkg()._complete(
        defaults, _WORLDBOOK_SYSTEM, "\n\n".join(parts),
        model=model, max_tokens=4096, temperature=0.8,
        response_format={"type": "json_object"},
    )
    if not raw.strip():
        raise HubRpcError(-32050, "the model returned an empty world book",
                          {"kind": "worldbook", "detail": "empty response"})
    try:
        obj = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise HubRpcError(-32050, f"the model did not return strict JSON ({exc.msg} at line {exc.lineno})",
                          {"kind": "worldbook_json", "detail": str(exc)}) from exc
    entries_raw = obj.get("entries") if isinstance(obj, dict) else obj
    return {"entries": _validate_world_entries(entries_raw)}


# ---- per-field AI edit (natural-language rewrite of ONE card field) -------------

_FIELD_REWRITE_SYSTEM = (
    "You are editing ONE field of a SillyTavern/LunaMoth character card. Rewrite just "
    "that field. Keep the SAME language as the current text. Preserve the character's "
    "established name, voice, world and facts unless the instruction says otherwise. "
    "Return ONLY the new field text — no quotes, no markdown, no labels, no commentary."
)

# Human-readable shape hint per field, so the model returns the right kind of text.
_FIELD_REWRITE_LABEL = {
    "name": "the character's name (a short name)",
    "description": "the character persona/description (rich prose)",
    "personality": "the character's personality (concise traits)",
    "scenario": "the scene/setting the character is in",
    "first_mes": "the character's opening message, in their own voice",
    "tagline": "a one-line tagline",
    "user_name": "who YOU (the human) are in this world — a short name or role",
    "user_persona": "a short description of who YOU (the human) are to the character",
    # key stays "goals" to match the editor's field key; the description guides the
    # AI rewrite toward a Polaris (one grand, never-finished ideal), not a list.
    "goals": "the character's North Star — one grand, never-finished ideal it lives toward",
    "world_entries": "world-book lorebook entries, one per line as 'key1, key2 — content'",
}


def rewrite_card_field(defaults: dict[str, str], field: str, value: str = "",
                       instruction: str = "", context: str = "", model: str = "") -> dict[str, Any]:
    """Rewrite ONE card field with the LLM. Empty instruction = free rephrase of the
    current value; a non-empty instruction steers the change. Returns {field, text}.
    No fallback: a failed/empty model call surfaces as a visible error."""
    field = (field or "").strip()
    if not field:
        raise RpcError(-32602, "card.rewrite_field needs a field")
    value = value if isinstance(value, str) else ""
    label = _FIELD_REWRITE_LABEL.get(field, f"the '{field}' field")
    directive = (instruction or "").strip() or (
        "Rephrase it freely — keep the meaning and language, but improve the wording and flavor."
    )
    parts = [f"Field: {label}"]
    if (context or "").strip():
        parts.append(f"\nCharacter context (do not rewrite this, just for consistency):\n{context.strip()}")
    parts.append(f"\nCurrent text:\n{value.strip() or '(empty)'}")
    parts.append(f"\nInstruction: {directive}")
    raw = _pkg()._complete(defaults, _FIELD_REWRITE_SYSTEM, "\n".join(parts),
                           model=model, max_tokens=2048, temperature=0.9)
    text = _strip_text_fence(raw).strip()
    if not text:
        raise HubRpcError(-32050, "the model returned an empty rewrite",
                          {"kind": "rewrite", "detail": "empty response"})
    return {"field": field, "text": text}


def _strip_text_fence(raw: str) -> str:
    """Drop a ```...``` fence the model may wrap text in, despite instructions."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


# ---- natural language -> card draft ----------------------------------------------

_TRANSCRIBE_SYSTEM = """You turn a person's free-form description of an original character (OC) \
into a structured character card. Write in the SAME LANGUAGE as the user's text. Preserve their \
ideas and wording where possible — you are a careful editor, not a co-author. Fill gaps \
conservatively and tastefully; never invent contradictions. Reply with ONLY a JSON object, \
no markdown fence, with exactly these keys:
{"name": str, "appearance": str, "personality": str, "scenario": str, "first_mes": str,
 "alternate_greetings": [str], "world": [{"key": str, "desc": str, "constant": bool}],
 "relationship": str, "polaris": str, "rules": str}
- appearance: who they are + how they look, 2-4 sentences, prose.
- personality: temperament and voice, 2-4 sentences, prose.
- first_mes: their in-character opening line when meeting the user.
- world: 2-5 lorebook entries (key = a name/term, desc = one sentence); constant=true for at most one core entry.
- relationship: the user's place in this character's life, 1-2 sentences.
- polaris: ONE grand, never-finished ideal the character lives toward (a single sentence), else "".
- rules: boundaries/never-dos if implied, else ""."""


def transcribe_card(defaults: dict[str, str], text: str, model: str = "") -> dict[str, Any]:
    raw = _pkg()._complete(defaults, _TRANSCRIBE_SYSTEM, text.strip(), model=model)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RpcError(-32050, f"the model did not return a usable draft ({exc})") from exc
    if not isinstance(draft, dict) or not draft.get("name"):
        raise RpcError(-32050, "the model did not return a usable draft")
    return draft


def _draft_world_entries(draft: dict[str, Any]) -> list[dict[str, Any]]:
    source = draft.get("world_entries") if isinstance(draft.get("world_entries"), list) else draft.get("world")
    out: list[dict[str, Any]] = []
    for i, w in enumerate(source or []):
        if not isinstance(w, dict):
            continue
        raw_keys = w.get("keys")
        if isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        else:
            key = str(w.get("key") or "").strip()
            keys = [key] if key else []
        content = str(w.get("content") if "content" in w else w.get("desc", "")).strip()
        if not keys or not content:
            continue
        out.append({
            "id": i,
            "keys": keys[:6],
            "content": content,
            "constant": bool(w.get("constant")),
            "enabled": True,
            "insertion_order": i,
        })
    return out


def _draft_polaris(draft: dict[str, Any]) -> str:
    """The drafted Polaris — a single north-star string. Prefer an explicit
    `polaris`; fold a legacy/loose `seed_goals`/`goals` list into one if that's
    all the model returned."""
    pol = draft.get("polaris")
    if isinstance(pol, str) and pol.strip():
        return pol.strip()[:1000]
    legacy = draft.get("seed_goals") if isinstance(draft.get("seed_goals"), list) else draft.get("goals")
    if isinstance(legacy, list):
        return "；".join(str(g).strip() for g in legacy if str(g).strip())[:1000]
    return ""


def draft_to_card(draft: dict[str, Any], origin_text: str = "", as_draft: bool = False) -> dict[str, Any]:
    """Assemble a V3 card object from a (possibly user-edited) draft."""
    world_entries = _draft_world_entries(draft)
    ext: dict[str, Any] = {"origin": origin_text[:8000]}
    if as_draft:
        ext["draft"] = True
    polaris = _draft_polaris(draft)
    if polaris:
        ext["polaris"] = polaris
    if draft.get("rules"):
        ext["rules"] = str(draft["rules"])
    if draft.get("tagline"):
        ext["tagline"] = str(draft["tagline"]).strip()
    # Who "you" are in this world (the SillyTavern persona convention) rides the card.
    if str(draft.get("user_name") or "").strip():
        ext["user_name"] = str(draft["user_name"]).strip()
    if str(draft.get("user_persona") or "").strip():
        ext["user_persona"] = str(draft["user_persona"]).strip()
    # Two-color theme for the character's gradient (primary signature + secondary accent).
    # Derive a secondary when one wasn't supplied, so a card always has the dual gradient.
    tc1 = str(draft.get("theme_color") or "").strip()
    tc2 = draft.get("theme_color_2")
    if tc1 and _THEME_RE.match(tc1):
        tc2 = _theme_color_2(tc2, tc1.upper())
    theme = _clean_theme({"primary": tc1, "secondary": tc2}, tc1)
    if theme:
        ext["theme"] = theme
    # No avatar from the draft — it's a manual upload/generate step (sidecar).
    # The card FIELD is a boolean force_roleplay (True ≡ the old "actor" stance).
    # Accept the boolean from a UI draft, or a legacy `embodiment: "actor"` string.
    forced = normalize_force_roleplay(draft.get("force_roleplay"))
    if forced is None and str(draft.get("embodiment") or "").lower() == "actor":
        forced = True
    if forced:
        ext["force_roleplay"] = True

    description = str(draft.get("description") if draft.get("description") is not None else draft.get("appearance", ""))
    data: dict[str, Any] = {
        "name": str(draft.get("name", "")),
        "description": description,
        "personality": str(draft.get("personality", "")),
        "scenario": str(draft.get("scenario", "")) + (
            ("\n\n" + str(draft["relationship"])) if draft.get("relationship") else ""),
        "first_mes": str(draft.get("first_mes", "")),
        "mes_example": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [str(g) for g in (draft.get("alternate_greetings") or [])][:4],
        "creator_notes": str(draft.get("tagline", "")),
        "tags": ["original"],
        "extensions": {"lunamoth": ext},
    }
    if world_entries:
        data["character_book"] = {"name": f"{data['name']} world", "entries": world_entries}
    if detect_language(text=description + " " + data["first_mes"]) == "zh" and "中文" not in data["tags"]:
        data["tags"].append("中文")
    return {"version": "1.0", "name": data["name"], "data": data}
