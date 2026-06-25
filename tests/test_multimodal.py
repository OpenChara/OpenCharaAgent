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
    SHRINK_TARGET_BYTES,
    RawAttachment,
    build_user_content,
    ingest_attachments,
    shrink_data_url,
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


_PNG_SIG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
_HEIC_SIG = b"\x00\x00\x00\x18ftypheic" + b"0" * 8


def test_from_wire_sniff_overrides_lying_client_mime():
    # A client that mislabels a PNG as image/webp would 400 a strict provider; the
    # magic bytes win over the declared mime (hermes _sniff_mime_from_bytes).
    att = RawAttachment.from_wire(_wire("photo.bin", "image/webp", _PNG_SIG))
    assert att is not None and att.mime == "image/png" and att.is_image


def test_from_wire_sniff_recovers_image_with_no_mime_and_unknown_name():
    # No declared mime and an extension not in the name table → previously fell to
    # application/octet-stream and was shunted to disk; sniffing recovers it.
    att = RawAttachment.from_wire(_wire("photo", "", _HEIC_SIG))
    assert att is not None and att.mime == "image/heic" and att.is_image


def test_from_wire_sniff_leaves_non_image_to_declared_mime():
    # Sniffing only knows images; a real document keeps its declared mime.
    att = RawAttachment.from_wire(_wire("d.pdf", "application/pdf", b"%PDF-1.4"))
    assert att is not None and att.mime == "application/pdf" and not att.is_image


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


def test_oversized_image_inlines_full_size_for_vision():
    # hermes native shape: an image is inlined at FULL size (no proactive shrink) —
    # the model sees the real pixels; a provider too-large rejection is recovered by
    # the LLM layer's reactive shrink, not by dropping the image at ingest.
    sb = _sandbox()
    big = b"\x89PNG\r\n\x1a\n" + b"y" * (SHRINK_TARGET_BYTES + 10)
    res = ingest_attachments([RawAttachment.from_wire(_wire("big.png", "image/png", big))],
                             sandbox=sb, vision_ok=True)
    assert res.saved == []                       # inlined, not dropped to disk
    assert len(res.content_parts) == 1
    url = res.content_parts[0]["image_url"]["url"]
    inlined = base64.b64decode(url.split(",", 1)[1])
    assert inlined == big                        # FULL size — byte-identical, not shrunk


def test_shrink_data_url_downscales_a_large_real_image():
    # The reactive recovery: shrink_data_url re-encodes an oversized data URL to fit
    # the 4MB target (hermes), used on a provider image-too-large rejection.
    pytest.importorskip("PIL")
    import io
    import os
    from PIL import Image

    raw = os.urandom(1400 * 1400 * 3)  # random noise → ~5.9MB PNG, well over 4MB
    buf = io.BytesIO()
    Image.frombytes("RGB", (1400, 1400), raw).save(buf, format="PNG")
    big_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    assert len(big_url) > SHRINK_TARGET_BYTES

    small_url = shrink_data_url(big_url)
    assert small_url is not None and small_url.startswith("data:image/")
    assert len(small_url) <= SHRINK_TARGET_BYTES   # shrunk under the target
    assert len(small_url) < len(big_url)

    # A non-data URL (remote) is left alone.
    assert shrink_data_url("https://example.com/x.png") is None


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


def test_transcript_roundtrips_multimodal_message_stripping_image_bytes():
    # A multimodal message reloads as structured content, but inline image BYTES are
    # NOT persisted (commit 79eac31 "never persist bytes", extended to the upload
    # path): the text handle round-trips, the data: URL is stripped. A remote http
    # image URL is tiny and kept.
    from lunamoth.core.transcript import TranscriptStore
    db = Path(tempfile.mkdtemp()) / "t.db"
    t = TranscriptStore(db)
    if not t.available:
        pytest.skip("sqlite transcript unavailable")
    content = [
        {"type": "text", "text": "hello [image: cat.png]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://cdn.example.com/y.png"}},
    ]
    msg = {"role": "user", "content": content}
    t.append_message(msg)
    # the in-memory message is NOT mutated — the live context keeps full pixels;
    # only the persisted copy is stripped.
    assert msg["content"] is content and content[1]["image_url"]["url"].startswith("data:")
    rows = t.load()
    assert rows and isinstance(rows[-1]["content"], list)
    parts = rows[-1]["content"]
    # the text handle survived
    assert any(p["type"] == "text" and "cat.png" in p["text"] for p in parts)
    # the base64 data URL was stripped; the remote URL was kept
    urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    assert not any(u.startswith("data:") for u in urls)
    assert "https://cdn.example.com/y.png" in urls


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
    def __init__(self, vision, describe=None):
        self._v = vision
        self._describe = describe

    def vision_supported(self):
        return self._v

    def describe_image(self, data, mime, question=""):
        return self._describe


def _agent_stub(sandbox, vision, describe=None):
    stub = _types.SimpleNamespace(sandbox=sandbox, llm=_StubLLM(vision, describe))
    # _image_vision_followup delegates to _vision_followup_for_path on self; bind the
    # real core so the unbound-method-with-stub-self calls resolve.
    stub._vision_followup_for_path = (
        lambda fp, label, question="": LunaMothAgent._vision_followup_for_path(stub, fp, label, question))
    return stub


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


def test_image_vision_followup_none_without_vision_or_vision_model():
    # main model can't see AND no vision_model describes it → None (honest note)
    sb = _sandbox()
    rel = sb.write_bytes("look.png", _png())
    assert LunaMothAgent._image_vision_followup(_agent_stub(sb, False), rel) is None


def test_vision_followup_for_absolute_path_inlines_with_question(tmp_path):
    # the browser-screenshot native path: an ABSOLUTE image file + a question →
    # inline the pixels with the question folded into the follow-up text.
    fp = tmp_path / "shot.png"
    fp.write_bytes(_png())
    out = LunaMothAgent._vision_followup_for_path(
        _agent_stub(None, True), fp, "shot.png", "what is on screen?")
    assert out is not None
    note, follow = out
    assert "shot.png" in note
    assert follow["content"][0]["text"].endswith("what is on screen?")
    assert follow["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_vision_followup_describes_via_vision_model_without_vision():
    # main model can't see, but a vision_model describes it → inject the description
    # as the note, with NO pixels inlined (follow is None).
    sb = _sandbox()
    rel = sb.write_bytes("look.png", _png())
    out = LunaMothAgent._image_vision_followup(
        _agent_stub(sb, False, describe="a small red square on white"), rel)
    assert out is not None
    note, follow = out
    assert follow is None                                   # no pixels — model can't see
    assert "a small red square on white" in note           # the description is injected
    assert "vision model" in note.lower()


def test_image_vision_followup_inlines_oversized_full_size():
    # hermes native shape: a large workspace image is re-viewed at FULL size (the
    # reactive shrink handles a provider rejection, not a proactive bail).
    sb = _sandbox()
    big = _png(SHRINK_TARGET_BYTES + 1)
    rel = sb.write_bytes("big.png", big)
    out = LunaMothAgent._image_vision_followup(_agent_stub(sb, True), rel)
    assert out is not None
    _note, follow = out
    url = follow["content"][-1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == big   # full size, not shrunk


def test_image_vision_followup_none_for_nonimage():
    sb = _sandbox()
    rel = sb.write_bytes("notes.txt", b"hello, not an image")
    assert LunaMothAgent._image_vision_followup(_agent_stub(sb, True), rel) is None


def test_read_file_image_vision_followup(tmp_path, monkeypatch):
    """read_file on an image yields an image_url follow-up USER message when the
    model has vision; without vision (or for non-images) it returns None so the
    honest 'can't see it' note stands. (R2 — on-disk image vision.)"""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.session.settings import Settings
    from lunamoth.core.agent import LunaMothAgent
    a = LunaMothAgent(Settings(character_path="", toolpack="sandbox"))
    ws = a.sandbox.workspace_dir
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "pic.png").write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64)

    monkeypatch.setattr(a.llm, "vision_supported", lambda: True)
    inj = a._image_vision_followup("pic.png")
    assert inj is not None
    note, follow = inj
    assert follow["role"] == "user"
    assert any(p.get("type") == "image_url"
               and p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in follow["content"])

    monkeypatch.setattr(a.llm, "vision_supported", lambda: False)
    assert a._image_vision_followup("pic.png") is None  # honest note stands

    (ws / "note.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(a.llm, "vision_supported", lambda: True)
    assert a._image_vision_followup("note.txt") is None  # not an image


def test_execute_tool_browser_vision_native_inlines(tmp_path, monkeypatch):
    """When the tool returns vision_native (main model has native vision), the agent
    inlines the screenshot pixels on a follow-up user message + keeps the MEDIA path."""
    import json as _json
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.session.settings import Settings
    from lunamoth.core.agent import LunaMothAgent
    a = LunaMothAgent(Settings(character_path="", toolpack="sandbox"))
    monkeypatch.setattr(a.llm, "vision_supported", lambda: True)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    data = _json.dumps({"success": True, "vision_native": True,
                        "screenshot_path": str(shot), "question": "what is here?"})
    monkeypatch.setattr(a.tools, "call", lambda name, **kw: {"ok": True, "data": data})
    monkeypatch.setattr(a.tools, "result_cap", lambda name: 100000)
    out = a._execute_tool({"function": {"name": "browser_vision", "arguments": "{}"}})
    assert out["follow_up"]["role"] == "user"
    parts = out["follow_up"]["content"]
    assert parts[-1]["type"] == "image_url"
    assert parts[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[0]["text"].endswith("what is here?")     # question rides the inline text
    assert "MEDIA:" in out["content"]                      # path preserved for surfacing


# ---- strip_old_images: keep newest image's pixels, collapse older to text ----
def test_strip_old_images_keeps_only_the_newest():
    from lunamoth.core.context import ContextBuffer
    from lunamoth.core import compaction

    def img(ref):
        return {"role": "user", "content": [
            {"type": "text", "text": f"[image: {ref}]"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}

    ctx = ContextBuffer()
    ctx.messages = [img("old.png"), {"role": "assistant", "content": "noted"}, img("new.png")]
    assert compaction.strip_old_images(ctx) is True
    # newest keeps real pixels
    assert any(p.get("type") == "image_url" for p in ctx.messages[2]["content"])
    # older collapsed to a text handle (no image part), still naming the ref
    old = ctx.messages[0]["content"]
    assert isinstance(old, str) and "old.png" in old and "no longer attached" in old
    # idempotent — a second pass changes nothing
    assert compaction.strip_old_images(ctx) is False


def test_strip_old_images_noop_without_images():
    from lunamoth.core.context import ContextBuffer
    from lunamoth.core import compaction
    ctx = ContextBuffer()
    ctx.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert compaction.strip_old_images(ctx) is False


def test_summarizer_does_not_leak_image_base64():
    """A surviving image in the summarized HEAD must collapse to its text handle,
    never dump ~2MB of base64 into the text summarizer prompt (audit HIGH)."""
    from lunamoth.core import compaction
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": [
            {"type": "text", "text": "[image: pic.png]"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}}]},
        {"role": "assistant", "content": "nice picture"},
    ]
    s = compaction._serialize(msgs)
    assert "base64" not in s and "data:image" not in s
    assert "[image: pic.png]" in s and "nice picture" in s


def test_msg_text_flattens_image_content_not_base64():
    from lunamoth.core.context import _msg_text
    m = {"role": "user", "content": [
        {"type": "text", "text": "[image: p.png]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}]}
    txt = _msg_text(m)
    assert "[image: p.png]" in txt and "base64" not in txt and len(txt) < 80
