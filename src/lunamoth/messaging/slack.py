"""Slack bot adapter — Socket Mode WebSocket (inbound) + Web API (outbound).

Socket Mode means no public webhook URL is needed: the bot opens an outbound
WebSocket (``apps.connections.open`` → ``wss://…``) and receives Events API
envelopes over it, ACKing each by ``envelope_id``. Outbound is the Web API
(``chat.postMessage``) over stdlib ``urllib``. Same shape as discord.py: an
asyncio loop inside the sync ``run()`` seam; ``send()`` is a REST POST.

Two tokens (Slack's Socket Mode split): an APP-LEVEL token ``xapp-…`` (with the
``connections:write`` scope) to open the socket, and a BOT token ``xoxb-…`` for
the Web API. Scope (v1, like the other adapters): DMs always, channels only on an
app_mention. Inbound media isn't fetched and outbound files aren't sent in v1
(an honest DeliveryDeferred), matching telegram/qq — text is the contract.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import urllib.request
from typing import Any
from urllib.error import HTTPError

from .base import Adapter, DeliveryDeferred, InboundMessage

_log = logging.getLogger("lunamoth.messaging.slack")

SLACK_API_BASE = "https://slack.com/api"
# Slack's chat.postMessage practical text limit (~40k, but 4000 is the safe
# per-message size before Slack splits/►truncates in the UI).
SLACK_TEXT_MAX = 4000
API_TIMEOUT_S = 15


class SlackAPIError(RuntimeError):
    """A Web API call that returned ok=false (or an HTTP error). Carries the
    method + Slack's error code, never the Authorization header (the token)."""

    def __init__(self, method: str, status: int, error: str) -> None:
        detail = f"Slack {method} failed: HTTP {status}"
        if error:
            detail = f"{detail} {error}"
        super().__init__(detail)
        self.method = method
        self.status = status
        self.error = error


class SlackAPI:
    """Minimal stdlib client for the Slack Web API (Bearer token in a header)."""

    def __init__(self, bot_token: str, app_token: str, *, api_base: str = SLACK_API_BASE, opener=None) -> None:
        self.bot_token = bot_token
        self.app_token = app_token
        self.api_base = str(api_base or SLACK_API_BASE).rstrip("/")
        self._opener = opener or urllib.request.urlopen

    def call(self, method: str, payload: dict[str, Any] | None = None, *, token: str | None = None) -> dict[str, Any]:
        url = f"{self.api_base}/{method}"
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token or self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with self._opener(req, timeout=API_TIMEOUT_S) as resp:
                raw = resp.read()
        except HTTPError as e:
            # `from None`: the request carries the token header — keep it out of tracebacks.
            raise SlackAPIError(method, int(e.code), getattr(e, "reason", "") or "") from None
        out = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(out, dict):
            raise SlackAPIError(method, 200, "non-object response")
        if not out.get("ok"):
            raise SlackAPIError(method, 200, str(out.get("error") or "unknown_error"))
        return out

    def open_connection(self) -> str:
        """apps.connections.open → a single-use wss URL (uses the APP-level token)."""
        out = self.call("apps.connections.open", token=self.app_token)
        url = str(out.get("url") or "")
        if not url:
            raise SlackAPIError("apps.connections.open", 200, "no url in response")
        return url

    def auth_test(self) -> dict[str, Any]:
        return self.call("auth.test")

    def post_message(self, channel: str, text: str) -> dict[str, Any]:
        return self.call("chat.postMessage", {"channel": channel, "text": text})


def parse_event(payload: Any, bot_user_id: str, *, allow_bot: bool = False) -> InboundMessage | None:
    """Normalize a Socket Mode ``events_api`` payload → InboundMessage, or None.

    Engages on a DM message (channel_type == 'im') or an app_mention; ignores the
    bot's own / other bots' messages (loop guard), message edits/deletes
    (subtypes), and empty text.
    """
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if etype not in ("message", "app_mention"):
        return None
    # Loop guard: skip bots (incl. ourselves) and non-user message subtypes
    # (edits, joins, bot_message, etc.).
    if event.get("bot_id") or event.get("subtype"):
        return None
    user = str(event.get("user") or "")
    if not user or (bot_user_id and user == bot_user_id):
        return None
    if etype == "message" and event.get("channel_type") != "im":
        return None  # a plain channel message without a mention → ignore
    text = event.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    clean = text.strip()
    if bot_user_id:
        for token in (f"<@{bot_user_id}>",):
            if clean.startswith(token):
                clean = clean[len(token):].strip()
                break
    channel = str(event.get("channel") or "")
    if not channel:
        return None
    # Prefer the Events API event_id for dedup (stable across Socket Mode retries),
    # else the message ts.
    message_id = str(payload.get("event_id") or event.get("ts") or "")
    return InboundMessage(
        sender_id=user,
        sender_name=user,  # Slack ids are opaque; a display-name lookup is a v2 nicety
        text=clean,
        reply={"channel": channel},
        message_id=message_id,
    )


class SlackAdapter(Adapter):
    """Slack bot over Socket Mode (inbound WS) + the Web API (outbound REST)."""

    max_message_length = SLACK_TEXT_MAX

    def __init__(self, config: dict[str, Any], *, opener=None) -> None:
        self.config = dict(config)
        self.bot_token = str(self.config.get("bot_token") or "").strip()
        self.app_token = str(self.config.get("app_token") or "").strip()
        if not self.bot_token or not self.app_token:
            raise ValueError("Slack adapter needs both config: bot_token (xoxb-…) and app_token (xapp-…)")
        self.owner = str(self.config.get("owner_id") or "").strip()
        self.default_channel = str(self.config.get("channel_id") or "").strip()
        self.api_base = str(self.config.get("api_base") or SLACK_API_BASE).rstrip("/")
        self.allow_bot_messages = bool(self.config.get("allow_bot_messages", False))
        self._api = SlackAPI(self.bot_token, self.app_token, api_base=self.api_base, opener=opener)

        self._closed = threading.Event()
        self._bot_user_id = ""
        self._reply_channel = ""
        self._last_channel = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None

    @property
    def name(self) -> str:
        return "slack"

    def owner_id(self) -> str:
        return self.owner

    def set_reply_target(self, message: InboundMessage) -> None:
        channel = ""
        if isinstance(message.reply, dict):
            channel = str(message.reply.get("channel") or "").strip()
        self._reply_channel = channel
        if channel:
            self._last_channel = channel

    def clear_reply_target(self) -> None:
        self._reply_channel = ""

    def _target_channel(self) -> str:
        return self._reply_channel or self._last_channel or self.default_channel

    def send(self, text: str) -> None:
        target = self._target_channel()
        if not target:
            raise DeliveryDeferred(
                "Slack has no channel to send to yet (no inbound message and no configured "
                "channel_id); this message was dropped, not queued"
            )
        try:
            self._api.post_message(target, text)
        except SlackAPIError as e:
            raise DeliveryDeferred(
                f"Slack send failed ({e.error or e.status}); this message was dropped"
            ) from None
        except OSError as e:
            raise DeliveryDeferred(
                f"Slack send network failure ({type(e).__name__}: {e}); this message was dropped"
            ) from None

    # ---- inbound (Socket Mode WebSocket) ------------------------------------------

    def run(self, inbox: "Any") -> None:
        try:
            me = self._api.auth_test()
            self._bot_user_id = str(me.get("user_id") or "")
            _log.info("Slack bot %s ready", me.get("user") or self._bot_user_id)
        except SlackAPIError as e:
            raise RuntimeError(f"Slack auth.test failed ({e.error or e.status}): check the bot_token") from None
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._socket_loop(inbox))
        finally:
            with contextlib.suppress(Exception):
                self._loop.close()
            self._loop = None

    async def _socket_loop(self, inbox: "Any") -> None:
        try:
            import websockets
        except ModuleNotFoundError as exc:  # pragma: no cover - deploy-time guard
            raise RuntimeError(
                "Slack Socket Mode needs the 'websockets' package. Install with: uv sync --extra server"
            ) from exc
        loop = asyncio.get_event_loop()
        while not self._closed.is_set():
            try:
                url = await loop.run_in_executor(None, self._api.open_connection)
                async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._connection(ws, inbox)
            except Exception as e:  # noqa: BLE001 - keep reconnecting
                if self._closed.is_set():
                    break
                # Log only the exception TYPE — the message can embed the socket URL
                # (which carries a single-use ticket); don't write that to disk.
                _log.warning("Slack socket disconnected (%s); reconnecting in 5s", type(e).__name__)
                await asyncio.sleep(5.0)
            finally:
                self._ws = None

    async def _connection(self, ws: Any, inbox: "Any") -> None:
        async for raw in ws:
            if self._closed.is_set():
                break
            if not await self._handle(json.loads(raw), ws, inbox):
                break  # Slack asked us to reconnect

    async def _handle(self, msg: dict, ws: Any, inbox: "Any") -> bool:
        """Process one Socket Mode frame. Returns False to drop the link."""
        mtype = msg.get("type")
        if mtype == "hello":
            return True
        if mtype == "disconnect":
            # Slack rotates the socket (e.g. 'refresh_requested'); reconnect with a fresh URL.
            _log.info("Slack asked to reconnect (%s)", msg.get("reason") or "disconnect")
            return False
        if mtype in ("events_api", "slash_commands", "interactive"):
            envelope_id = msg.get("envelope_id")
            if envelope_id:  # ACK FIRST so Slack doesn't retry the envelope
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"envelope_id": envelope_id}))
            if mtype == "events_api":
                inbound = parse_event(msg.get("payload"), self._bot_user_id, allow_bot=self.allow_bot_messages)
                if inbound is not None:
                    self._last_channel = str((inbound.reply or {}).get("channel") or "") or self._last_channel
                    inbox.put(inbound)
        return True

    def close(self) -> None:
        self._closed.set()
        loop, ws = self._loop, self._ws
        if loop is not None and not loop.is_closed() and ws is not None:
            def _kill() -> None:
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        asyncio.ensure_future(self._ws.close())
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_kill)
