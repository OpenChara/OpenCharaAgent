"""Shared outbound-media helper for BOTH send paths (the in-child
``server/messaging_host`` and the standalone ``messaging/gateway``) — one copy,
so "send a file over the gateway" behaves identically and honestly everywhere.

Hermes shape: a chara surfaces a file by writing a ``MEDIA:<path>`` line (or a
bare existing path) in its reply. On a messaging channel we extract those markers
from the COMPLETE reply text (``protocol.media``), strip them from the words we
send, resolve each path against the sandbox, and upload the file natively. If the
platform can't upload yet (the default seam raises :class:`DeliveryDeferred`) — or
a marker points at a file that doesn't resolve — we send an HONEST text note
instead of silently dropping it (the file-over-gateway bug was the relay reporting
a delivery that never happened).
"""
from __future__ import annotations

import logging
import mimetypes
from collections.abc import Callable

from ..protocol import media as mediamod
from .base import Adapter, DeliveryDeferred

_log = logging.getLogger("chara.messaging.media")


def extract_outbound(
    raw_text: str, resolve: Callable[[str], str | None]
) -> tuple[str, list[str], list[str], list[str]]:
    """Pull the file/image markers out of a reply. Returns
    ``(cleaned_text, delivered_abspaths, image_urls, unresolved_rels)``: the
    user-visible text with markers stripped, the local file paths that resolved
    inside the sandbox, the remote image URLs (``![alt](url)``) to send natively,
    and the ``MEDIA:`` paths that did NOT resolve (so the caller sends an honest note
    rather than drop them). ``resolve`` is the sandbox-boundary check
    (CharaHandle.resolve_media).

    Mirrors hermes's run.py chain order exactly — ``extract_media`` →
    ``extract_images`` → ``extract_local_files`` — each consuming the prior cleaned
    text.
    """
    rels, cleaned = mediamod.extract_media(raw_text)
    imgs, cleaned = mediamod.extract_images(cleaned)
    local, cleaned = mediamod.extract_local_files(
        cleaned, exists=lambda p: resolve(p) is not None
    )
    delivered: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for rel in [*rels, *local]:
        abspath = resolve(rel)
        if abspath is None:
            unresolved.append(rel)
        elif abspath not in seen:
            seen.add(abspath)
            delivered.append(abspath)
    image_urls = [url for url, _alt in imgs]
    return cleaned, delivered, image_urls, unresolved


def unsupported_media_note(name: str, caption: str, zh: bool) -> str:
    """The honest line sent when a channel can't upload files — names the file so
    the user knows it exists and where to look (never a silent drop)."""
    head = (caption + " ") if caption else ""
    return head + (f"（已生成文件：{name}，本渠道暂不支持直接发送）" if zh
                   else f"(generated a file: {name} — this channel can't send files yet)")


def missing_media_note(rel: str, zh: bool) -> str:
    """The honest line for a ``MEDIA:`` marker that doesn't resolve to a real file
    inside the sandbox — never silently swallow a promised file."""
    name = rel.rsplit("/", 1)[-1]
    return (f"（想发送「{name}」，但没能找到这个文件）" if zh
            else f"(meant to send {name}, but couldn't find that file)")


def deliver_media(adapter: Adapter, source_path: str, send_text: Callable[[str], None],
                  *, caption: str = "", zh: bool = False) -> None:
    """Deliver one resolved local file to *adapter*: try its native media upload,
    and on :class:`DeliveryDeferred` fall back to an honest text note via
    *send_text*. Never silently drops; never claims a delivery that didn't happen."""
    name = source_path.rsplit("/", 1)[-1]
    mime, _ = mimetypes.guess_type(source_path)
    head = (caption + " ") if caption else ""
    try:
        adapter.send_media(source_path, mime or "", caption or "")
        return
    except DeliveryDeferred:
        # the platform can't upload files (the default seam) — honest note
        send_text(unsupported_media_note(name, caption, zh))
    except Exception:  # noqa: BLE001 — a media failure must not crash the relay
        # a real upload failed (timeout/5xx): still tell the user, never drop silently
        _log.exception("send_media via %s failed", adapter.name)
        send_text(head + (f"（文件「{name}」这次没发出去）" if zh
                          else f"(couldn't send the file {name} just now)"))


def deliver_image_url(adapter: Adapter, url: str, send_text: Callable[[str], None],
                      *, caption: str = "", zh: bool = False) -> None:
    """Deliver one remote image URL (a chara's ``![alt](url)``) to *adapter* as a
    native photo. If the platform can't (DeliveryDeferred) or the send fails, fall
    back to delivering the URL as plain text — the link survives, never silently
    dropped (hermes sends it natively; the text fallback is our honest degrade)."""
    head = (caption + " ") if caption else ""
    try:
        adapter.send_image(url, caption or "")
        return
    except DeliveryDeferred:
        send_text(head + url)  # platform can't send a photo-by-URL — the link as text
    except Exception:  # noqa: BLE001 — a media failure must not crash the relay
        _log.exception("send_image via %s failed", adapter.name)
        send_text(head + url)
