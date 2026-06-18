from __future__ import annotations

import abc
import queue
from collections.abc import Callable
from dataclasses import dataclass, field


class DeliveryDeferred(RuntimeError):
    """A visible non-delivery that should not stop the gateway loop.

    This is for platform rules such as "the human must message first" where no
    fallback route exists, but continuing to listen is the correct behavior.
    """


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound item pushed by adapters.

    `sender_id` is the stable platform/user id used for allowlisting.  `reply`
    lets callback-style transports remember a per-message destination; direct
    adapters may ignore it and send to their current configured recipient.
    `message_id` is the platform's message id when one exists (e.g. OneBot
    message_id): the gateway dedups redeliveries on it, so a retried
    callback or a post-reconnect redelivery never runs a second LLM turn.
    Empty = no platform id = never deduplicated.

    `attachments` carries wire-shape attachment dicts the agent's ingest path
    understands: either inline-able bytes as base64 (``{"name","mime","data"}``,
    `data` = base64 of the raw file bytes) OR a not-yet-fetchable media
    reference (``{"name","mime","url"|"path","kind"}``, `kind` ∈
    image|file|sticker). The default empty tuple keeps every existing
    construction (text-only) working unchanged.
    """

    sender_id: str
    sender_name: str
    text: str
    reply: object | None = None
    message_id: str = ""
    attachments: tuple = field(default_factory=tuple)


class Adapter(abc.ABC):
    """Small AstrBot-style seam for messaging platforms.

    `run()` owns platform I/O and pushes normalized :class:`InboundMessage`
    objects into the shared inbox.  `send()` emits text back to the platform.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def run(self, inbox: "queue.Queue[InboundMessage]") -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def send(self, text: str) -> None:
        raise NotImplementedError

    def send_media(self, source: str, mime: str = "", caption: str = "") -> None:
        """Send a file/image to the platform. `source` is a local filesystem path.

        The DEFAULT raises :class:`DeliveryDeferred` — a platform that can't (yet)
        upload media says so honestly, and the host falls back to a text note
        rather than pretending the file was delivered (the file-over-gateway
        bug). Override per platform with a real upload (iLink media, etc.)."""
        raise DeliveryDeferred(f"{self.name} cannot send files on this channel yet")

    def send_image(self, url: str, caption: str = "") -> None:
        """Send a REMOTE image URL to the platform as a native photo (hermes's
        ![alt](url) path). `url` is an http(s) link the platform fetches itself.

        The DEFAULT raises :class:`DeliveryDeferred` — a platform that can't send a
        remote image natively says so, and the host falls back to delivering the URL
        as plain text (the link survives, never silently dropped). Override per
        platform with a real photo-by-URL send."""
        raise DeliveryDeferred(f"{self.name} cannot send image URLs on this channel yet")

    def set_reply_target(self, message: InboundMessage) -> None:
        """Select the destination for sends caused by one inbound message.

        Most adapters can ignore this and keep their own current recipient.
        Direct chat adapters use it so replies go to the inbound sender while
        unattended speak output can still use their configured default peer.
        """

    def clear_reply_target(self) -> None:
        """Clear the per-inbound destination selected by :meth:`set_reply_target`."""

    def close(self) -> None:
        """Stop platform I/O. Adapters with background servers override this."""

    def needs_login(self) -> bool:
        """True when this adapter can't run until an interactive login the
        operator must complete out of band (e.g. a WeChat QR scan).

        The in-process host (server/messaging_host.py) checks this BEFORE
        starting an adapter: a not-yet-logged-in adapter is left pending rather
        than spun up, so it never opens its own QR/login session competing with
        the app's QR flow on the same account. Most adapters use static
        credentials and never need this — the default is False."""
        return False


AdapterFactory = Callable[[dict], Adapter]
