"""The outbound media-marker extractor (protocol/media.py) — ported from hermes
gateway/platforms/base.py, adapted for sandbox-relative paths. These lock in the
hermes-faithful edge cases: extension gating, code/blockquote/JSON masking, the
three marker forms, and strip-from-visible-text."""
from lunamoth.protocol import media


def test_media_marker_relative_path_extracted_and_stripped():
    paths, cleaned = media.extract_media("here it is\nMEDIA:works/sketch.png\nenjoy")
    assert paths == ["works/sketch.png"]
    assert "MEDIA:" not in cleaned
    assert "here it is" in cleaned and "enjoy" in cleaned


def test_media_marker_absolute_and_quoted_paths():
    paths, _ = media.extract_media('MEDIA:/tmp/a.pdf\nMEDIA:"works/my file.png"')
    assert "/tmp/a.pdf" in paths
    assert "works/my file.png" in paths


def test_media_marker_requires_deliverable_extension():
    # .py is not a deliverable extension — not a marker.
    paths, cleaned = media.extract_media("MEDIA:works/main.py")
    assert paths == []
    assert "works/main.py" in cleaned  # left as prose


def test_media_marker_inside_code_fence_is_not_delivered():
    text = "```\nMEDIA:works/x.png\n```"
    paths, cleaned = media.extract_media(text)
    assert paths == []
    assert "MEDIA:works/x.png" in cleaned  # survives verbatim inside the fence


def test_media_marker_inside_inline_code_is_not_delivered():
    paths, _ = media.extract_media("use the `MEDIA:works/x.png` syntax")
    assert paths == []


def test_media_marker_inside_json_value_is_masked():
    # A stored marker in a serialized tool result must not re-deliver (hermes #34375).
    text = '{"result": "MEDIA:/Users/x/stale.png"}'
    paths, _ = media.extract_media(text)
    assert paths == []


def test_extract_images_only_image_urls():
    text = "![a](https://fal.media/x) and ![b](https://example.com/page.html)"
    images, cleaned = media.extract_images(text)
    urls = [u for u, _ in images]
    assert "https://fal.media/x" in urls
    assert "https://example.com/page.html" not in urls
    assert "![a]" not in cleaned


def test_extract_local_files_gated_on_exists():
    present = {"works/real.pdf"}
    paths, cleaned = media.extract_local_files(
        "saved works/real.pdf and works/ghost.pdf",
        exists=lambda p: p in present,
    )
    assert paths == ["works/real.pdf"]
    assert "works/real.pdf" not in cleaned
    assert "works/ghost.pdf" in cleaned  # not on disk → left as prose


def test_extract_local_files_skips_code_spans():
    present = {"works/real.png"}
    paths, _ = media.extract_local_files(
        "`works/real.png`",
        exists=lambda p: p in present,
    )
    assert paths == []


def test_is_image_path():
    assert media.is_image_path("works/a.PNG")
    assert not media.is_image_path("works/a.pdf")
    assert not media.is_image_path("noext")
