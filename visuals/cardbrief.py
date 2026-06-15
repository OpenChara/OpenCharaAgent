#!/usr/bin/env python3
"""Card -> visual brief (cached).

An LLM (Gemini 3.1 Pro via OpenRouter) reads a LunaMoth character card and
translates its identity/personality/lore into a concrete, drawable VISUAL brief:
appearance, palette, world scene, theme color. The brief is what the image
pipeline consumes — so the generator never hardcodes who a character is, it
reads the card. Briefs are cached to visuals/cache/<stem>.json (delete to rebuild).

  python cardbrief.py cards/Quinn.en.json        # build/print one brief
  python cardbrief.py cards/Quinn.en.json --force # ignore cache
"""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error
from pathlib import Path

OR_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OR_MODEL = os.environ.get("OPENROUTER_BRIEF_MODEL", "google/gemini-3.1-pro-preview")
CACHE = Path(__file__).parent / "cache"

SYSTEM = (
    "You are a world-class character designer for premium anime gacha games (miHoYo Genshin Impact / "
    "Honkai: Star Rail, Hypergryph Arknights). You read a character card and translate its identity, "
    "personality and lore into a concrete, DRAWABLE visual brief that an illustrator could execute "
    "directly. Your north star: the design must be INSTANTLY MEMORABLE — give the character a strong, "
    "readable silhouette and one or two unmistakable signature visual hooks that make them recognizable "
    "at a glance.\n\n"
    "PRINCIPLES:\n"
    "- Stay strictly in-character. Mine the card for concrete, specific details and honor them exactly: "
    "named institutions and their real insignia (e.g. an MIT character wears genuine MIT motifs), "
    "specific accessories, signature props, clothing styles, weapon designs, era and culture, color cues. "
    "Never contradict the card (no OOC).\n"
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


def _key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        p = Path.home() / ".lunamoth" / "openrouter_key"
        if p.exists():
            k = p.read_text().strip()
    if not k:
        sys.exit("no OPENROUTER_API_KEY (env or ~/.lunamoth/openrouter_key)")
    return k


def _card_text(card: dict) -> str:
    d = card.get("data", card)
    parts = [f"NAME: {d.get('name','')}"]
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


def _card_theme(card: dict) -> str | None:
    ext = (card.get("data", card).get("extensions", {}) or {}).get("lunamoth", {})
    theme = ext.get("theme")
    if isinstance(theme, dict) and isinstance(theme.get("primary"), str):
        return theme["primary"]
    return None


def _llm(card_text: str) -> dict:
    body = {
        "model": OR_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Character card:\n\n" + card_text},
        ],
        "temperature": 0.7,
        "max_tokens": 3000,
    }
    req = urllib.request.Request(OR_ENDPOINT, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_key()}",
        "HTTP-Referer": "https://lunamoth.local",
        "X-Title": "LunaMoth chara visuals",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.loads(r.read())
    txt = (j["choices"][0]["message"].get("content") or "").strip()
    if not txt:
        raise RuntimeError("empty completion (model returned no content)")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        # tolerate markdown fences / prose around the object: take the outermost {...}
        i, k = txt.find("{"), txt.rfind("}")
        if i != -1 and k != -1 and k > i:
            return json.loads(txt[i:k + 1])
        raise


def get_brief(card_path: str | Path, force: bool = False) -> dict:
    card_path = Path(card_path)
    card = json.loads(card_path.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{card_path.stem}.json"
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text())
    brief = _llm(_card_text(card))
    # the card's own theme color wins if it declares one (presentation lives on the card)
    ct = _card_theme(card)
    if ct:
        brief["theme"] = ct
    for k in ("appearance", "palette", "world", "theme"):
        brief.setdefault(k, "")
    cache_file.write_text(json.dumps(brief, ensure_ascii=False, indent=2))
    return brief


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    path = args[0] if args else "cards/Quinn.en.json"
    print(json.dumps(get_brief(path, force=force), ensure_ascii=False, indent=2))
