"""Image-performance helpers — thumbnailing + re-compression (Pillow).

ONE place for the two image-shrinking jobs the product needs:

  * `avatar_thumb_data_uri` — a tiny inline avatar (≤160px long side, WEBP q80)
    for the board list + StateSnapshot, with an in-memory (path, st_mtime_ns)
    cache so `list_cards` doesn't re-encode N thumbnails every hub.state. The
    FULL-res avatar still rides /asset or `card.avatar_read`; only the inline
    data-URI shrinks.
  * `compress_image_bytes` — cap the long side + re-encode (WEBP q82 for photos,
    optimized PNG for PNGs), preserving transparency. Used by the upload path
    (asset_save) and the one-time bundled-asset pass.

Pillow is a core dependency (pyproject); `from PIL import Image` is safe here.
Every public helper is best-effort: a decode/encode failure returns the input
unchanged (compress) or '' (thumbnail) rather than raising — an image that
won't shrink must never break a card list or an upload.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

# Inline-avatar thumbnail: small enough that N of them in list_cards stay cheap.
_AVATAR_THUMB_PX = 160
_AVATAR_THUMB_Q = 80

# (path, st_mtime_ns) -> data-URI. Keyed on mtime so an edited/replaced sidecar
# re-encodes; an unchanged one is served from memory.
_thumb_cache: dict[tuple[str, int], str] = {}


def _has_alpha(im: "Image.Image") -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _fit_long_side(im: "Image.Image", max_px: int) -> "Image.Image":
    w, h = im.size
    long = max(w, h)
    if long <= max_px:
        return im
    scale = max_px / float(long)
    new = (max(1, round(w * scale)), max(1, round(h * scale)))
    return im.resize(new, Image.LANCZOS)


def avatar_thumb_data_uri(sidecar: "Path") -> str:
    """A tiny WEBP (or PNG fallback) data-URI for an avatar sidecar, cached.

    Downscales to ≤160px on the long side, WEBP quality 80. SVG/unknown
    handling stays with the caller — this only touches raster sidecars. Returns
    '' if the file can't be read/decoded so the caller can fall back."""
    try:
        st = sidecar.stat()
    except OSError:
        return ""
    key = (str(sidecar), st.st_mtime_ns)
    hit = _thumb_cache.get(key)
    if hit is not None:
        return hit
    try:
        with Image.open(sidecar) as im:
            im.load()
            alpha = _has_alpha(im)
            im = im.convert("RGBA" if alpha else "RGB")
            im = _fit_long_side(im, _AVATAR_THUMB_PX)
            buf = io.BytesIO()
            try:
                im.save(buf, format="WEBP", quality=_AVATAR_THUMB_Q, method=6)
                mime = "image/webp"
            except Exception:  # noqa: BLE001 — webp encode unavailable: PNG fallback
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                mime = "image/png"
    except Exception:  # noqa: BLE001 — undecodable sidecar: caller falls back
        return ""
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    uri = f"data:{mime};base64,{data}"
    # Bound the cache so a long-running daemon paging through many decks can't
    # grow it without limit; mtime keys make eviction safe (re-encode on miss).
    if len(_thumb_cache) > 512:
        _thumb_cache.clear()
    _thumb_cache[key] = uri
    return uri


# ---- re-compression (uploads + the bundled-asset pass) -----------------------
# Format-preserving: a .png stays a PNG (optimized), a .webp/.jpg re-encodes as
# WEBP q82 — the card.json references the file by its existing extension, so the
# extension never changes here.
_PHOTO_Q = 82


def _encode(im: "Image.Image", fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "PNG":
        im.save(buf, format="PNG", optimize=True)
    elif fmt == "WEBP":
        im.save(buf, format="WEBP", quality=_PHOTO_Q, method=6)
    elif fmt in ("JPEG", "JPG"):
        # JPEG has no alpha; flatten onto white if needed.
        if _has_alpha(im):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        im.save(buf, format="JPEG", quality=_PHOTO_Q, optimize=True, progressive=True)
    else:
        im.save(buf, format=fmt)
    return buf.getvalue()


def _pil_format(ext: str) -> str:
    e = ext.lower().lstrip(".")
    return {"png": "PNG", "webp": "WEBP", "jpg": "JPEG", "jpeg": "JPEG"}.get(e, "")


def compress_image_bytes(raw: bytes, ext: str, max_px: int) -> bytes:
    """Re-encode `raw` capped at `max_px` on the long side, KEEPING its format.

    PNG: cap dims + optimize (transparency preserved). WEBP/JPEG: cap dims +
    quality 82. Returns the ORIGINAL bytes when the result wouldn't be smaller,
    when the format is unknown, or on any decode/encode error — never raises,
    never grows a file."""
    fmt = _pil_format(ext)
    if not fmt:
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            alpha = _has_alpha(im)
            # PNG keeps its (possibly paletted) alpha; WEBP wants RGBA/RGB.
            if fmt == "WEBP":
                im = im.convert("RGBA" if alpha else "RGB")
            elif fmt == "PNG" and im.mode not in ("RGBA", "RGB", "L", "LA", "P"):
                im = im.convert("RGBA" if alpha else "RGB")
            im = _fit_long_side(im, max_px)
            out = _encode(im, fmt)
    except Exception:  # noqa: BLE001 — undecodable/odd image: leave it untouched
        return raw
    return out if 0 < len(out) < len(raw) else raw


def compress_file_in_place(path: "Path", max_px: int) -> tuple[int, int]:
    """Re-compress a file on disk in place at its existing format. Returns
    (before, after) byte sizes; (n, n) when nothing changed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return (0, 0)
    before = len(raw)
    out = compress_image_bytes(raw, path.suffix, max_px)
    if len(out) < before:
        try:
            path.write_bytes(out)
        except OSError:
            return (before, before)
        return (before, len(out))
    return (before, before)


# Long-side caps by asset role (the bundled-pass + upload policy).
CAP_AVATAR = 512
CAP_STICKER = 512
CAP_ART = 1280  # sprite / keyvisual / background


def reencode_to_webp(src: "Path", max_px: int) -> bytes:
    """Decode `src` (any format), cap the long side, return WEBP q82 bytes
    (alpha preserved). Raises on a decode/encode failure so the bundled pass can
    skip the file rather than write garbage."""
    with Image.open(src) as im:
        im.load()
        alpha = _has_alpha(im)
        im = im.convert("RGBA" if alpha else "RGB")
        im = _fit_long_side(im, max_px)
        return _encode(im, "WEBP")


def _axis_cuts(im: "Image.Image", axis: str, n: int, full: int, samples: int = 192) -> list[int]:
    """Find n+1 boundary positions along an axis by detecting the white GUTTERS of a
    sheet (a fast averaged 1-D brightness profile). Returns the content start, the
    centers of the n-1 widest internal white runs, and the content end. Falls back to
    an even split when a clean grid can't be read — so a messy sheet still yields n
    bands rather than garbage."""
    even = [round(k * full / n) for k in range(n + 1)]
    try:
        gray = im.convert("L")
        prof = gray.resize((samples, 1), Image.BOX) if axis == "x" else gray.resize((1, samples), Image.BOX)
        vals = list(prof.tobytes())  # brightness 0..255 per bucket (high = white bg); "L" → 1 byte/px
    except Exception:  # noqa: BLE001
        return even
    bg = [v >= 240 for v in vals]
    lo = 0
    while lo < len(bg) and bg[lo]:
        lo += 1
    hi = len(bg) - 1
    while hi > lo and bg[hi]:
        hi -= 1
    if hi <= lo:
        return even
    runs: list[tuple[int, int]] = []
    k = lo + 1
    while k < hi:
        if bg[k]:
            s = k
            while k < hi and bg[k]:
                k += 1
            runs.append((s, k))
        else:
            k += 1
    if len(runs) < n - 1:
        return even
    gutters = sorted(sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:n - 1])
    scale = full / len(vals)
    bounds = [lo * scale] + [((s + e) / 2) * scale for s, e in gutters] + [(hi + 1) * scale]
    cuts = [max(0, min(full, round(b))) for b in bounds]
    # guard against degenerate (non-increasing) boundaries → even split
    return cuts if all(cuts[i] < cuts[i + 1] for i in range(n)) else even


def _trim_white(cell: "Image.Image", tol: int = 18, margin: int = 2) -> "Image.Image":
    """Tighten a cell to its non-white content bbox (+ a small margin) so a sticker is
    centered, not floating in white. Returns the cell unchanged if it looks empty."""
    mask = cell.convert("L").point(lambda v: 255 if v < 255 - tol else 0)
    bbox = mask.getbbox()
    if not bbox:
        return cell
    x0, y0, x1, y1 = bbox
    return cell.crop((max(0, x0 - margin), max(0, y0 - margin),
                      min(cell.width, x1 + margin), min(cell.height, y1 + margin)))


def slice_grid(sheet: bytes, rows: int = 3, cols: int = 3, pad_frac: float = 0.04) -> list[bytes]:
    """Slice a rows x cols sheet into cells → one PNG (bytes) per cell in row-major
    order (left→right, top→bottom), alpha preserved. Detects the white gutters to place
    the cuts (robust to an imperfect grid), then trims each cell to its content bbox so
    a sticker is centered. Falls back to an even split when no clean grid is found.
    Raises on an undecodable sheet so the caller surfaces a real error (no fake stickers).
    `pad_frac` is retained for compatibility but detection + trim supersede it."""
    with Image.open(io.BytesIO(sheet)) as im:
        im.load()
        im = im.convert("RGBA")
        w, h = im.size
        xs = _axis_cuts(im, "x", cols, w)
        ys = _axis_cuts(im, "y", rows, h)
        out: list[bytes] = []
        for i in range(rows):
            for j in range(cols):
                cell = im.crop((xs[j], ys[i], xs[j + 1], ys[i + 1]))
                buf = io.BytesIO()
                _trim_white(cell).save(buf, format="PNG")
                out.append(buf.getvalue())
    return out


def has_transparency(raw: bytes) -> bool:
    """True if the image already carries meaningful alpha (some pixels not fully
    opaque). Lets the matte path skip a pointless re-cut of an already-transparent
    PNG when only the keyless white-bg fallback is available. Best-effort: False on
    any decode error (so the caller still attempts a cut rather than refusing)."""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if not _has_alpha(im):
                return False
            alpha = im.convert("RGBA").getchannel("A")
            return alpha.getextrema()[0] < 250
    except Exception:  # noqa: BLE001
        return False


def _clear_thumb_cache() -> None:
    """Test seam: drop the in-memory thumbnail cache."""
    _thumb_cache.clear()


def _thumb_cache_size() -> int:
    return len(_thumb_cache)
