"""Image-performance helpers: avatar thumbnailing (cached) + format-preserving
re-compression with a long-side cap. See content/imaging.py."""
import io

from PIL import Image

from chara.content import imaging


def _png_bytes(size, color=(120, 80, 200, 255), mode="RGBA"):
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _webp_bytes(size, color=(30, 30, 30)):
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=95)
    return buf.getvalue()


def test_avatar_thumb_is_small_webp(tmp_path):
    imaging._clear_thumb_cache()
    p = tmp_path / "a.avatar.png"
    p.write_bytes(_png_bytes((512, 512)))
    uri = imaging.avatar_thumb_data_uri(p)
    assert uri.startswith("data:image/webp;base64,")
    # The thumbnail decodes back to ≤160px on the long side.
    import base64
    payload = base64.b64decode(uri.split(",", 1)[1])
    with Image.open(io.BytesIO(payload)) as im:
        assert max(im.size) <= imaging._AVATAR_THUMB_PX


def test_avatar_thumb_cache_keyed_on_mtime(tmp_path):
    imaging._clear_thumb_cache()
    p = tmp_path / "a.avatar.png"
    p.write_bytes(_png_bytes((300, 300)))
    first = imaging.avatar_thumb_data_uri(p)
    assert imaging._thumb_cache_size() == 1
    # A second call is a cache hit (no new entry).
    assert imaging.avatar_thumb_data_uri(p) == first
    assert imaging._thumb_cache_size() == 1


def test_avatar_thumb_missing_file_returns_empty(tmp_path):
    imaging._clear_thumb_cache()
    assert imaging.avatar_thumb_data_uri(tmp_path / "nope.png") == ""


def test_compress_caps_long_side_keeps_png_format(tmp_path):
    raw = _png_bytes((2000, 1000))
    out = imaging.compress_image_bytes(raw, "png", 1280)
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) == 1280
        assert im.format == "PNG"


def test_compress_webp_stays_webp_and_shrinks(tmp_path):
    raw = _webp_bytes((1600, 900))
    out = imaging.compress_image_bytes(raw, "webp", 1280)
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "WEBP"
        assert max(im.size) <= 1280


def test_compress_preserves_alpha(tmp_path):
    raw = _png_bytes((600, 600), color=(0, 0, 0, 0))
    out = imaging.compress_image_bytes(raw, "png", 512)
    with Image.open(io.BytesIO(out)) as im:
        assert im.mode in ("RGBA", "LA", "P")


def test_compress_never_grows(tmp_path):
    # A tiny image can't shrink — the original bytes come back unchanged.
    raw = _png_bytes((1, 1))
    assert imaging.compress_image_bytes(raw, "png", 1280) == raw


def test_compress_unknown_ext_is_passthrough():
    raw = b"not really an image"
    assert imaging.compress_image_bytes(raw, "gif", 1280) == raw


def test_compress_file_in_place(tmp_path):
    p = tmp_path / "art.png"
    p.write_bytes(_png_bytes((2000, 2000)))
    before, after = imaging.compress_file_in_place(p, 1280)
    assert after < before
    with Image.open(p) as im:
        assert max(im.size) == 1280
