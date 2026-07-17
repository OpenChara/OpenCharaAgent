#!/usr/bin/env python3
"""Background removal via the remove.bg API (true alpha, no heavy local deps).

Only TWO cutouts per character:
  - the sprite (立绘)                -> 1 API call
  - the whole sticker SHEET (表情包)  -> 1 API call, then cropped locally into cells

remove.bg is a cloud call, so the runtime stays light (no onnxruntime/opencv/model
weights). Key in ~/.chara/removebg_key (or env REMOVEBG_API_KEY).

  python removebg.py sprites [Names...]    # cut立绘 for the given chars (default: all)
  python removebg.py stickers [Names...]   # cut + 3x3-crop sticker sheets
  python removebg.py all [Names...]        # both
"""
from __future__ import annotations
import os, sys
from pathlib import Path
import requests
from PIL import Image

OUT = Path(__file__).parent / "out"
ENDPOINT = "https://api.remove.bg/v1.0/removebg"


def _key() -> str:
    k = os.environ.get("REMOVEBG_API_KEY")
    if not k:
        p = Path.home() / ".chara" / "removebg_key"
        if p.exists():
            k = p.read_text().strip()
    if not k:
        sys.exit("no REMOVEBG_API_KEY (env or ~/.chara/removebg_key)")
    return k


def removebg(src: Path, dst: Path, crop: bool = True, size: str = "auto") -> Path:
    with open(src, "rb") as f:
        r = requests.post(
            ENDPOINT,
            headers={"X-Api-Key": _key()},
            data={"size": size, "format": "png", "crop": "true" if crop else "false"},
            files={"image_file": f},
            timeout=180,
        )
    if r.status_code != 200:
        raise RuntimeError(f"remove.bg {r.status_code}: {r.text[:300]}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(r.content)
    return dst


def cut_sprite(name: str):
    base = OUT / name
    src = base / "sprite.jpg"
    if not src.exists():
        print(f"  ! {name}: no sprite.jpg"); return
    out = removebg(src, base / "sprite.png", crop=True)
    w, h = Image.open(out).size
    print(f"  {name}: sprite cut -> {out.name} ({w}x{h})")


def cut_stickers(name: str, rows: int = 3, cols: int = 3):
    base = OUT / name
    src = base / "stickers.jpg"
    if not src.exists():
        print(f"  ! {name}: no stickers.jpg"); return
    # crop=false keeps the full sheet geometry so the grid stays aligned
    sheet = removebg(src, base / "stickers_cut.png", crop=False)
    im = Image.open(sheet).convert("RGBA")
    W, H = im.size
    cw, ch = W // cols, H // rows
    pad = int(min(cw, ch) * 0.03)
    od = base / "stickers"
    od.mkdir(parents=True, exist_ok=True)
    for old in od.glob("sticker_*.png"):  # clear stale (e.g. old 4x4) cells
        old.unlink()
    n = 0
    for i in range(rows):
        for j in range(cols):
            cell = im.crop((j * cw + pad, i * ch + pad, (j + 1) * cw - pad, (i + 1) * ch - pad))
            bb = cell.getbbox()
            if bb:
                cell = cell.crop(bb)
            cell.save(od / f"sticker_{i*cols+j:02d}.png")
            n += 1
    print(f"  {name}: sticker sheet cut + cropped -> {n} stickers")


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
