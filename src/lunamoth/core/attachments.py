"""Ingest user-sent attachments (images + files) into a turn.

Follows the hermes shape: an image the model can actually *see* is injected
DIRECTLY into the user message as an OpenAI ``image_url`` content part (not via a
tool call); a file, an oversized image, or any image when the model has no vision
is copied into the chara's ``workspace/uploads/`` and referenced by a text note,
so the chara reads it with its own tools. No fabricated descriptions, no silent
drops — a model that can't see an image says so and the bytes are kept on disk.

The wire shape (web ``send`` RPC / messaging adapters) for one attachment::

    {"name": "photo.png", "mime": "image/png", "size": 12345, "data": "<base64>"}

``data`` is base64 of the raw bytes (no ``data:`` prefix). See
``docs/multimodal-contract.md``.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

# hermes native-image shape: an image is inlined as a base64 data URL at FULL size
# (the model sees the real pixels). We do NOT shrink proactively. If a provider
# rejects the request as image-too-large, the LLM layer reactively shrinks the
# data-URL parts to the target below and retries once (see llm._shrink_request_images).
# Prompt/transcript stay bounded elsewhere: compaction.strip_old_images collapses all
# but the newest image to a text handle, and transcript persistence strips the data
# URL to a handle (transcript._strip_inline_images), so the SQLite log never bloats.

# Reactive-shrink target: 4MB base64 data URL (headroom under Anthropic's 5MB cap),
# plus an 8000px per-side cap Anthropic enforces independently. Verbatim from hermes
# (agent/conversation_compression.try_shrink_image_parts_in_messages).
SHRINK_TARGET_BYTES = 4 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8000

_UPLOAD_DIR = "uploads"


def shrink_image_to_inline(data: bytes, mime: str) -> tuple[bytes, str] | None:
    """Downscale/re-encode an image so its base64 data URL fits ``SHRINK_TARGET_BYTES``
    and the longest side is ≤ ``MAX_IMAGE_DIMENSION`` — the reactive recovery from a
    provider image-too-large rejection.

    Ported from hermes (agent/conversation_compression.try_shrink_image_parts +
    tools/vision_tools._resize_image_for_vision): pick JPEG (PNG for png sources),
    flatten alpha for JPEG, then step quality down (85→70→50) and halve dimensions
    (LANCZOS, floor 64px) until it fits. Returns ``(bytes, out_mime)`` when it fits,
    or ``None`` (Pillow missing / corrupt / can't fit) so the caller surfaces the
    original error — never a fabricated or silently-dropped image."""
    try:
        from PIL import Image
    except ImportError:
        return None
    import io
    try:
        out_mime = "image/png" if mime == "image/png" else "image/jpeg"
        fmt = "PNG" if out_mime == "image/png" else "JPEG"
        img = Image.open(io.BytesIO(data))
        img.load()
        if fmt == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        quality_steps: tuple = (85, 70, 50) if fmt == "JPEG" else (None,)
        for _ in range(12):  # bounded: 12 halvings reduces any real image to <64px
            for q in quality_steps:
                buf = io.BytesIO()
                kw: dict[str, Any] = {"format": fmt}
                if q is not None:
                    kw["quality"] = q
                img.save(buf, **kw)
                out = buf.getvalue()
                # Gate on the base64 data-URL length (≈ raw·4/3), as hermes does.
                if (len(out) * 4 // 3 + 64) <= SHRINK_TARGET_BYTES and max(img.size) <= MAX_IMAGE_DIMENSION:
                    return out, out_mime
            w, h = img.size
            if max(w, h) <= 64:
                break
            img = img.resize((max(int(w * 0.5), 64), max(int(h * 0.5), 64)), Image.LANCZOS)
        return None
    except Exception:  # noqa: BLE001 — corrupt/unsupported → surface the original error
        return None


def shrink_data_url(url: str) -> str | None:
    """Re-encode an oversized ``data:image/...;base64,...`` URL to fit the shrink
    target — the reactive recovery applied to a request's image parts. Returns a
    smaller data URL, or ``None`` if it isn't a data URL, can't be shrunk, or the
    result isn't smaller. Mirrors hermes ``_shrink_data_url``."""
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    header, _, b64 = url.partition(",")
    mime = "image/jpeg"
    if header.startswith("data:"):
        m = header[len("data:"):].split(";", 1)[0].strip()
        if m.startswith("image/"):
            mime = m
    try:
        raw = base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return None
    shrunk = shrink_image_to_inline(raw, mime)
    if shrunk is None:
        return None
    out, out_mime = shrunk
    new_url = f"data:{out_mime};base64," + base64.b64encode(out).decode("ascii")
    return new_url if len(new_url) < len(url) else None


@dataclass
class RawAttachment:
    name: str
    mime: str
    data: bytes

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")

    @classmethod
    def from_wire(cls, d: Any) -> "RawAttachment | None":
        """Build from one wire dict; return None for anything malformed (a bad
        attachment must never take down the whole turn)."""
        if not isinstance(d, dict):
            return None
        raw = d.get("data")
        if not isinstance(raw, str) or not raw:
            return None
        # Tolerate a "data:<mime>;base64,<...>" prefix if a client sent one.
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return None
        if not data:
            return None
        name = str(d.get("name") or "attachment").strip() or "attachment"
        # Strip any path components a client may have leaked into the name.
        name = name.replace("\\", "/").rsplit("/", 1)[-1] or "attachment"
        mime = str(d.get("mime") or "").strip().lower()
        if not mime:
            mime = _guess_mime(name)
        return cls(name=name, mime=mime, data=data)


@dataclass
class IngestResult:
    content_parts: list[dict] = field(default_factory=list)  # inline image_url parts
    notes: list[str] = field(default_factory=list)           # appended to the user text
    notices: list[str] = field(default_factory=list)         # user-facing Notice texts
    saved: list[str] = field(default_factory=list)           # workspace-relative paths


def ingest_attachments(
    raws: list[RawAttachment], *, sandbox: Any, vision_ok: bool,
    describe: "Any" = None,
) -> IngestResult:
    """Turn raw attachments into (inline image parts, text notes, notices, saved paths).

    ``sandbox`` is a :class:`~lunamoth.tools.sandbox.Sandbox` (needs ``write_bytes``).
    ``vision_ok`` is whether the active model can see images.
    ``describe(data, mime) -> str | None`` is the OPTIONAL auxiliary vision
    describer (llm.describe_image): when the main model has no vision, an image
    is handed to a separate vision model and the returned text is injected so the
    main model still "sees" it (hermes auxiliary task=vision). Returns None when
    no vision model is configured → the honest "saved to disk" note stands.
    """
    res = IngestResult()
    for att in raws:
        if not att or not att.data:
            continue
        if att.is_image and vision_ok:
            # hermes native shape: inline the image at FULL size (the model sees the
            # real pixels). A provider that rejects it as too-large triggers the
            # reactive shrink+retry in the LLM layer (llm._shrink_request_images);
            # compaction.strip_old_images keeps only the newest image's pixels live.
            b64 = base64.b64encode(att.data).decode("ascii")
            res.content_parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{att.mime};base64,{b64}"}}
            )
            res.notes.append(f"[图片 / image: {att.name}]")
        elif att.is_image:  # main model has no vision
            rel = _save(sandbox, att)
            res.saved.append(rel)
            res.notes.append(f"[图片 / image: {att.name} → workspace/{rel}]")
            # Auxiliary vision: a separate model describes the image and the text
            # is fed back, so a non-vision main model still understands it. If no
            # vision model is configured (describe → None), the honest note stands.
            desc = None
            if describe is not None:
                try:
                    desc = describe(att.data, att.mime)
                except Exception:  # noqa: BLE001 — a failed describe keeps the honest note
                    desc = None
            if desc:
                res.notes.append(
                    f"[图片内容 / image contents, described by the vision model: {desc}]\n"
                    f"(To look again, read workspace/{rel}.)"
                )
            else:
                res.notices.append(
                    f"当前模型不支持图像；图片已存到 workspace/{rel}。"
                    f" Current model has no vision; the image was saved to workspace/{rel}."
                )
        else:  # non-image file
            rel = _save(sandbox, att)
            res.saved.append(rel)
            res.notes.append(
                f"[用户上传文件 / file: {att.name} → workspace/{rel}]"
            )
    return res


def build_user_content(text: str, res: IngestResult) -> Any:
    """Compose the ``content`` field for the user message: a plain string when
    there are no inline images, else an OpenAI multi-part list (text + images)."""
    body = text
    if res.notes:
        joined = "\n".join(res.notes)
        body = f"{text}\n{joined}" if text else joined
    if res.content_parts:
        head = [{"type": "text", "text": body}] if body else []
        return head + res.content_parts
    return body


def _save(sandbox: Any, att: RawAttachment) -> str:
    return sandbox.write_bytes(f"{_UPLOAD_DIR}/{att.name}", att.data)


_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".heic": "image/heic", ".tiff": "image/tiff",
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".json": "application/json", ".csv": "text/csv",
}


def _guess_mime(name: str) -> str:
    lower = name.lower()
    for ext, mime in _EXT_MIME.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"
