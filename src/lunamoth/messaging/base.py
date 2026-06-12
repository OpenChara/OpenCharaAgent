from __future__ import annotations

import abc
import queue
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound item pushed by adapters.

    `sender_id` is the stable platform/user id used for allowlisting.  `reply`
    lets callback-style transports remember a per-message destination; direct
    adapters may ignore it and send to their current configured recipient.
    """

    sender_id: str
    sender_name: str
    text: str
    reply: object | None = None


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

    def close(self) -> None:
        """Stop platform I/O. Adapters with background servers override this."""


AdapterFactory = Callable[[dict], Adapter]
