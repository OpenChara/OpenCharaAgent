"""Shared outbound-media helper for BOTH send paths (the in-child
``server/messaging_host`` and the standalone ``messaging/gateway``) — one copy,
so "send a file over the gateway" behaves identically and honestly everywhere.

The send_file tool surfaces an ``Attachment`` whose ``url`` is a same-origin
``/asset?p=<abspath>`` link. A messaging adapter can't fetch that URL, so we
resolve it back to the local path and hand it to the adapter's ``send_media``.
If the platform can't upload files yet (the default seam raises
:class:`DeliveryDeferred`), we send an HONEST text note instead of silently
dropping it — the send_file-over-gateway bug was the tool reporting delivery
while the file never arrived.
"""
from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Callable
from typing import Any

from .base import Adapter, DeliveryDeferred

_log = logging.getLogger("lunamoth.messaging.media")


def asset_local_path(url: str) -> str:
    """The local file path behind a ``/asset?p=<abspath>`` URL, or "" if the URL
    isn't that shape (the platform upload needs the real file, not the URL)."""
    try:
        q = urllib.parse.urlparse(url).query
        return urllib.parse.parse_qs(q).get("p", [""])[0]
    except Exception:  # noqa: BLE001
        return ""


def unsupported_media_note(name: str, caption: str, zh: bool) -> str:
    """The honest line sent when a channel can't upload files — names the file so
    the user knows it exists and where to look (never a silent drop)."""
    head = (caption + " ") if caption else ""
    return head + (f"（已生成文件：{name}，本渠道暂不支持直接发送）" if zh
                   else f"(generated a file: {name} — this channel can't send files yet)")


def deliver_attachment(adapter: Adapter, att: Any, send_text: Callable[[str], None],
                       *, zh: bool = False) -> None:
    """Deliver one send_file ``Attachment`` to *adapter*: try its media upload,
    and on :class:`DeliveryDeferred` fall back to an honest text note via
    *send_text*. Never silently drops; never claims a delivery that didn't happen."""
    source = asset_local_path(att.url) or att.url
    name = att.name or (source.rsplit("/", 1)[-1] if source else "file")
    head = (att.caption + " ") if att.caption else ""
    try:
        adapter.send_media(source, att.mime, att.caption or "")
        return
    except DeliveryDeferred:
        # the platform can't upload files (the default seam) — honest note
        send_text(unsupported_media_note(name, att.caption or "", zh))
    except Exception:  # noqa: BLE001 — a media failure must not crash the relay
        # a real upload failed (timeout/5xx): still tell the user, never drop silently
        _log.exception("send_media via %s failed", adapter.name)
        send_text(head + (f"（文件「{name}」这次没发出去）" if zh
                          else f"(couldn't send the file {name} just now)"))
