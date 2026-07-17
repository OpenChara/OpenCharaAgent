"""Shared messaging access-control: the allow-list + the refusal throttle.

Both the standalone :class:`~chara.messaging.gateway.MessagingGateway` (own
agent + idle loop) and the in-child :class:`~chara.server.messaging_host.
MessagingHost` (shared agent) gate inbound messages on the same two rules.
Keeping them here means a change lands in BOTH paths instead of drifting — the
"empty allow-list = open" fix previously had to be applied to each by hand.
"""
from __future__ import annotations

import logging
from datetime import datetime

_log = logging.getLogger("chara.messaging.access")


def warn_if_open_allowlist(allowed, channel: str = "", owner_id: str = "") -> bool:
    """Surface a messaging gateway's access posture at start, loudly when risky.

    * ``"*"`` in the list → truly OPEN (anyone can reach a tool-capable agent):
      a WARNING, the #1 misconfiguration risk. Returns True.
    * empty list AND no resolvable owner → CLOSED to everyone, so NOBODY can
      reach the chara: a WARNING (the operator must set ``allowed_senders`` or the
      platform's owner id). Returns False.
    * empty list with an owner → owner-only (the safe default): quiet info.
    * a non-empty list → restricted: quiet info.
    """
    if "*" in (allowed or set()):
        _log.warning(
            "messaging gateway%s started OPEN (allow-list contains '*': anyone can "
            "reach this chara, which has tool access). Remove '*' to restrict it.",
            f" [{channel}]" if channel else "",
        )
        return True
    if not allowed and not owner_id:
        _log.warning(
            "messaging gateway%s has an EMPTY allow-list and no resolvable owner — "
            "NOBODY can reach this chara. Set allowed_senders (or the platform's "
            "owner id) so at least you can.",
            f" [{channel}]" if channel else "",
        )
    return False


def sender_allowed(sender_id: str, allowed: set[str], owner_id: str = "") -> bool:
    """Whether `sender_id` may reach the chara.

    The bound OWNER (``owner_id``, e.g. WeChat's logged-in account / QQ peer / the
    configured Telegram owner) is ALWAYS allowed — so an empty allow-list means
    "only me", never "locked out" (the WeChat case: its sender id is opaque and
    can't be typed into a list). ``"*"`` is the explicit "open to everyone" opt-in;
    a non-empty list restricts to its members (plus the owner).
    """
    if owner_id and sender_id == owner_id:
        return True
    if "*" in allowed:
        return True
    if not allowed:
        return False  # empty = owner-only (handled above), closed to strangers
    return sender_id in allowed


class RefusalThrottle:
    """Emit at most one 'unauthorized sender' refusal per sender per day.

    OneBot redelivers after a reconnect (and any callback platform retries an
    unacked delivery), so an unknown sender can hit us repeatedly; we tell them no once a day, then
    stay silent (audit: never spam, never run a turn for them).
    """

    def __init__(self) -> None:
        self._last_day: dict[str, str] = {}

    def allow(self, sender_id: str) -> bool:
        """Return True at most once per sender per calendar day; the caller
        sends the refusal text when this returns True."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_day.get(sender_id) == today:
            return False
        self._last_day[sender_id] = today
        return True
