"""Messaging gateway adapters.

Messaging frontends are deliberately narrower than panoramic frontends: they
deliver only :class:`TextDelta(channel="say") <lunamoth.protocol.events.TextDelta>`
to external platforms. Muse/thinking/tool events stay inside the house.
"""

from .base import Adapter, InboundMessage

__all__ = ["Adapter", "InboundMessage"]
