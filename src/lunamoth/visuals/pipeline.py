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
    "You are a world-class character designer and art director. You read a character card and translate "
    "its identity, personality and lore into a concrete, DRAWABLE visual brief that an illustrator could "
    "execute directly. Your north star: the design must be INSTANTLY MEMORABLE — give the character a "
    "strong, readable silhouette and one or two unmistakable signature visual hooks that make them "
    "recognizable at a glance.\n\n"
    "CHOOSE THE RENDERING STYLE THAT FITS THIS CHARACTER. Your DEFAULT, recommended look is a polished "
    "anime gacha illustration — miHoYo Genshin Impact / Honkai: Star Rail quality, Hypergryph Arknights "
    "aesthetic, clean confident lineart, expressive cel shading — and most characters read beautifully "
    "that way, so lean toward it unless the card pulls you elsewhere. When the character genuinely calls "
    "for a different medium (grounded modern, historical, noir, horror, painterly, storybook, comic, or "
    "photoreal), render them in the style that actually fits (e.g. cinematic photorealism, oil painting, "
    "ink wash, flat vector, watercolor, pixel art, 3D render); if the card names or implies a medium, era, "
    "or genre, honor it over the default. You are the judge of style — start from the anime default and "
    "depart from it whenever the character's genre, era or tone genuinely fits another medium better; an "
    "explicit instruction in the card is NOT required, trust your judgment.\n\n"
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
    '  "appearance": 4-7 detailed English sentences describing the canonical look for a full-body '
    "illustration: apparent age and build, gender presentation (respect the card; if gender-neutral, make "
    "it a deliberate androgynous design), hairstyle/color, eyes, skin/markings, the FULL layered outfit "
    "with specific colors/materials/trims, footwear, and the SIGNATURE props/accessories/weapon that make "
    "the character iconic. End with a short clause naming the single most memorable visual hook. Describe "
    "only what an artist would draw — no name, no backstory prose, no rendering technique (that goes in "
    '"style").\n'
    '  "style": ONE rich English phrase naming ONLY the rendering style for THIS character — the medium '
    "and technique, linework/shading, lighting quality and finish. Name NO composition, framing, shot-type, "
    'lens or subject words (no "portrait", "close-up", "full-body", "50mm") — those are decided per-image '
    'by the pipeline, and this phrase is reused for the avatar, full-body art AND the scene background. '
    'E.g. "polished anime gacha illustration, clean lineart, cel shading, soft rim light, ultra detailed" '
    'OR "cinematic photorealistic rendering, soft directional key light, fine skin and fabric detail, '
    'filmic grade" OR "painterly storybook gouache, warm and hand-made". Choose it to fit the character, '
    "not a fixed house look. This phrase drives every image the pipeline makes.\n"
    '  "palette": a short phrase like "color palette of X, Y and Z" naming 3-4 key colors (include the '
    "signature accent).\n"
    '  "world": one vivid English sentence describing an establishing environment that embodies the '
    "character's world, suitable as a visual-novel / chat background, with NO people.\n"
    '  "theme": exactly seven characters — a leading # and six hex digits (e.g. #1a6b6b), nothing else.\n'
    "Describe the CHARACTER, their world, and the fitting style — the pipeline composes the final "
    "per-image prompt from these fields."
)

# --- per-kind prompt craft (ported from genviz.py) ----------------------------
# STYLE_REAL / CHIBI are now only DEFAULTS: used when the brief's LM-chosen
# ``style`` is absent (an older cached brief, or a model that skipped the field).
# A style-aware brief always wins, so the chara is no longer locked to anime.

STYLE_REAL = (
    "official gacha mobile game character art, miHoYo Genshin Impact / Honkai: Star Rail "
    "quality and Hypergryph Arknights aesthetic, anime illustration, clean confident lineart, "
    "cel shading with soft gradients, exquisitely detailed ornate costume, highly detailed "
    "expressive eyes, delicate rendering, soft cinematic rim light, refined elegant color "
    "harmony, ultra detailed"
)
CHIBI = (
    "chibi sticker style, super-deformed, about 2.5 heads tall, big round head, large "
    "sparkling eyes, tiny expressive body, simple thick clean outline, flat bright cel coloring, "
    "adorable kawaii, the SAME character design, outfit and colors as the reference"
)
WHITE_BG = ("a clean flat pure white seamless studio background (#FFFFFF), evenly lit, no colored "
            "light, no cast shadow on the floor")

# Shared inline negatives. Not every image provider exposes a negative-prompt field,
# so these guards ride the POSITIVE prompt in natural language (model-agnostic).
_GUARDS_FIGURE = ("No watermark, no signature, no logo, no text or lettering. Avoid extra or missing "
                  "limbs, extra fingers and malformed hands.")
_GUARDS_SCENE = "No text or lettering, no watermark, no signature, no logo."

# Standardized 9-expression set (3x3), fixed left-to-right / top-to-bottom order. Ported
# from the dev pipeline so a sticker sheet is always sliceable into the same nine emotes.
EXPR9 = [
    "calm neutral default face",
    "a warm happy smile",
    "laughing cheerfully, eyes closed",
    "crying with teary eyes, sad",
    "angry with puffed cheeks",
    "wide-eyed surprised and shocked",
    "shy and blushing, glancing away",
    "making a hand-heart, affectionate",
    "a cheerful thumbs up, approving",
]
# Short filename tags aligned to EXPR9 — a sticker is surfaced by writing
# `MEDIA:<…sticker.<tag>.png>`, so the chara reads the emotion from the filename.
EXPR9_TAGS = [
    "neutral", "happy", "laugh", "cry", "angry", "surprised", "shy", "love", "thumbs-up",
]


def sticker_default_names(n: int) -> list[str]:
    """The default per-image name tags for a freshly sliced sheet (first n of the
    standard expression set, then generic fallbacks)."""
    return [EXPR9_TAGS[i] if i < len(EXPR9_TAGS) else f"sticker-{i + 1}" for i in range(n)]

# kind → generation spec. ``matte`` is the default cutout preference for that kind
# (a full-body sprite wants a transparent cut; a tinted-background avatar does not).
# ``grid`` (stickers only) marks a sheet that is sliced into rows*cols cells.
KINDS: dict[str, dict] = {
    "keyvisual": {"size": "4096x2304", "matte": False},
    "avatar": {"size": "1920x1920", "matte": False},
    "sprite": {"size": "1664x2496", "matte": True},
    "stickers": {"size": "2304x2304", "matte": True, "grid": (3, 3)},
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
    """Parse the brief JSON, tolerating ```json fences / prose around the object."""
    txt = (txt or "").strip()
    if not txt:
        raise RuntimeError("the brief model returned no content")
    if txt.startswith("```"):
        # peel a ```json … ``` fence, keep the inside
        parts = txt.split("```")
        if len(parts) >= 2:
            inner = parts[1].strip()
            txt = inner[4:].strip() if inner.lower().startswith("json") else inner
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
    for k in ("appearance", "style", "palette", "world", "theme"):
        brief.setdefault(k, "")
    return brief


def _ext_of(data: bytes) -> str:
    """The true image extension from the magic bytes (so a JPEG/WebP result isn't
    saved with a .png ext and rejected by the asset magic-byte check)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def prompt_for(kind: str, brief: dict, grid: tuple[int, int] | None = None) -> str:
    a, pal = brief.get("appearance", ""), brief.get("palette", "")
    # The LM-chosen rendering style drives every image. Fall back to the polished
    # anime house look ONLY when the brief omitted a style (older briefs / a model
    # that skipped the field), so a style-aware brief is never overridden — this is
    # what unlocks realistic / painterly / other looks instead of forcing 二次元.
    style = (brief.get("style") or "").strip()
    if kind == "keyvisual":
        # The settei / identity-reference sheet. Generated FIRST; its image is then
        # reused as the reference for the other kinds so the character stays one person.
        return (f"A character settei / key-visual reference sheet of ONE single character on a clean "
                f"off-white sheet. {a}. {pal}. {style or STYLE_REAL}. Lay out, clearly separated: one "
                f"large full-body standing pose; a clean FRONT / SIDE / BACK three-view turnaround; one "
                f"dynamic action pose; a small study of the signature props/accessories; and a row of "
                f"color swatches as pure color blocks. The SAME face, hairstyle and outfit repeated "
                f"exactly in every panel — this is ONE person shown several times, never different "
                f"characters. Coherent lighting, crisp and highly detailed; this sheet is the canonical "
                f"identity reference for all the character's other art. No labels, no panel titles, no "
                f"numbers, no UI frames. {_GUARDS_FIGURE}")
    if kind == "stickers":
        rows, cols = grid or KINDS["stickers"]["grid"]
        n = rows * cols
        if n <= 1:
            # A single sticker — no grid, just one centered chibi expression.
            return (f"ONE single chibi expression sticker of the SAME character, {EXPR9[1]}. {a}. "
                    f"{CHIBI}. Centered on a flat pure white background (#FFFFFF) with an even white "
                    f"margin and a subtle soft drop shadow. No text anywhere. {_GUARDS_FIGURE}")
        exprs = EXPR9[:n]
        expr = "; ".join(f"{i+1}) {e}" for i, e in enumerate(exprs))
        return (f"ONE single image: a clean {cols}x{rows} grid of exactly {n} chibi expression stickers "
                f"of the SAME character — no more, no fewer, one per cell. {a}. {CHIBI}. A strict "
                f"{rows}-row by {cols}-column lattice on a flat pure white background (#FFFFFF), with "
                f"generous even white gutters between every cell and a white margin around the whole "
                f"sheet; each cell the same square size. Assign expressions to cells in reading order "
                f"(left to right, top to bottom): {expr}. Render ONLY the facial expression in each cell "
                f"— do NOT draw the numbers, words, captions or labels; the numbering only tells you "
                f"which expression goes where. Each sticker has a subtle soft drop shadow. Consistent "
                f"character across all {n} cells, same colors and design. No text anywhere. {_GUARDS_FIGURE}")
    if kind == "avatar":
        # Avatars are intentionally chibi (a cute app-icon bust) regardless of the
        # character's main art style — a tiny realistic bust reads oddly as an icon.
        return (f"A clean app avatar icon: chibi bust of the SAME character, head and shoulders, "
                f"friendly warm expression, facing the viewer, centered. {a}. {CHIBI}. A single "
                f"character. Simple smooth background of a soft gradient tinted "
                f"{brief.get('theme', '') or '#888'} with a faint sparkle. Iconic and clean. {_GUARDS_FIGURE}")
    if kind == "sprite":
        return (f"A single full-body character standing illustration (full-body character art) of one "
                f"character alone, no other people. {a}. {pal}. One elegant three-quarter standing pose, "
                f"the whole body from head to toe fully inside the frame, looking at the viewer, confident "
                f"relaxed posture. {style or STYLE_REAL}. {WHITE_BG}, no cast shadow, no extra props, no "
                f"border, centered with generous margin. {_GUARDS_FIGURE}")
    if kind == "background":
        render = style or "atmospheric painterly game background"
        return (f"Wide environment background art, visual-novel / chat-app backdrop. Scene: "
                f"{brief.get('world', '')}. Rendered in the same artistic medium and finish as the "
                f"character (matching: {render}) but as a wide establishing SCENE, not a portrait — soft "
                f"depth of field. Keep the CENTER open and uncluttered for overlaying chat bubbles, detail "
                f"pushed to the edges. No characters, no people. {_GUARDS_SCENE}")
    raise ValueError(f"unknown visual kind: {kind}")


def generate(
    card: dict,
    kind: str,
    *,
    llm_call: Callable[[str, str], str],
    brief: dict | None = None,
    matte: bool | None = None,
    refs: list[str] | None = None,
    extra: str = "",
    grid: tuple[int, int] | None = None,
    ark_generate=None,
    download_bytes=None,
) -> dict:
    """Run the pipeline for one *kind* and return
    ``{data: bytes, mime, brief, kind, matted}``.

    Stages: (build) brief via the injected LLM → (gen) Seedream image → (download)
    → (matte) optional transparent cutout. ``refs`` are optional user-provided
    reference images (http(s)/data URIs) that guide generation. Image/download fns
    are injectable for tests; they default to the R10 Ark client. Raises on a
    generation/download failure (no fake image); the matte is best-effort and
    reported via ``matted``.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown visual kind: {kind}")
    if not _image_gen.image_key():
        raise RuntimeError(
            "no image key configured — set it in Settings·生图 before generating visuals.")

    brief = brief if brief is not None else build_brief(card, llm_call)
    # stickers can be a 1x1 / 2x2 / 3x3 sheet; other kinds ignore grid.
    grid_used = (tuple(grid) if grid else KINDS[kind].get("grid")) if kind == "stickers" else None
    prompt = prompt_for(kind, brief, grid=grid_used)
    # An optional per-generation steer (the UI's 额外提示词) — appended so each regen
    # can differ without touching the shared brief.
    extra = (extra or "").strip()
    if extra:
        prompt = f"{prompt} {extra}"

    if ark_generate is not None or download_bytes is not None:
        # Test / explicit-injection path: the old URL-then-download Ark shape.
        gen = ark_generate or _image_gen.ark_generate
        dl = download_bytes or _image_gen.download_bytes
        urls = gen(prompt, KINDS[kind]["size"], refs=refs)
        if not urls:
            raise RuntimeError("image generation returned no result")
        data = dl(urls[0])
        if not _image_gen.is_image_bytes(data):
            raise RuntimeError("the generation endpoint did not return an image; nothing was saved")
    else:
        # Real path: dispatch to the active provider's adapter (validates bytes).
        data = _image_gen.generate_bytes(prompt, KINDS[kind]["size"], refs=refs)

    want = KINDS[kind].get("matte", False) if matte is None else bool(matte)

    # stickers: one sheet → slice into the grid → cut each cell → a LIST of PNGs.
    # The RAW sheet is returned too (kept as a candidate so a bad slice is recoverable);
    # a 1x1 sheet IS the sticker, so no separate sheet is kept.
    if grid_used:
        rows, cols = grid_used
        cells, matted, note = _slice_and_cut(data, rows, cols, want)
        return {"stickers": cells, "names": sticker_default_names(len(cells)),
                "sheet": (data if rows * cols > 1 else None), "grid": [rows, cols],
                "mime": "image/png", "ext": "png",
                "brief": brief, "kind": kind, "matted": matted, "note": note}

    matted = False
    note = ""
    if want:
        mid = _matte.selected_model()
        if _matte.deps_available() and _matte.is_installed(mid):
            data = _matte.cut(data, model_id=mid)  # always PNG (RGBA)
            matted = True
        else:
            note = ("matte skipped — the visuals extra / a matte model isn't ready "
                    "(install it in Settings·生图).")

    ext = "png" if matted else _ext_of(data)
    return {"data": data, "mime": _EXT_MIME.get(ext, "image/png"), "ext": ext,
            "brief": brief, "kind": kind, "matted": matted, "note": note}


def _slice_and_cut(sheet: bytes, rows: int, cols: int, want_matte: bool) -> tuple[list[bytes], bool, str]:
    """Slice a grid sheet into rows*cols cells, cut each to a transparent PNG, and
    compress to the sticker cap. Prefers the semantic matte model; falls back to a
    white-background removal (the sheet is generated on flat white) that floods
    transparency inward from the borders — so interior whites (eyes, highlights) are
    kept and stickers still come out cut without the heavy ``visuals`` extra. Every
    step is best-effort per cell: a cell that can't be cut is kept as-is."""
    from ..content import imaging as _imaging

    cells = _imaging.slice_grid(sheet, rows, cols)
    mid = _matte.selected_model()
    use_matte = want_matte and _matte.deps_available() and _matte.is_installed(mid)
    out: list[bytes] = []
    matted = False
    for c in cells:
        if use_matte:
            try:
                c = _matte.cut(c, model_id=mid)
                matted = True
            except Exception:  # noqa: BLE001 — fall back to white-bg removal for this cell
                try:
                    c = _matte.cut_white_bg(c)
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                c = _matte.cut_white_bg(c)
            except Exception:  # noqa: BLE001 — keep the raw cell if removal fails
                pass
        try:
            c = _imaging.compress_image_bytes(c, "png", _imaging.CAP_STICKER)
        except Exception:  # noqa: BLE001
            pass
        out.append(c)
    note = "" if use_matte else (
        "stickers were cut with the keyless white-background fallback — install a matte "
        "model in Settings·生图 for cleaner cutouts.")
    return out, matted, note
