#!/usr/bin/env python3
"""Deterministic blank LAYOUT TEMPLATE for the 主视觉 (key-visual settei sheet).

A wireframe of labelled empty panels, drawn with PIL so it is byte-identical every
time. It is fed to Seedream as a structural reference image so every character's
key visual lands in the SAME layout — making the most complex asset controllable
and (for downstream tooling) deterministically croppable.

  python template.py            -> visuals/template_keyvisual.png  (4096x2304, 16:9)

REGIONS (fractions of W,H) are exported as TEMPLATE_REGIONS so a future script can
crop each panel out of the finished sheet by fixed geometry.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 4096, 2304
INK = (60, 64, 72)
FAINT = (210, 214, 220)
GRID = (236, 238, 242)
BG = (252, 252, 250)

# region boxes in pixels (x0, y0, x1, y1) + label
M = 48
def _grid(d):
    for x in range(0, W, 96):
        d.line([(x, 0), (x, H)], fill=GRID, width=1)
    for y in range(0, H, 96):
        d.line([(0, y), (W, y)], fill=GRID, width=1)

# layout (full body + 3-view turnaround + action pose + props + palette; no expression row)
LEFT_W = 1340
panels = {}
panels["FULL BODY"] = (M, M, M + LEFT_W, H - M)
rx0 = M + LEFT_W + 40
rx1 = W - M
g = 28
# top: 3-view turnaround (tall)
tw = (rx1 - rx0 - 2 * g) // 3
y0 = M; y1 = 1160
for i, lab in enumerate(["FRONT", "SIDE", "BACK"]):
    x0 = rx0 + i * (tw + g)
    panels[lab] = (x0, y0, x0 + tw, y1)
# bottom: action pose (left) + props + palette (right)
y0 = y1 + 40; y1 = H - M
act_w = 1480
panels["ACTION POSE"] = (rx0, y0, rx0 + act_w, y1)
px0 = rx0 + act_w + g
panels["PROPS"] = (px0, y0, rx1, y0 + 460)
panels["COLOR PALETTE"] = (px0, y0 + 500, rx1, y1)

TEMPLATE_REGIONS = {k: (x0 / W, y0 / H, x1 / W, y1 / H) for k, (x0, y0, x1, y1) in panels.items()}


def _font(sz):
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def build(path: Path = None) -> Path:
    path = path or (Path(__file__).parent / "template_keyvisual.png")
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    _grid(d)
    f = _font(34)
    for lab, (x0, y0, x1, y1) in panels.items():
        d.rounded_rectangle([x0, y0, x1, y1], radius=18, outline=INK, width=4)
        d.rectangle([x0 + 8, y0 + 8, x0 + 18 + len(lab) * 20, y0 + 54], fill=BG)
        d.text((x0 + 16, y0 + 14), lab, fill=INK, font=f)
        # crosshair to mark panel center as a fill cue
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        d.line([(cx - 22, cy), (cx + 22, cy)], fill=FAINT, width=2)
        d.line([(cx, cy - 22), (cx, cy + 22)], fill=FAINT, width=2)
    # palette swatch sub-cells
    x0, y0, x1, y1 = panels["COLOR PALETTE"]
    n = 5; cw = (x1 - x0 - 40) // n
    for i in range(n):
        sx = x0 + 20 + i * cw
        d.rectangle([sx, y0 + 70, sx + cw - 16, y1 - 30], outline=FAINT, width=3)
    im.save(path)
    return path


if __name__ == "__main__":
    p = build()
    print("template ->", p, f"({W}x{H})")
    for k, v in TEMPLATE_REGIONS.items():
        print(f"  {k:16} {tuple(round(x,3) for x in v)}")
