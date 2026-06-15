#!/usr/bin/env python3
"""End-to-end character visual builder (DEV / offline tool).

One entry point that takes a character name or a card path and runs the WHOLE
experimental art pipeline, producing a small WEB-OPTIMIZED asset library ready to
drop into a per-character card folder:

  card.json
    -> cardbrief.get_brief        # LLM visual brief (cached, Gemini 3.1 Pro)
    -> genviz.generate            # 5 raw assets (Doubao-Seedream 5.0-lite)
    -> localmatte (BiRefNet)      # cut sprite + sticker sheet to transparent PNG
    -> web variants               # downscaled avatar/sprite/background/keyvisual/stickers
    -> assets.json                # manifest under the keys the card will use

The web/ folder is the ONLY thing meant to ship. We never write into cards/ —
the backend owns that layout; this just produces a web/ folder a human or a
script copies in.

  python build_character.py Quinn                 # full run for cards/Quinn.en.json
  python build_character.py cards/K-9.en.json      # by path
  python build_character.py Quinn --steps web      # only re-derive web variants
  python build_character.py Quinn --steps images   # brief + raw generation only
  python build_character.py Quinn --steps matte    # brief + raw + matte (no web)
  python build_character.py Quinn --out /tmp/quinnweb

KEYS (both optional, DEV-ONLY — not a runtime dependency of LunaMoth):
  - OPENROUTER_API_KEY  (or ~/.lunamoth/openrouter_key)  -> the visual brief LLM
  - ARK_API_KEY         (or ~/.lunamoth/ark_api_key)     -> Volcano Ark image gen
Generation/matting are heavy and offline by design; see visuals/README.md.

Import-safe: importing this module runs nothing. Everything is behind functions
and the __main__ guard.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Reuse the existing pipeline modules rather than duplicating their logic.
from cardbrief import get_brief
import genviz
import localmatte

VISUALS = Path(__file__).resolve().parent
RAW_OUT = VISUALS / "out"          # genviz / localmatte write raw assets here

# The asset keys the card will reference, in the manifest's canonical order.
RAW_ASSETS = ["keyvisual", "sprite", "avatar", "stickers", "background"]
N_STICKERS = 9                     # genviz emits a 3x3 expression sheet

# Web variant targets (the only assets meant to ship).
AVATAR_PX = 256                    # square
SPRITE_LONGEST = 1200              # transparent PNG, longest side
BACKGROUND_W = 1440                # webp, q80
KEYVISUAL_W = 1600                 # webp, q80
STICKER_PX = 256                   # square-ish max, transparent PNG
WEBP_QUALITY = 80


# ------------------------------------------------------------------ helpers ---
def _name_of(card: Path) -> str:
    """cards/Quinn.en.json -> 'Quinn' (matches genviz.generate's naming)."""
    return card.stem.split(".")[0]


def _fit(im, longest: int):
    """Downscale so the longest side <= longest (never upscale). Returns image."""
    from PIL import Image
    w, h = im.size
    scale = longest / max(w, h)
    if scale >= 1.0:
        return im
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def _fit_width(im, width: int):
    """Downscale so width <= `width` (never upscale), preserving aspect."""
    from PIL import Image
    w, h = im.size
    if w <= width:
        return im
    return im.resize((width, max(1, round(h * width / w))), Image.LANCZOS)


def _square_pad(im, px: int):
    """Fit a transparent image into a px*px square canvas, centered (no crop)."""
    from PIL import Image
    im = _fit(im, px)
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    canvas.paste(im, ((px - im.width) // 2, (px - im.height) // 2), im)
    return canvas


# --------------------------------------------------------------- pipeline -----
def step_brief(card: Path, *, force: bool = False) -> dict:
    """card -> cached visual brief (LLM via OpenRouter)."""
    brief = get_brief(card, force=force)
    print(f"[{_name_of(card)}] brief ready: {brief.get('appearance', '')[:80]}…")
    return brief


def step_images(card: Path) -> None:
    """Generate the 5 raw assets into out/<Name>/ (Volcano Ark)."""
    genviz.generate(str(card), RAW_ASSETS)


def step_matte(name: str) -> None:
    """Cut sprite + sticker sheet to transparent PNG at full res (BiRefNet)."""
    localmatte.cut_sprite(name)
    localmatte.cut_stickers(name)


def step_web(name: str, out_dir: Path) -> dict:
    """Derive downscaled web variants from the raw + matted assets.

    Returns the assets.json manifest dict. Skips any source that is missing so a
    partial run still yields a partial (but valid) manifest.
    """
    from PIL import Image, features

    base = RAW_OUT / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stickers").mkdir(parents=True, exist_ok=True)
    webp_ok = features.check("webp")
    manifest: dict = {}

    # avatar: the rounded chibi PNG genviz already produced -> 256x256
    src = base / "avatar.png"
    if src.exists():
        im = Image.open(src).convert("RGBA")
        im = im.resize((AVATAR_PX, AVATAR_PX), Image.LANCZOS)
        im.save(out_dir / "avatar.png")
        manifest["avatar"] = "avatar.png"
        print(f"  web: avatar.png ({AVATAR_PX}x{AVATAR_PX})")
    else:
        print(f"  ! missing {src.name} (run --steps images first)")

    # sprite: matted transparent PNG -> longest side ~1200, transparent
    src = base / "sprite.png"
    if src.exists():
        im = _fit(Image.open(src).convert("RGBA"), SPRITE_LONGEST)
        im.save(out_dir / "sprite.png")
        manifest["sprite"] = "sprite.png"
        print(f"  web: sprite.png ({im.width}x{im.height})")
    else:
        print(f"  ! missing {src.name} (run --steps matte first)")

    # background: raw .jpg -> width ~1440 webp q80 (fallback .png if no webp)
    src = base / "background.jpg"
    if src.exists():
        im = _fit_width(Image.open(src).convert("RGB"), BACKGROUND_W)
        if webp_ok:
            im.save(out_dir / "background.webp", "WEBP", quality=WEBP_QUALITY, method=6)
            manifest["background"] = "background.webp"
        else:
            im.save(out_dir / "background.png")
            manifest["background"] = "background.png"
        print(f"  web: {manifest['background']} (w={im.width})")
    else:
        print(f"  ! missing {src.name} (run --steps images first)")

    # keyvisual: raw .jpg -> width ~1600 webp q80
    src = base / "keyvisual.jpg"
    if src.exists():
        im = _fit_width(Image.open(src).convert("RGB"), KEYVISUAL_W)
        if webp_ok:
            im.save(out_dir / "keyvisual.webp", "WEBP", quality=WEBP_QUALITY, method=6)
            manifest["keyvisual"] = "keyvisual.webp"
        else:
            im.save(out_dir / "keyvisual.png")
            manifest["keyvisual"] = "keyvisual.png"
        print(f"  web: {manifest['keyvisual']} (w={im.width})")
    else:
        print(f"  ! missing {src.name} (run --steps images first)")

    # stickers: matted cells out/<Name>/stickers/sticker_NN.png -> stickers/NN.png ~256
    sticker_paths: list[str] = []
    src_dir = base / "stickers"
    for i in range(N_STICKERS):
        s = src_dir / f"sticker_{i:02d}.png"
        if not s.exists():
            continue
        im = _square_pad(Image.open(s).convert("RGBA"), STICKER_PX)
        rel = f"stickers/{i:02d}.png"
        im.save(out_dir / rel)
        sticker_paths.append(rel)
    if sticker_paths:
        manifest["stickers"] = sticker_paths
        print(f"  web: {len(sticker_paths)} stickers -> stickers/NN.png ({STICKER_PX}px)")
    else:
        print(f"  ! no matted stickers in {src_dir} (run --steps matte first)")

    (out_dir / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"  web: assets.json -> {out_dir / 'assets.json'}")
    return manifest


# --------------------------------------------------------------- driver -------
def build(card_or_name: str, steps: str = "all", out_dir: Path | None = None,
          force_brief: bool = False) -> dict:
    """Run the pipeline for one character. Returns the web assets.json manifest
    (or {} for runs that stop before the web step). Pure orchestration — each
    stage is a small reused function above.

    steps:
      all    -> brief + images + matte + web
      images -> brief + images
      matte  -> brief + images + matte
      web    -> brief + web (re-derive variants from assets already on disk)
    """
    card = genviz.resolve_card(card_or_name)
    name = _name_of(card)
    out_dir = out_dir or (RAW_OUT / name / "web")

    step_brief(card, force=force_brief)

    if steps in ("all", "images", "matte"):
        step_images(card)
    if steps in ("all", "matte"):
        step_matte(name)
    if steps in ("all", "web"):
        return step_web(name, out_dir)
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="build_character.py",
        description="Build a web-optimized visual asset library for one character (DEV tool).",
    )
    ap.add_argument("character", help="character name (e.g. Quinn) or a card.json path")
    ap.add_argument("--steps", choices=["all", "images", "matte", "web"], default="all",
                    help="how far to run (default: all)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output web/ dir (default: visuals/out/<Name>/web/)")
    ap.add_argument("--force-brief", action="store_true",
                    help="ignore the cached visual brief and re-query the LLM")
    args = ap.parse_args(argv)

    manifest = build(args.character, steps=args.steps, out_dir=args.out,
                     force_brief=args.force_brief)
    if manifest:
        print("\nmanifest:\n" + json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
