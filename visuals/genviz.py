#!/usr/bin/env python3
"""OpenCharaAgent chara-visual pipeline (experiment).

Input: a character config (canonical appearance + world + palette).
Output, per character, a UNIFIED-LAYOUT set rendered in a shared anime
gacha-game (二游) aesthetic — the character's own genre lives in the *content*,
the house style stays constant:

  keyvisual  主视觉   one sheet: turnaround + expressions + action + palette (16:9)
  sprite     立绘     full-body standing art, chroma-keyed to transparent PNG
  avatar     头像     Q版 chibi bust, rounded-rect icon (unified with stickers)
  stickers   表情包   3x3 = 9 chibi expressions, chroma-keyed + grid-cropped
  background 背景     world-view scene, center kept open (VN / chat backdrop)

Order: keyvisual first (defines the look) -> sprite/avatar/stickers reference it
for identity lock -> background last. Backend: Volcano Ark Doubao-Seedream 5.0-lite.

This is the seed of the future wake-time automation: card -> CHARACTERS entry -> assets.
"""
from __future__ import annotations
import base64, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from cardbrief import get_brief  # card -> cached visual brief (LLM)

CARDS = Path(__file__).resolve().parent.parent / "cards"

# ---------------------------------------------------------------- API ---------
ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-260128")
OUT = Path(__file__).parent / "out"
TEMPLATE = Path(__file__).parent / "template_keyvisual.png"  # blank layout (see template.py)


def _key() -> str:
    k = os.environ.get("ARK_API_KEY")
    if not k:
        p = Path.home() / ".chara" / "ark_api_key"
        if p.exists():
            k = p.read_text().strip()
    if not k:
        sys.exit("no ARK_API_KEY (env or ~/.chara/ark_api_key)")
    return k


def ark_image(prompt: str, size: str, refs: list[str] | None = None,
              max_images: int = 1, timeout: int = 240, tries: int = 3) -> list[str]:
    """Return a list of result image URLs (valid ~24h)."""
    body = {
        "model": MODEL,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    if refs:
        body["image"] = refs
    if max_images > 1:
        body["sequential_image_generation"] = "auto"
        body["sequential_image_generation_options"] = {"max_images": max_images}
    data = json.dumps(body).encode()
    last = ""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(ENDPOINT, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_key()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read())
            return [d["url"] for d in j["data"]]
        except urllib.error.HTTPError as e:
            last = e.read().decode()[:300]
            print(f"  ! HTTP {e.code} (try {attempt}/{tries}): {last}")
            if e.code in (429, 500, 502, 503) and attempt < tries:
                time.sleep(5 * attempt); continue
            break
        except Exception as e:  # noqa
            last = str(e)
            print(f"  ! {last} (try {attempt}/{tries})")
            if attempt < tries:
                time.sleep(5); continue
    raise RuntimeError(f"generation failed: {last}")


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r:
        dest.write_bytes(r.read())
    return dest


def file_to_dataurl(path: Path) -> str:
    b = path.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


# ------------------------------------------------------ image post-process ----
_SESSION = "uninit"


def _matte_session():
    """Lazily create a rembg segmentation session; None if rembg unavailable."""
    global _SESSION
    if _SESSION == "uninit":
        try:
            from rembg import new_session
            _SESSION = new_session("isnet-general-use")
        except Exception as e:  # noqa
            print(f"  (rembg unavailable, falling back to chroma key: {e})")
            _SESSION = None
    return _SESSION


def cutout(src: Path, dst: Path) -> Path:
    """Background removal -> transparent PNG (autocropped).

    Prefers a semantic matting model (rembg/ISNet): color-agnostic, so a
    character that shares the chroma color (e.g. a green-robed mage) is cut
    correctly. Falls back to chroma-key only if rembg isn't installed.
    """
    sess = _matte_session()
    if sess is not None:
        from rembg import remove
        out = remove(Image.open(src).convert("RGBA"), session=sess, post_process_mask=True)
        bbox = out.getbbox()
        if bbox:
            out = out.crop(bbox)
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst)
        return dst
    return chroma_key(src, dst)


def chroma_key(src: Path, dst: Path, despill: bool = True) -> Path:
    """Key out a solid chroma-green background -> transparent PNG, autocrop."""
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    green = (g > 90) & (g - r > 40) & (g - b > 40)
    rgba = np.dstack([a.astype(np.uint8), np.where(green, 0, 255).astype(np.uint8)])
    if despill:  # tame green fringe on kept pixels
        keep = ~green
        over = keep & (g > r) & (g > b)
        rgba[..., 1][over] = np.minimum(rgba[..., 1][over],
                                        ((r + b) // 2)[over].astype(np.uint8))
    out = Image.fromarray(rgba, "RGBA")
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return dst


def rounded_avatar(src: Path, dst: Path, size: int = 512, radius_pct: float = 0.18) -> Path:
    im = Image.open(src).convert("RGBA")
    s = min(im.size)
    im = im.crop(((im.width - s) // 2, (im.height - s) // 2,
                  (im.width - s) // 2 + s, (im.height - s) // 2 + s)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], int(size * radius_pct), fill=255)
    im.putalpha(mask)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst)
    return dst


def crop_grid(src: Path, outdir: Path, rows: int = 3, cols: int = 3,
              key: bool = True) -> list[Path]:
    """Cut a uniform rows x cols sheet into cells; chroma-key each to PNG."""
    im = Image.open(src).convert("RGB")
    W, H = im.size
    cw, ch = W // cols, H // rows
    pad = int(min(cw, ch) * 0.04)  # nudge in from gutters
    outdir.mkdir(parents=True, exist_ok=True)
    cells = []
    for i in range(rows):
        for j in range(cols):
            box = (j * cw + pad, i * ch + pad, (j + 1) * cw - pad, (i + 1) * ch - pad)
            cell = im.crop(box)
            raw = outdir / f"cell_{i*cols+j:02d}.png"
            cell.save(raw)
            if key:
                cutout(raw, outdir / f"sticker_{i*cols+j:02d}.png")
            cells.append(raw)
    return cells


# ----------------------------------------------------------- characters -------
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
# Standardized 9-expression set (3x3), fixed left-to-right / top-to-bottom order
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

GREEN = "a perfectly flat solid chroma-key green screen background (pure #00d000 green), no green color anywhere on the character"
WHITE_BG = "a clean flat pure white seamless studio background (#FFFFFF), evenly lit, no colored light, no cast shadow on the floor"


def prompts(cfg: dict) -> dict:
    a, pal = cfg["appearance"], cfg["palette"]
    return {
        "keyvisual": dict(size="4096x2304", ref="template", max_images=1, prompt=(
            f"Fill in this character-sheet layout template. The reference image is a blank wireframe of "
            f"labelled panels; reproduce the EXACT same layout — same panel positions, sizes, thin rounded "
            f"borders and small corner labels — and paint the SAME character into each panel. "
            f"Panels: FULL BODY = one large full-body standing pose; FRONT / SIDE / BACK = a clean three-view "
            f"turnaround; ACTION POSE = one dynamic pose; PROPS = a small accessory / prop study; "
            f"COLOR PALETTE = fill the five swatch boxes with the character's key colors. "
            f"Subject: {a}. {pal}. {STYLE_REAL}. Identical character design and outfit in every panel; clean "
            f"off-white sheet; coherent lighting; crisp. Keep the panel grid neat and aligned to the template.")),
        "sprite": dict(size="1664x2496", ref=True, max_images=1, prompt=(
            f"A single full-body character standing illustration (full-body character art) of the SAME character. {a}. {pal}. "
            f"One elegant three-quarter standing pose, the whole body from head to toe fully inside the frame, "
            f"looking at the viewer, confident relaxed posture. {STYLE_REAL}. {WHITE_BG}, "
            f"no cast shadow, no extra props, no text, no border, centered with generous margin.")),
        "avatar": dict(size="1920x1920", ref=True, max_images=1, prompt=(
            f"A cute app avatar icon: chibi bust portrait of the SAME character. {a}. {CHIBI}. "
            f"Close-up of head and shoulders, friendly warm expression, facing the viewer, centered. Simple "
            f"smooth background of a soft gradient tinted {cfg['theme']} with a faint sparkle. Iconic and clean, no text.")),
        "stickers": dict(size="2304x2304", ref=True, max_images=1, prompt=(
            f"ONE single image: a perfectly even 3x3 grid of 9 chibi expression stickers of the SAME character. "
            f"{a}. {CHIBI}. Nine cells, each cell exactly the same size with uniform even spacing and clear "
            f"gutters, one clear distinct expression per cell, in this exact order (left to right, top to bottom): "
            f"{'; '.join(f'{i+1}) {e}' for i, e in enumerate(EXPR9))}. Each is a die-cut sticker with a thick clean "
            f"white outline and a subtle drop shadow. {GREEN}. Consistent character across all 9 cells, same colors "
            f"and design. No text captions.")),
        "background": dict(size="4096x2304", ref=False, max_images=1, prompt=(
            f"Wide environment background art, visual-novel / chat-app backdrop. Scene: {cfg['world']}. "
            f"Atmospheric painterly anime game background, soft depth of field. IMPORTANT: keep the CENTER of "
            f"the image open, calm and uncluttered — empty negative space for overlaying chat bubbles — with the "
            f"visual detail and interest pushed toward the edges and corners. No characters, no people, no text, no logos.")),
    }


def resolve_card(name: str) -> Path:
    """Accept a character name (Quinn) or a direct card path."""
    p = Path(name)
    if p.exists():
        return p
    cand = CARDS / f"{name}.en.json"
    if cand.exists():
        return cand
    raise SystemExit(f"no card found for {name!r} (tried {cand})")


def generate(name: str, assets: list[str]):
    card = resolve_card(name)
    name = card.stem.split(".")[0]  # Quinn.en -> Quinn
    cfg = get_brief(card)  # cached card -> visual brief (appearance/palette/world/theme)
    print(f"[{name}] brief: {cfg.get('appearance','')[:90]}…")
    pr = prompts(cfg)
    base = OUT / name
    base.mkdir(parents=True, exist_ok=True)
    kv_ref = None  # URL or data-URL of the keyvisual, used for identity lock
    kv_file = base / "keyvisual.jpg"
    if kv_file.exists() and "keyvisual" not in assets:
        kv_ref = file_to_dataurl(kv_file)  # reuse the existing sheet on disk
        print(f"[{name}] reusing existing keyvisual.jpg as identity reference")
    # keyvisual must run first if any ref-using asset is requested
    order = ["keyvisual", "sprite", "avatar", "stickers", "background"]
    todo = [a for a in order if a in assets]
    for asset in todo:
        spec = pr[asset]
        if asset == "keyvisual":
            # layout-locked by the blank template (structural reference), if present
            refs = [file_to_dataurl(TEMPLATE)] if (spec["ref"] == "template" and TEMPLATE.exists()) else None
            if refs:
                print(f"[{name}] keyvisual (template-locked)…")
        else:
            refs = [kv_ref] if spec["ref"] and kv_ref else None
            if spec["ref"] and not kv_ref:
                # need keyvisual as identity ref; generate it first
                print(f"[{name}] keyvisual (needed as reference)…")
                kvr = [file_to_dataurl(TEMPLATE)] if TEMPLATE.exists() else None
                kv_url0 = ark_image(prompt=pr["keyvisual"]["prompt"], size=pr["keyvisual"]["size"],
                                    refs=kvr, max_images=1)[0]
                download(kv_url0, kv_file)
                kv_ref = file_to_dataurl(kv_file)
                refs = [kv_ref]
        print(f"[{name}] {asset}…")
        url = ark_image(prompt=spec["prompt"], size=spec["size"],
                        refs=refs, max_images=spec["max_images"])[0]
        raw = download(url, base / f"{asset}.jpg")
        if asset == "keyvisual":
            kv_ref = file_to_dataurl(base / "keyvisual.jpg")
        elif asset == "avatar":
            rounded_avatar(raw, base / "avatar.png")
        # sprite & stickers: only the raw .jpg is saved here. Transparency is a
        # separate, controllable step (removebg.py): 1 cut for the sprite + 1 cut
        # for the whole sticker sheet (then local 3x3 crop) = 2 cutouts per character.
        print(f"   saved -> {base}")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "Yan"
    which = sys.argv[2:] or ["keyvisual", "sprite", "avatar", "stickers", "background"]
    generate(name, which)
