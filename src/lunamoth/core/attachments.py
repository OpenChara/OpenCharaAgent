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

# Small images ride inline as a base64 data URL (the model sees the pixels, and
# they persist in history/transcript). Anything larger is copied to workspace and
# referenced by path — keeps the prompt and the SQLite transcript from bloating.
INLINE_IMAGE_MAX_BYTES = 1_500_000

_UPLOAD_DIR = "uploads"


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
    raws: list[RawAttachment], *, sandbox: Any, vision_ok: bool
) -> IngestResult:
    """Turn raw attachments into (inline image parts, text notes, notices, saved paths).

    ``sandbox`` is a :class:`~lunamoth.tools.sandbox.Sandbox` (needs ``write_bytes``).
    ``vision_ok`` is whether the active model can see images.
    """
    res = IngestResult()
    for att in raws:
        if not att or not att.data:
            continue
        if att.is_image and vision_ok and len(att.data) <= INLINE_IMAGE_MAX_BYTES:
            b64 = base64.b64encode(att.data).decode("ascii")
            res.content_parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{att.mime};base64,{b64}"}}
            )
            res.notes.append(f"[图片 / image: {att.name}]")
        elif att.is_image and vision_ok:
            rel = _save(sandbox, att)
            res.saved.append(rel)
            res.notes.append(
                f"[图片已保存到 workspace/{rel}（过大未内联，可用工具查看）"
                f" / image saved to workspace/{rel} (too large to inline; read it with tools)]"
            )
        elif att.is_image:  # model has no vision
            rel = _save(sandbox, att)
            res.saved.append(rel)
            res.notes.append(f"[图片 / image: {att.name} → workspace/{rel}]")
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
