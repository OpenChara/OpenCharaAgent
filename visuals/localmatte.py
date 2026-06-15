#!/usr/bin/env python3
"""Local FLAGSHIP matting — BiRefNet via rembg, full-resolution, SOTA edges.

Unlike the remove.bg free tier (which caps output at ~0.25MP preview → blurry),
this runs locally on the FULL generated resolution, so the transparent PNGs stay
sharp. BiRefNet is the current state-of-the-art matting model; far cleaner hair /
fine-edge handling than U2-Net/ISNet. Includes green-spill suppression for sources
generated on a chroma background.

  python localmatte.py sprites [Names...]    # cut 立绘 at full res
  python localmatte.py stickers [Names...]   # cut sheet at full res, crop 3x3
  python localmatte.py all [Names...]        # both  (default: all characters)
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from PIL import Image
from rembg import remove, new_session

OUT = Path(__file__).parent / "out"
MODEL = "birefnet-general"   # flagship; falls back to lite if unavailable
_SESS = None


def sess():
    global _SESS
    if _SESS is None:
        try:
            _SESS = new_session(MODEL)
        except Exception as e:  # noqa
            print(f"  ({MODEL} unavailable: {e}; trying birefnet-general-lite)")
            _SESS = new_session("birefnet-general-lite")
    return _SESS


def _despill(rgba: Image.Image) -> Image.Image:
    """Suppress green fringe left by a chroma background on kept (opaque) pixels."""
    a = np.asarray(rgba).astype(np.int16)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    keep = al > 0
    over = keep & (g > r) & (g > b)
    a[..., 1][over] = np.maximum(r, b)[over]
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def _matte(src: Path, despill: bool = True) -> Image.Image:
    out = remove(Image.open(src).convert("RGBA"), session=sess(),
                 post_process_mask=True)
    if despill:
        out = _despill(out)
    return out


def cut_sprite(name: str):
    base = OUT / name
    src = base / "sprite.jpg"
    if not src.exists():
        print(f"  ! {name}: no sprite.jpg"); return
    out = _matte(src)
    bb = out.getbbox()
    if bb:
        out = out.crop(bb)
    out.save(base / "sprite.png")
    print(f"  {name}: sprite -> {out.size}")


def cut_stickers(name: str, rows: int = 3, cols: int = 3):
    base = OUT / name
    src = base / "stickers.jpg"
    if not src.exists():
        print(f"  ! {name}: no stickers.jpg"); return
    sheet = _matte(src)
    sheet.save(base / "stickers_cut.png")
    W, H = sheet.size
    cw, ch = W // cols, H // rows
    pad = int(min(cw, ch) * 0.03)
    od = base / "stickers"
    od.mkdir(parents=True, exist_ok=True)
    for old in od.glob("sticker_*.png"):
        old.unlink()
    for i in range(rows):
        for j in range(cols):
            cell = sheet.crop((j * cw + pad, i * ch + pad, (j + 1) * cw - pad, (i + 1) * ch - pad))
            bb = cell.getbbox()
            if bb:
                cell = cell.crop(bb)
            cell.save(od / f"sticker_{i*cols+j:02d}.png")
    print(f"  {name}: 9 stickers from sheet {sheet.size}")


def run(mode: str, names: list[str]):
    names = names or sorted(p.name for p in OUT.iterdir() if p.is_dir())
    for n in names:
        if mode in ("sprites", "all"):
            cut_sprite(n)
        if mode in ("stickers", "all"):
            cut_stickers(n)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    run(mode, sys.argv[2:])
