"""Discord bot adapter — native Gateway WebSocket, no ``discord.py`` dependency.

Discord (unlike Telegram long-poll or QQ/OneBot) REQUIRES a persistent Gateway
WebSocket to receive messages: connect → HELLO → IDENTIFY (with intents) →
heartbeat loop → MESSAGE_CREATE dispatch, with RESUME on a dropped link. This
adapter speaks that protocol directly over the ``websockets`` package (already a
server-extra dep) and sends over the REST API with stdlib ``urllib`` (the same
client style as telegram.py). It fits the SYNC :class:`Adapter` seam by driving
an asyncio loop inside ``run()``; ``send()`` is a plain REST POST on the relay
thread.

Scope (v1, mirroring telegram.py's conservative defaults): replies in DMs always,
and in a guild channel ONLY when the bot is @mentioned (so it never answers every
message in a busy server). MESSAGE_CONTENT is a privileged intent — it must be
enabled for the bot in the Discord developer portal or inbound text arrives empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import urllib.request
import uuid
from typing import Any
from urllib.error import HTTPError

from .base import Adapter, DeliveryDeferred, InboundMessage

_log = logging.getLogger("chara.messaging.discord")

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# Discord's message content limit (UTF-16 code units, like Telegram — the gateway
# splitter counts Python chars, so an astral-heavy chunk can still 400 visibly).
DISCORD_TEXT_MAX = 2000
API_TIMEOUT_S = 15
_USER_AGENT = "DiscordBot (https://lunamoth.ai, 1.0)"

# Bounded in-adapter retries for TRANSIENT outbound failures (429 honors
# Retry-After, 5xx/network back off briefly). Anything else — auth, bad
# channel — is permanent for this message and defers immediately; the relay
# treats DeliveryDeferred as final, so retrying here is the only retry.
_SEND_TRANSIENT_RETRIES = 2
_RETRY_AFTER_CAP_S = 15.0

# Gateway opcodes (https://discord.com/developers/docs/topics/gateway).
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Gateway intents we need: guild messages + DMs + the privileged MESSAGE_CONTENT.
# GUILDS is deliberately OMITTED — we don't need guild metadata and it would pull
# large GUILD_CREATE payloads we'd only discard.
_INTENT_GUILD_MESSAGES = 1 << 9   # 512
_INTENT_DIRECT_MESSAGES = 1 << 12  # 4096
_INTENT_MESSAGE_CONTENT = 1 << 15  # 32768
INTENTS = _INTENT_GUILD_MESSAGES | _INTENT_DIRECT_MESSAGES | _INTENT_MESSAGE_CONTENT


class DiscordAPIError(RuntimeError):
    """A REST call that came back non-2xx. Carries the route + status + Discord's
    message — never the Authorization header (which holds the bot token)."""

    def __init__(self, route: str, status: int, description: str, *, retry_after: float | None = None) -> None:
        detail = f"Discord {route} failed: HTTP {status}"
        if description:
            detail = f"{detail} {description}"
        super().__init__(detail)
        self.route = route
        self.status = status
        self.description = description
        self.retry_after = retry_after


def _retry_after_of(body: Any, headers: Any) -> float | None:
    """Discord's 429 wait, from the JSON body (`retry_after`, seconds) or the
    Retry-After header."""
    try:
        return float(body["retry_after"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return float((headers or {}).get("Retry-After") or "")
    except (TypeError, ValueError, AttributeError):
        return None


class DiscordAPI:
    """Minimal stdlib REST client for the Discord HTTP API (token in a header)."""

    def __init__(self, bot_token: str, *, api_base: str = DISCORD_API_BASE, opener=None) -> None:
        self.bot_token = bot_token
        self.api_base = str(api_base or DISCORD_API_BASE).rstrip("/")
        self._opener = opener or urllib.request.urlopen

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.bot_token}", "User-Agent": _USER_AGENT}

    def _send(self, route: str, req: urllib.request.Request) -> Any:
        try:
            with self._opener(req, timeout=API_TIMEOUT_S) as resp:
                raw = resp.read()
        except HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", errors="replace"))
            except Exception:
                body = {}
            description = str((body or {}).get("message") or getattr(e, "reason", "") or "")
            # `from None`: never chain the original (its request carries the token header).
            raise DiscordAPIError(
                route, int(e.code), description,
                retry_after=_retry_after_of(body, getattr(e, "headers", None)),
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}

    def post_message(self, channel_id: str, content: str) -> Any:
        route = f"POST /channels/{channel_id}/messages"
        url = f"{self.api_base}/channels/{channel_id}/messages"
        data = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        return self._send(route, req)

    def upload_file(self, channel_id: str, filename: str, file_bytes: bytes,
                    mime: str = "application/octet-stream", content: str = "") -> Any:
        """Upload one file (multipart/form-data) — Discord's native attachment send."""
        route = f"POST /channels/{channel_id}/messages (file)"
        url = f"{self.api_base}/channels/{channel_id}/messages"
        boundary = "chara" + uuid.uuid4().hex
        # Sanitize the filename before the Content-Disposition header: strip quotes/CR/LF
        # so a chara-authored workspace name can't malform the multipart body.
        filename = filename.replace('"', "").replace("\r", "").replace("\n", "") or "file"
        payload = json.dumps({"content": content}, ensure_ascii=False)
        pre = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{payload}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n"
        ).encode("utf-8")
        body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        headers = {**self._headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        return self._send(route, req)


def parse_message_create(data: Any, bot_user_id: str, *, allow_bot: bool = False) -> InboundMessage | None:
    """Normalize a MESSAGE_CREATE payload → InboundMessage, or None to ignore.

    Ignores: the bot's own messages and (unless allow_bot) other bots — the
    reply-loop guard; guild messages that don't @mention the bot (so it doesn't
    answer everything in a channel); empty content (no text / MESSAGE_CONTENT
    intent off). DMs (no guild_id) are always considered.
    """
    if not isinstance(data, dict):
        return None
    author = data.get("author")
    if not isinstance(author, dict):
        return None
    author_id = str(author.get("id") or "")
    if author_id and author_id == bot_user_id:
        return None  # never reply to ourselves
    if author.get("bot") and not allow_bot:
        return None  # reply-loop guard against other bots
    channel_id = str(data.get("channel_id") or "")
    if not channel_id:
        return None
    in_guild = bool(data.get("guild_id"))
    if in_guild:
        # Only engage in a server channel when actually addressed.
        mention_ids = {str(m.get("id")) for m in data.get("mentions", []) if isinstance(m, dict)}
        if not bot_user_id or bot_user_id not in mention_ids:
            return None
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        _log.debug("ignored Discord message with no text content (MESSAGE_CONTENT intent off?)")
        return None
    # Strip a leading bot mention (<@id> / <@!id>) so the chara sees a clean prompt.
    text = content.strip()
    for token in (f"<@{bot_user_id}>", f"<@!{bot_user_id}>"):
        if bot_user_id and text.startswith(token):
            text = text[len(token):].strip()
            break
    sender_name = str(author.get("global_name") or author.get("username") or author_id)
    return InboundMessage(
        sender_id=author_id,
        sender_name=sender_name,
        text=text,
        reply={"channel_id": channel_id},
        message_id=str(data.get("id") or ""),
    )


class DiscordAdapter(Adapter):
    """Discord bot over the native Gateway WS (inbound) + REST (outbound).

    Outbound has no "user must message first" rule like Telegram — a bot can post
    to any channel it can see — but it still needs a TARGET channel: a reply uses
    the inbound channel; an unattended speak uses the last channel it saw, else the
    configured ``channel_id``. With neither, an unattended speak is a logged
    DeliveryDeferred (honest non-delivery), never a crash.
    """

    max_message_length = DISCORD_TEXT_MAX

    def __init__(self, config: dict[str, Any], *, opener=None, sleep=None) -> None:
        self.config = dict(config)
        self.bot_token = str(self.config.get("bot_token") or "").strip()
        if not self.bot_token:
            raise ValueError("Discord adapter missing required config: bot_token")
        # The owner's Discord user id (always allowed → empty allow-list = owner-only).
        self.owner = str(self.config.get("owner_id") or "").strip()
        # A default channel for unattended speak before any inbound message arrives.
        self.default_channel = str(self.config.get("channel_id") or "").strip()
        self.api_base = str(self.config.get("api_base") or DISCORD_API_BASE).rstrip("/")
        self.gateway_url = str(self.config.get("gateway_url") or DISCORD_GATEWAY_URL)
        self.allow_bot_messages = bool(self.config.get("allow_bot_messages", False))
        self._api = DiscordAPI(self.bot_token, api_base=self.api_base, opener=opener)

        self._closed = threading.Event()
        # Retry backoff wait — interruptible by close(); injectable for tests.
        self._sleep = sleep or (lambda s: self._closed.wait(s))
        self._reply_channel = ""
        self._last_channel = ""
        # Gateway session state (in-memory; RESUME replays missed events on reconnect).
        self._bot_user_id = ""
        self._session_id = ""
        self._resume_url = ""
        self._seq: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None

    @property
    def name(self) -> str:
        return "discord"

    def owner_id(self) -> str:
        return self.owner

    def set_reply_target(self, message: InboundMessage) -> None:
        # Ephemeral only — pre-auth. The durable _last_channel (the unattended
        # speak destination) moves in remember_peer, post-auth.
        channel = ""
        if isinstance(message.reply, dict):
            channel = str(message.reply.get("channel_id") or "").strip()
        self._reply_channel = channel

    def clear_reply_target(self) -> None:
        self._reply_channel = ""

    def remember_peer(self, message: InboundMessage) -> None:
        channel = ""
        if isinstance(message.reply, dict):
            channel = str(message.reply.get("channel_id") or "").strip()
        if channel:
            self._last_channel = channel

    def _target_channel(self) -> str:
        return self._reply_channel or self._last_channel or self.default_channel

    # ---- outbound (REST) ----------------------------------------------------------

    def _rest_with_retry(self, call, what: str) -> Any:
        """Run one outbound REST call: retry 429 (honoring Retry-After, capped)
        and 5xx/network a bounded number of times, then raise DeliveryDeferred.
        Permanent rejections (auth, bad channel — any other 4xx) defer at once."""
        for attempt in range(_SEND_TRANSIENT_RETRIES + 1):
            try:
                return call()
            except DiscordAPIError as e:
                transient = e.status == 429 or 500 <= e.status < 600
                if not transient or attempt == _SEND_TRANSIENT_RETRIES or self._closed.is_set():
                    raise DeliveryDeferred(
                        f"Discord {what} failed (HTTP {e.status} {e.description}); this message was dropped"
                    ) from None
                if e.status == 429 and e.retry_after is not None:
                    wait = min(e.retry_after, _RETRY_AFTER_CAP_S)
                else:
                    wait = float(attempt + 1)
                _log.warning("Discord %s got HTTP %s; retry %d/%d in %.1fs",
                             what, e.status, attempt + 1, _SEND_TRANSIENT_RETRIES, wait)
                self._sleep(wait)
            except OSError as e:
                if attempt == _SEND_TRANSIENT_RETRIES or self._closed.is_set():
                    raise DeliveryDeferred(
                        f"Discord {what} network failure ({type(e).__name__}: {e}); this message was dropped"
                    ) from None
                _log.warning("Discord %s network failure (%s); retry %d/%d in %.1fs",
                             what, type(e).__name__, attempt + 1, _SEND_TRANSIENT_RETRIES, float(attempt + 1))
                self._sleep(float(attempt + 1))
        return None  # unreachable: the last attempt raised or returned

    def send(self, text: str) -> None:
        target = self._target_channel()
        if not target:
            raise DeliveryDeferred(
                "Discord has no channel to send to yet (no inbound message and no "
                "configured channel_id); this message was dropped, not queued"
            )
        self._rest_with_retry(lambda: self._api.post_message(target, text), "send")

    def send_media(self, source: str, mime: str = "", caption: str = "") -> None:
        target = self._target_channel()
        if not target:
            raise DeliveryDeferred("Discord has no channel to send a file to yet")
        from pathlib import Path

        p = Path(source)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise DeliveryDeferred(f"Discord could not read the file {source}: {e}") from None
        self._rest_with_retry(
            lambda: self._api.upload_file(
                target, p.name, data, mime=mime or "application/octet-stream", content=caption or ""
            ),
            "file upload",
        )

    def send_image(self, url: str, caption: str = "") -> None:
        # Discord auto-embeds a bare image URL posted as content — deliver the link
        # (and any caption) as text so it renders as an inline image, not dropped.
        self.send((f"{caption}\n{url}" if caption else url).strip())

    # ---- inbound (Gateway WebSocket) ----------------------------------------------

    def run(self, inbox: "Any") -> None:
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._gateway_loop(inbox))
        finally:
            with contextlib.suppress(Exception):
                self._loop.close()
            self._loop = None

    async def _gateway_loop(self, inbox: "Any") -> None:
        try:
            import websockets
        except ModuleNotFoundError as exc:  # pragma: no cover - deploy-time guard
            raise RuntimeError(
                "the Discord gateway needs the 'websockets' package. Install with: uv sync --extra server"
            ) from exc
        while not self._closed.is_set():
            url = self._resume_url or self.gateway_url
            try:
                async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._connection(ws, inbox)
            except Exception as e:  # noqa: BLE001 - the gateway must keep reconnecting
                if self._closed.is_set():
                    break
                # Log only the exception TYPE — the message can embed the gateway URL
                # (which carries a short-lived resume token); don't write that to disk.
                _log.warning("Discord gateway disconnected (%s); reconnecting in 5s", type(e).__name__)
                await asyncio.sleep(5.0)
            finally:
                self._ws = None

    async def _connection(self, ws: Any, inbox: "Any") -> None:
        hello = json.loads(await ws.recv())
        if hello.get("op") != _OP_HELLO:
            raise RuntimeError(f"Discord gateway sent op {hello.get('op')} before HELLO")
        interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
        if self._session_id and self._seq is not None:
            await ws.send(json.dumps({"op": _OP_RESUME, "d": {
                "token": self.bot_token, "session_id": self._session_id, "seq": self._seq}}))
        else:
            await ws.send(json.dumps({"op": _OP_IDENTIFY, "d": {
                "token": self.bot_token, "intents": INTENTS,
                "properties": {"os": "linux", "browser": "chara", "device": "chara"}}}))
        hb = asyncio.create_task(self._heartbeat(ws, interval))
        try:
            async for raw in ws:
                if self._closed.is_set():
                    break
                if not await self._handle(json.loads(raw), ws, inbox):
                    break  # a reconnect/invalid-session asked us to drop the link
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hb

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        # Per spec the first beat is jittered; a fixed first interval is fine here.
        while not self._closed.is_set():
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._seq}))

    async def _handle(self, msg: dict, ws: Any, inbox: "Any") -> bool:
        """Process one gateway frame. Returns False to drop the link (→ reconnect)."""
        op = msg.get("op")
        if op == _OP_DISPATCH:
            if isinstance(msg.get("s"), int):
                self._seq = msg["s"]
            t = msg.get("t")
            d = msg.get("d") or {}
            if t == "READY" and isinstance(d, dict):
                user = d.get("user") or {}
                self._bot_user_id = str(user.get("id") or "")
                self._session_id = str(d.get("session_id") or "")
                self._resume_url = str(d.get("resume_gateway_url") or "") + "?v=10&encoding=json" \
                    if d.get("resume_gateway_url") else ""
                _log.info("Discord bot %s ready", user.get("username") or self._bot_user_id)
            elif t == "MESSAGE_CREATE":
                inbound = parse_message_create(d, self._bot_user_id, allow_bot=self.allow_bot_messages)
                if inbound is not None:
                    # _last_channel is updated post-auth via remember_peer, never
                    # here — an unauthorized sender must not become the speak target.
                    inbox.put(inbound)
            return True
        if op == _OP_HEARTBEAT:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._seq}))
            return True
        if op == _OP_HEARTBEAT_ACK:
            return True
        if op == _OP_RECONNECT:
            _log.info("Discord gateway asked to reconnect; resuming")
            with contextlib.suppress(Exception):
                await ws.close(code=4000)
            return False
        if op == _OP_INVALID_SESSION:
            resumable = bool(msg.get("d"))
            if not resumable:
                # Can't resume → start a fresh session next connect.
                self._session_id = ""
                self._seq = None
                self._resume_url = ""
            _log.info("Discord gateway invalid session (resumable=%s); reconnecting", resumable)
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                await ws.close(code=4000)
            return False
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
