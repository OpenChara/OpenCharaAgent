"""Multimodal attachment ingestion + persistence + WeChat media recognition.

Covers the branch 多模态适配 contract (docs/multimodal-contract.md): small images
inline as image_url parts, files / oversized / unsupported-vision images land in
workspace/uploads with a note, list-content messages survive the transcript
round-trip, and WeChat media items are recognized rather than dropped.
"""
import base64
import tempfile
from pathlib import Path

import pytest

from lunamoth.core.attachments import (
    INLINE_IMAGE_MAX_BYTES,
    RawAttachment,
    build_user_content,
    ingest_attachments,
)
from lunamoth.tools.sandbox import Sandbox


def _wire(name, mime, data: bytes):
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode()}


def _sandbox():
    return Sandbox(Path(tempfile.mkdtemp()))


# ---- RawAttachment.from_wire -------------------------------------------------

def test_from_wire_decodes_and_guesses_mime():
    att = RawAttachment.from_wire(_wire("a.png", "", b"\x89PNGabc"))
    assert att is not None
    assert att.name == "a.png" and att.mime == "image/png" and att.is_image
    assert att.data == b"\x89PNGabc"


def test_from_wire_strips_data_url_prefix():
    raw = "data:image/png;base64," + base64.b64encode(b"hi").decode()
    att = RawAttachment.from_wire({"name": "x.png", "mime": "image/png", "data": raw})
    assert att is not None and att.data == b"hi"


def test_from_wire_strips_path_components_from_name():
    att = RawAttachment.from_wire(_wire("../../etc/passwd", "text/plain", b"x"))
    assert att is not None and "/" not in att.name and att.name == "passwd"


@pytest.mark.parametrize("bad", [None, "nope", {}, {"name": "x"}, {"data": "!!!notb64@@@"}, {"data": ""}])
def test_from_wire_rejects_malformed(bad):
    assert RawAttachment.from_wire(bad) is None


# ---- ingest_attachments ------------------------------------------------------

def test_small_image_inlines_for_vision_model():
    sb = _sandbox()
    img = b"\x89PNG" + b"x" * 200
    res = ingest_attachments([RawAttachment.from_wire(_wire("a.png", "image/png", img))],
                             sandbox=sb, vision_ok=True)
    assert len(res.content_parts) == 1
    part = res.content_parts[0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert res.saved == []  # inlined, not written to disk


def test_large_image_goes_to_workspace_with_note():
    sb = _sandbox()
    big = b"\x89PNG" + b"y" * (INLINE_IMAGE_MAX_BYTES + 10)
    res = ingest_attachments([RawAttachment.from_wire(_wire("big.png", "image/png", big))],
                             sandbox=sb, vision_ok=True)
    assert res.content_parts == []
    assert res.saved == ["uploads/big.png"]
    assert any("big.png" in n for n in res.notes)
    assert "uploads/big.png" in sb.list_files()


def test_image_without_vision_saves_and_notices():
    sb = _sandbox()
    img = b"\x89PNG" + b"x" * 50
    res = ingest_attachments([RawAttachment.from_wire(_wire("a.png", "image/png", img))],
                             sandbox=sb, vision_ok=False)
    assert res.content_parts == []
    assert res.saved == ["uploads/a.png"]
    assert res.notices and "workspace/uploads/a.png" in res.notices[0]


def test_non_image_file_always_saved():
    sb = _sandbox()
    res = ingest_attachments([RawAttachment.from_wire(_wire("doc.txt", "text/plain", b"hello"))],
                             sandbox=sb, vision_ok=True)
    assert res.content_parts == []
    assert res.saved == ["uploads/doc.txt"]
    assert sb.read_file("uploads/doc.txt") == "hello"


def test_name_collision_does_not_overwrite():
    sb = _sandbox()
    a = ingest_attachments([RawAttachment.from_wire(_wire("doc.txt", "text/plain", b"one"))],
                           sandbox=sb, vision_ok=True)
    b = ingest_attachments([RawAttachment.from_wire(_wire("doc.txt", "text/plain", b"two"))],
                           sandbox=sb, vision_ok=True)
    assert a.saved == ["uploads/doc.txt"]
    assert b.saved == ["uploads/doc (2).txt"]
    assert sb.read_file("uploads/doc.txt") == "one"
    assert sb.read_file("uploads/doc (2).txt") == "two"


def test_build_user_content_string_when_no_inline():
    res = ingest_attachments([RawAttachment.from_wire(_wire("doc.txt", "text/plain", b"x"))],
                             sandbox=_sandbox(), vision_ok=True)
    content = build_user_content("see file", res)
    assert isinstance(content, str)
    assert content.startswith("see file")


def test_build_user_content_list_when_inline_image():
    res = ingest_attachments([RawAttachment.from_wire(_wire("a.png", "image/png", b"\x89PNGxx"))],
                             sandbox=_sandbox(), vision_ok=True)
    content = build_user_content("what is this", res)
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and content[0]["text"].startswith("what is this")
    assert content[-1]["type"] == "image_url"


def test_empty_text_with_image_still_builds_list():
    res = ingest_attachments([RawAttachment.from_wire(_wire("a.png", "image/png", b"\x89PNGxx"))],
                             sandbox=_sandbox(), vision_ok=True)
    content = build_user_content("", res)
    assert isinstance(content, list)
    # no empty leading text part — just the note + image
    assert all(p.get("text") != "" for p in content if p["type"] == "text")


# ---- context.pairs flattening + transcript round-trip ------------------------

def test_context_pairs_flattens_list_content():
    from lunamoth.core.context import ContextBuffer
    buf = ContextBuffer()
    buf.add_message({"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]})
    pairs = buf.pairs()
    assert pairs == [("user", "look")]


def test_transcript_roundtrips_multimodal_message():
    from lunamoth.core.transcript import TranscriptStore
    db = Path(tempfile.mkdtemp()) / "t.db"
    t = TranscriptStore(db)
    if not t.available:
        pytest.skip("sqlite transcript unavailable")
    content = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    t.append_message({"role": "user", "content": content})
    rows = t.load()
    assert rows and isinstance(rows[-1]["content"], list)
    assert rows[-1]["content"][1]["type"] == "image_url"


# ---- llm.vision_supported ----------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("openai/gpt-4o", True),
    ("gpt-4o-mini", True),
    ("anthropic/claude-sonnet-4", True),
    ("google/gemini-2.5-flash", True),
    ("qwen/qwen2.5-vl-72b", True),
    ("deepseek/deepseek-v4-flash", False),
    ("mistralai/ministral-8b", False),
])
def test_vision_supported_heuristic(model, expected):
    from dataclasses import replace

    from lunamoth.config import LLMConfig
    from lunamoth.core.llm import LLMClient
    client = LLMClient(replace(LLMConfig(), model=model, vision="auto"))
    assert client.vision_supported() is expected


def test_vision_override_on_off():
    from dataclasses import replace

    from lunamoth.config import LLMConfig
    from lunamoth.core.llm import LLMClient
    off = LLMClient(replace(LLMConfig(), model="gpt-4o", vision="off"))
    on = LLMClient(replace(LLMConfig(), model="deepseek/deepseek-v4-flash", vision="on"))
    assert off.vision_supported() is False
    assert on.vision_supported() is True


# ---- WeChat media recognition ------------------------------------------------

def test_wechat_text_and_voice_still_extracted():
    from lunamoth.messaging.weixin import item_list_to_parts
    items = [
        {"type": 1, "text_item": {"text": "hi"}},
        {"type": 3, "voice_item": {"text": "transcribed"}},
    ]
    text, atts = item_list_to_parts(items)
    assert text == "hi\ntranscribed" and atts == []


def test_wechat_image_with_url_is_attached():
    from lunamoth.messaging.weixin import item_list_to_parts
    items = [{"type": 2, "image_item": {"url": "https://x/y.jpg"}}]
    text, atts = item_list_to_parts(items)
    assert "[图片" in text and "https://x/y.jpg" in text
    assert atts == [{"name": "image", "mime": "image/jpeg", "url": "https://x/y.jpg", "kind": "image"}]


def test_wechat_cdn_image_without_url_is_marker_only():
    from lunamoth.messaging.weixin import item_list_to_parts
    items = [{"type": 2, "image_item": {"cdn_key": "encrypted-blob"}}]
    text, atts = item_list_to_parts(items)
    assert text == "[图片]" and atts == []  # recognized, not dropped; nothing fetchable


def test_wechat_file_marker_carries_name_and_size():
    from lunamoth.messaging.weixin import item_list_to_parts
    items = [{"type": 6, "file_item": {"file_name": "report.pdf", "file_size": 2048}}]
    text, atts = item_list_to_parts(items)
    assert "report.pdf" in text and "2048 bytes" in text


def test_wechat_sticker_marker():
    from lunamoth.messaging.weixin import item_list_to_parts
    text, atts = item_list_to_parts([{"type": 5, "emoji_item": {}}])
    assert text == "[表情]"


def test_wechat_unknown_type_is_generic_marker_not_dropped():
    from lunamoth.messaging.weixin import item_list_to_parts
    text, atts = item_list_to_parts([{"type": 99, "mystery_item": {}}])
    assert text == "[媒体]"


def test_wechat_junk_never_crashes():
    from lunamoth.messaging.weixin import item_list_to_parts
    # Not a list → empty. Non-dict items skipped. A malformed-but-present dict
    # item is recognized as generic media ([媒体]), never silently dropped.
    assert item_list_to_parts("nope") == ("", [])
    assert item_list_to_parts([None, 5]) == ("", [])
    text, atts = item_list_to_parts([None, 5, {"type": "x"}])
    assert text == "[媒体]" and atts == []


# ---- tool→model image vision (read_file on an image) -------------------------
# A chara reading a workspace image: when the model has vision, the agent injects
# the pixels as a follow-up USER message (hermes image_url shape); otherwise the
# honest no-vision note stands. Unit-tests the agent helper in isolation.
import types as _types

from lunamoth.core.agent import LunaMothAgent


class _StubLLM:
    def __init__(self, vision):
        self._v = vision

    def vision_supported(self):
        return self._v


def _agent_stub(sandbox, vision):
    return _types.SimpleNamespace(sandbox=sandbox, llm=_StubLLM(vision))


def _png(nbytes=64):
    return b"\x89PNG\r\n\x1a\n" + b"x" * nbytes


def test_image_vision_followup_injects_user_image_when_vision():
    sb = _sandbox()
    rel = sb.write_bytes("look.png", _png())
    out = LunaMothAgent._image_vision_followup(_agent_stub(sb, True), rel)
    assert out is not None
    note, follow = out
    assert "attached" in note.lower()
    assert follow["role"] == "user"            # image rides a USER message, not the tool message
    parts = follow["content"]
    assert parts[-1]["type"] == "image_url"
    assert parts[-1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_vision_followup_none_without_vision():
    sb = _sandbox()
    rel = sb.write_bytes("look.png", _png())
    assert LunaMothAgent._image_vision_followup(_agent_stub(sb, False), rel) is None


def test_image_vision_followup_none_for_oversized():
    sb = _sandbox()
    rel = sb.write_bytes("big.png", _png(INLINE_IMAGE_MAX_BYTES + 1))
    assert LunaMothAgent._image_vision_followup(_agent_stub(sb, True), rel) is None


def test_image_vision_followup_none_for_nonimage():
    sb = _sandbox()
    rel = sb.write_bytes("notes.txt", b"hello, not an image")
    assert LunaMothAgent._image_vision_followup(_agent_stub(sb, True), rel) is None
