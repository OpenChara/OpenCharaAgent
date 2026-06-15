#!/usr/bin/env python3
"""Re-cut sprites/stickers with a SEMANTIC matting model (color-agnostic).

Chroma-key fails when the character shares the key color (e.g. Vesper's moss-green
robes on a green screen). A segmentation matter (rembg: ISNet / BiRefNet) cuts the
subject by content, not color, so green/white/blue costumes all work and hair edges
are cleaner. This reprocesses the RAW .jpg outputs already on disk — no re-generation.

  python rematte.py            # re-matte every character in visuals/out
  python rematte.py Vesper K-9 # only these
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from PIL import Image
from rembg import remove, new_session

OUT = Path(__file__).parent / "out"
# isnet-general-use = sharp, reliable general matte; birefnet-general = finer hair (heavier).
# NOTE: localmatte.py (BiRefNet) is the production matter; this stays as a lighter fallback.
_SESSION = None


def _session():  # lazy (don't load the model at import, matching the rest of the pipeline)
    global _SESSION
    if _SESSION is None:
        _SESSION = new_session("isnet-general-use")
    return _SESSION


def matte(src: Path, dst: Path) -> Path:
    im = Image.open(src).convert("RGBA")
    cut = remove(im, session=_session(), post_process_mask=True)
    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cut.save(dst)
    return dst


def crop_grid_matte(sheet: Path, outdir: Path, rows=3, cols=3):
    im = Image.open(sheet).convert("RGB")
    W, H = im.size
    cw, ch = W // cols, H // rows
    pad = int(min(cw, ch) * 0.04)
    outdir.mkdir(parents=True, exist_ok=True)
    for i in range(rows):
        for j in range(cols):
            box = (j * cw + pad, i * ch + pad, (j + 1) * cw - pad, (i + 1) * ch - pad)
            cell = im.crop(box)
            cut = remove(cell.convert("RGBA"), session=_session(), post_process_mask=True)
            bb = cut.getbbox()
            if bb:
                cut = cut.crop(bb)
            cut.save(outdir / f"sticker_{i*cols+j:02d}.png")


def run(name: str):
    base = OUT / name
    if (base / "sprite.jpg").exists():
        matte(base / "sprite.jpg", base / "sprite.png")
        print(f"  {name}: sprite re-matted")
    if (base / "stickers.jpg").exists():
        crop_grid_matte(base / "stickers.jpg", base / "stickers")
        print(f"  {name}: 9 stickers re-matted")


if __name__ == "__main__":
    names = sys.argv[1:] or sorted(p.name for p in OUT.iterdir() if p.is_dir())
    for n in names:
        run(n)
