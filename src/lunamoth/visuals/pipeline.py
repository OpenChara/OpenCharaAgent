"""The in-app visuals pipeline (R9): card → visual brief (LLM) → image (Seedream)
→ optional matte (R11) → image bytes.

Ports the prompt craft of the dev scripts (``visuals/cardbrief.py`` +
``visuals/genviz.py``) but closes their two gaps:

  - the BRIEF no longer hard-codes Gemini + a separate ``~/.lunamoth/openrouter_key``.
    It runs through an INJECTED ``llm_call(system, user) -> str`` so this module
    never imports ``server/``; the hub passes a closure over the GLOBAL default
    text model + key (the same seam card drafting uses).
  - the IMAGE uses the global image key + model (``tools.builtin._image_gen``, R10);
    the MATTE uses the selected, downloaded model (``visuals.matte``, R11).

No fabrication: every stage surfaces its real error. The matte is honest about
being optional — if the caller asks for it but no model/extra is available, the
generated image is returned un-matted with ``matted=False`` and a note, never a
fake-cut or a silent failure.
"""
from __future__ import annotations

import json
from typing import Callable

from ..tools.builtin import _image_gen
from . import matte as _matte

# --- the brief LLM contract (ported from visuals/cardbrief.py) ----------------

BRIEF_SYSTEM = (
    "You are a world-class character designer for premium anime gacha games (miHoYo Genshin Impact / "
    "Honkai: Star Rail, Hypergryph Arknights). You read a character card and translate its identity, "
    "personality and lore into a concrete, DRAWABLE visual brief that an illustrator could execute "
    "directly. Your north star: the design must be INSTANTLY MEMORABLE — give the character a strong, "
    "readable silhouette and one or two unmistakable signature visual hooks that make them recognizable "
    "at a glance.\n\n"
    "PRINCIPLES:\n"
    "- Stay strictly in-character. Mine the card for concrete, specific details and honor them exactly: "
    "named institutions and their real insignia, specific accessories, signature props, clothing styles, "
    "weapon designs, era and culture, color cues. Never contradict the card (no OOC).\n"
    "- Where the card is sparse on looks, invent tasteful, on-theme detail that amplifies the character's "
    "essence — but keep it consistent with everything the card DOES say.\n"
    "- Translate personality and lore into VISIBLE design: a perfectionist reads in posture and tidy "
    "details; a haunted past reads in a scar or a kept token; a craft reads in the tools they carry.\n"
    "- Be specific and rich: name materials, textures, trims, patterns, hardware, layering, footwear, "
    "hairstyle particulars, eye color, and the exact signature item(s). Avoid generic filler.\n\n"
    "Output STRICT JSON only — no markdown, no commentary — with exactly these keys:\n"
    '  "appearance": 4-7 detailed English sentences describing the canonical look for a full-body anime '
    "illustration: apparent age and build, gender presentation (respect the card; if gender-neutral, make "
    "it a deliberate androgynous design), hairstyle/color, eyes, skin/markings, the FULL layered outfit "
    "with specific colors/materials/trims, footwear, and the SIGNATURE props/accessories/weapon that make "
    "the character iconic. End with a short clause naming the single most memorable visual hook. Describe "
    "only what an artist would draw — no name, no backstory prose.\n"
    '  "palette": a short phrase like "color palette of X, Y and Z" naming 3-4 key colors (include the '
    "signature accent).\n"
    '  "world": one vivid English sentence describing an establishing environment that embodies the '
    "character's world, suitable as a visual-novel / chat background, with NO people.\n"
    '  "theme": a single #RRGGBB hex string for the character\'s primary signature color.\n'
    "The house art style (anime gacha rendering) is added later by the pipeline; describe the CHARACTER "
    "and their world, not the rendering technique."
)

# --- the house art style + per-kind prompt craft (ported from genviz.py) ------

STYLE_REAL = (
    "official gacha mobile game character art, miHoYo Genshin Impact / Honkai: Star Rail "
    "quality and Hypergryph Arknights aesthetic, anime illustration, clean confident lineart, "
    "cel shading with soft gradients, exquisitely detailed ornate costume, highly detailed "
    "expressive eyes, delicate rendering, soft cinematic rim light, refined elegant color "
    "harmony, masterpiece, best quality, ultra detailed"
)
CHIBI = (
    "chibi sticker style, super-deformed, about 2.5 heads tall, big round head, large "
    "sparkling eyes, tiny expressive body, simple thick clean outline, flat bright cel coloring, "
    "adorable kawaii, the SAME character design, outfit and colors as the reference"
)
WHITE_BG = ("a clean flat pure white seamless studio background (#FFFFFF), evenly lit, no colored "
            "light, no cast shadow on the floor")

# kind → generation spec. ``matte`` is the default cutout preference for that kind
# (a full-body sprite wants a transparent cut; a tinted-background avatar does not).
KINDS: dict[str, dict] = {
    "avatar": {"size": "1920x1920", "matte": False},
    "sprite": {"size": "1664x2496", "matte": True},
    "background": {"size": "2304x1280", "matte": False},
}


def card_text(card: dict) -> str:
    d = card.get("data", card)
    parts = [f"NAME: {d.get('name', '')}"]
    for f in ("description", "personality", "scenario"):
        if d.get(f):
            parts.append(f"{f.upper()}: {d[f]}")
    ext = (d.get("extensions", {}) or {}).get("lunamoth", {})
    if ext.get("tagline"):
        parts.append(f"TAGLINE: {ext['tagline']}")
    book = d.get("character_book") or {}
    for e in (book.get("entries") or [])[:8]:
        c = e.get("content", "")
        if c:
            parts.append(f"WORLD-NOTE: {c}")
    return "\n\n".join(parts)


def card_theme(card: dict) -> str | None:
    ext = (card.get("data", card).get("extensions", {}) or {}).get("lunamoth", {})
    theme = ext.get("theme")
    if isinstance(theme, dict) and isinstance(theme.get("primary"), str):
        return theme["primary"]
    return None


def parse_brief(txt: str) -> dict:
    """Parse the brief JSON, tolerating markdown fences / prose around the object."""
    txt = (txt or "").strip()
    if not txt:
        raise RuntimeError("the brief model returned no content")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        i, k = txt.find("{"), txt.rfind("}")
        if i != -1 and k != -1 and k > i:
            return json.loads(txt[i:k + 1])
        raise


def build_brief(card: dict, llm_call: Callable[[str, str], str]) -> dict:
    """Run the brief LLM (injected) and normalize. The card's own theme color wins
    if it declares one (presentation lives on the card)."""
    brief = parse_brief(llm_call(BRIEF_SYSTEM, "Character card:\n\n" + card_text(card)))
    ct = card_theme(card)
    if ct:
        brief["theme"] = ct
    for k in ("appearance", "palette", "world", "theme"):
        brief.setdefault(k, "")
    return brief


def prompt_for(kind: str, brief: dict) -> str:
    a, pal = brief.get("appearance", ""), brief.get("palette", "")
    if kind == "avatar":
        return (f"A cute app avatar icon: chibi bust portrait of the character. {a}. {CHIBI}. "
                f"Close-up of head and shoulders, friendly warm expression, facing the viewer, centered. "
                f"Simple smooth background of a soft gradient tinted {brief.get('theme', '') or '#888'} "
                f"with a faint sparkle. Iconic and clean, no text.")
    if kind == "sprite":
        return (f"A single full-body character standing illustration (full-body character art). {a}. {pal}. "
                f"One elegant three-quarter standing pose, the whole body from head to toe fully inside the "
                f"frame, looking at the viewer, confident relaxed posture. {STYLE_REAL}. {WHITE_BG}, "
                f"no cast shadow, no extra props, no text, no border, centered with generous margin.")
    if kind == "background":
        return (f"Wide environment background art, visual-novel / chat-app backdrop. Scene: "
                f"{brief.get('world', '')}. Atmospheric painterly anime game background, soft depth of field. "
                f"Keep the CENTER open and uncluttered for overlaying chat bubbles, detail pushed to the edges. "
                f"No characters, no people, no text, no logos.")
    raise ValueError(f"unknown visual kind: {kind}")


def generate(
    card: dict,
    kind: str,
    *,
    llm_call: Callable[[str, str], str],
    brief: dict | None = None,
    matte: bool | None = None,
    ark_generate=None,
    download_bytes=None,
) -> dict:
    """Run the pipeline for one *kind* and return
    ``{data: bytes, mime, brief, kind, matted}``.

    Stages: (build) brief via the injected LLM → (gen) Seedream image → (download)
    → (matte) optional transparent cutout. Image/download fns are injectable for
    tests; they default to the R10 Ark client. Raises on a generation/download
    failure (no fake image); the matte is best-effort and reported via ``matted``.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown visual kind: {kind}")
    if not _image_gen.image_key():
        raise RuntimeError(
            "no image key configured — set it in Settings·生图 before generating visuals.")

    brief = brief if brief is not None else build_brief(card, llm_call)
    prompt = prompt_for(kind, brief)
    gen = ark_generate or _image_gen.ark_generate
    dl = download_bytes or _image_gen.download_bytes

    urls = gen(prompt, KINDS[kind]["size"])
    if not urls:
        raise RuntimeError("image generation returned no result")
    data = dl(urls[0])
    if not _image_gen.is_image_bytes(data):
        raise RuntimeError("the generation endpoint did not return an image; nothing was saved")

    matted = False
    note = ""
    want = KINDS[kind]["matte"] if matte is None else bool(matte)
    if want:
        mid = _matte.selected_model()
        if _matte.deps_available() and _matte.is_installed(mid):
            data = _matte.cut(data, model_id=mid)
            matted = True
        else:
            note = ("matte skipped — the visuals extra / a matte model isn't ready "
                    "(install it in Settings·生图).")

    return {"data": data, "mime": "image/png", "brief": brief,
            "kind": kind, "matted": matted, "note": note}
