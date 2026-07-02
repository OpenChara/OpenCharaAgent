from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from .base import Adapter, DeliveryDeferred, InboundMessage

_log = logging.getLogger("lunamoth.messaging.telegram")

DEFAULT_API_BASE = "https://api.telegram.org"
LONG_POLL_TIMEOUT_S = 25
# The HTTP socket timeout must outlive the server-side long poll.
LONG_POLL_SOCKET_SLACK_S = 10
API_TIMEOUT_S = 15
# Telegram's sendMessage limit. NOTE (hermes gateway/platforms/base.py
# utf16_len): the platform counts UTF-16 code units, so astral characters
# (emoji, CJK Extension B) count double. The gateway splitter counts Python
# chars; an astral-heavy chunk can therefore still exceed the platform limit,
# which surfaces as a visible sendMessage 400 — never a silent truncation.
TELEGRAM_TEXT_MAX = 4096


def _normalize_api_base(value: Any) -> str:
    text = str(value or DEFAULT_API_BASE).strip().rstrip("/")
    return text or DEFAULT_API_BASE


def default_state_path() -> Path:
    root = os.getenv("LUNAMOTH_CONFIG_DIR")
    if root:
        return Path(root).expanduser().resolve() / "telegram_state.json"
    session = os.getenv("LUNAMOTH_SESSION", "")
    if session:
        return Path.home().expanduser() / ".lunamoth" / "sessions" / session / "telegram_state.json"
    return Path("telegram_state.json").resolve()


class TelegramAPIError(RuntimeError):
    """A Bot API call that came back non-ok (HTTP error status or ok=false).

    The message carries the method, status, and Telegram's description — never
    the request URL, because the URL embeds the bot token.
    """

    def __init__(self, method: str, status: int, description: str, *, retry_after: float | None = None) -> None:
        detail = f"Telegram {method} failed: HTTP {status}"
        if description:
            detail = f"{detail} {description}"
        super().__init__(detail)
        self.method = method
        self.status = status
        self.description = description
        self.retry_after = retry_after


def _retry_after_of(payload: dict[str, Any]) -> float | None:
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    try:
        return float(parameters["retry_after"])
    except (KeyError, TypeError, ValueError):
        return None


class TelegramAPI:
    """Small urllib client for the Telegram Bot API (stdlib only)."""

    def __init__(self, bot_token: str, *, api_base: str = DEFAULT_API_BASE, opener=None) -> None:
        self.bot_token = bot_token
        self.api_base = _normalize_api_base(api_base)
        self._opener = opener or urllib.request.urlopen

    def call(self, method: str, payload: dict[str, Any] | None = None, *, timeout_s: float = API_TIMEOUT_S) -> Any:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with self._opener(req, timeout=timeout_s) as resp:
                raw = resp.read()
        except HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", errors="replace"))
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            description = str(body.get("description") or getattr(e, "reason", "") or "")
            # `from None`: HTTPError carries the request URL — and the URL
            # embeds the bot token — so the original is never chained into
            # tracebacks or logs (hermes: redact tokens in ALL log output).
            raise TelegramAPIError(method, int(e.code), description, retry_after=_retry_after_of(body)) from None
        out = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(out, dict):
            raise RuntimeError(f"Telegram {method} returned non-object JSON")
        if not out.get("ok"):
            # Self-hosted Bot API servers may answer 200 with ok=false.
            description = str(out.get("description") or "")
            try:
                status = int(out.get("error_code") or 0)
            except (TypeError, ValueError):
                status = 0
            raise TelegramAPIError(method, status, description, retry_after=_retry_after_of(out))
        return out.get("result")


# Common Bot API attachment keys, only so the debug log can say WHAT was
# ignored (like weixin's item-type filtering).
_MEDIA_KEYS = frozenset(
    {"photo", "video", "audio", "voice", "document", "sticker", "animation",
     "video_note", "location", "venue", "contact", "poll", "dice"}
)


def parse_update(update: Any) -> InboundMessage | None:
    """Normalize one getUpdates entry; private-chat new text messages only.

    Group support is intentionally out of scope for v1: hermes' group story
    needs mention patterns, per-chat-type authorization, and forum/topic
    thread routing — none of which is cheap or safe to port here.
    """

    if not isinstance(update, dict):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        kinds = sorted(k for k in update if k != "update_id")
        if kinds:
            _log.debug("ignored Telegram update kind %s (v1 handles new messages only)", ", ".join(kinds))
        return None
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    chat_type = str(chat.get("type") or "") if isinstance(chat, dict) else ""
    if chat_id is None:
        _log.debug("ignored Telegram message without chat id")
        return None
    if chat_type != "private":
        _log.debug("ignored Telegram chat type=%s (v1 handles private chats only)", chat_type or "(unknown)")
        return None
    sender = message.get("from")
    if isinstance(sender, dict) and sender.get("is_bot"):
        _log.debug("ignored Telegram message from a bot (reply-loop guard)")
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        kinds = sorted(set(message) & _MEDIA_KEYS)
        _log.debug("ignored non-text Telegram message (%s); v1 is text-only", ", ".join(kinds) or "no text")
        return None
    sender_name = str(chat_id)
    if isinstance(sender, dict):
        sender_name = str(sender.get("first_name") or sender.get("username") or chat_id)
    update_id = update.get("update_id")
    return InboundMessage(
        sender_id=str(chat_id),
        sender_name=sender_name,
        text=text.strip(),
        reply={"chat_id": str(chat_id)},
        # update_id is unique per bot and survives redeliveries after an
        # unconfirmed offset; it keys the gateway's inbound dedup.
        message_id=str(update_id) if update_id is not None else "",
    )


class TelegramAdapter(Adapter):
    """Telegram bot adapter over long-poll getUpdates (no public URL, no webhook).

    Private text chats only for v1. The confirmed offset persists in
    telegram_state.json (0600, atomic replace) so a restart never replays old
    messages. A bot cannot message a user who has never messaged it, so
    unattended speak before first contact is a logged DeliveryDeferred; the
    last private chat id persists in the state file so unattended speak keeps
    a destination across restarts.
    """

    max_message_length = TELEGRAM_TEXT_MAX

    def __init__(
        self,
        config: dict[str, Any],
        *,
        opener=None,
        state_path: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = dict(config)
        self.bot_token = str(self.config.get("bot_token") or "").strip()
        if not self.bot_token:
            raise ValueError("Telegram adapter missing required config: bot_token")
        # The owner's numeric chat/user id (from @userinfobot). Always allowed, so an
        # empty allow-list = owner-only rather than locked out. Configured, not the
        # first-to-/start, so a stranger can't claim ownership.
        self.owner = str(self.config.get("owner_id") or "").strip()
        self.api_base = _normalize_api_base(self.config.get("api_base"))
        self.state_path = Path(state_path).expanduser().resolve() if state_path is not None else default_state_path()
        self._sleep = sleep
        self._monotonic = monotonic
        self._closed = threading.Event()
        self._state_lock = threading.RLock()
        self._api = TelegramAPI(self.bot_token, api_base=self.api_base, opener=opener)

        self.offset: int | None = None
        self.last_chat_id = ""
        self._reply_target = ""
        self._flood_until = 0.0
        self._load_state()

    @property
    def name(self) -> str:
        return "telegram"

    def owner_id(self) -> str:
        return self.owner

    def set_reply_target(self, message: InboundMessage) -> None:
        # Ephemeral only — pre-auth. The durable last_chat_id (the unattended
        # speak destination, persisted) moves in remember_peer, post-auth.
        self._reply_target = str(message.sender_id).strip()

    def clear_reply_target(self) -> None:
        self._reply_target = ""

    def remember_peer(self, message: InboundMessage) -> None:
        peer = str(message.sender_id).strip()
        if peer and peer != self.last_chat_id:
            self.last_chat_id = peer
            self._save_state()

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Telegram state file {self.state_path} is unreadable: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"Telegram state file {self.state_path} must contain a JSON object")
        offset = data.get("offset")
        if isinstance(offset, int):
            self.offset = offset
        self.last_chat_id = str(data.get("last_chat_id") or "").strip()

    def _save_state(self) -> None:
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(
                {"offset": self.offset, "last_chat_id": self.last_chat_id},
                ensure_ascii=False, indent=2, sort_keys=True,
            )
            tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.write("\n")
                os.replace(tmp, self.state_path)
                os.chmod(self.state_path, 0o600)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def startup_check(self) -> dict[str, Any]:
        """getMe once: a bad token is a clear startup error, never a retry loop."""

        try:
            me = self._api.call("getMe")
        except TelegramAPIError as e:
            if e.status == 401:
                raise RuntimeError(
                    "Telegram bot token rejected (401 Unauthorized): "
                    "check adapters.telegram.bot_token in messaging.json"
                ) from None
            raise
        if not isinstance(me, dict):
            raise RuntimeError("Telegram getMe returned no bot object")
        _log.info("Telegram bot @%s ready", me.get("username") or me.get("id"))
        # A leftover webhook makes every getUpdates fail with 409; clearing it
        # is safe and idempotent (hermes telegram.py does the same at startup).
        try:
            self._api.call("deleteWebhook", {"drop_pending_updates": False})
        except Exception as e:
            _log.warning("Telegram deleteWebhook failed (getUpdates may 409 while a webhook is active): %s", e)
        return me

    def poll_once(self, inbox: "queue.Queue[InboundMessage]") -> int:
        """Run one getUpdates long poll. Exposed for tests."""

        payload: dict[str, Any] = {
            "timeout": LONG_POLL_TIMEOUT_S,
            "allowed_updates": ["message"],
        }
        if self.offset is not None:
            payload["offset"] = self.offset
        result = self._api.call(
            "getUpdates", payload, timeout_s=LONG_POLL_TIMEOUT_S + LONG_POLL_SOCKET_SLACK_S
        )
        if not isinstance(result, list):
            raise RuntimeError("Telegram getUpdates returned a non-list result")
        count = 0
        dirty = False
        for update in result:
            if isinstance(update, dict):
                update_id = update.get("update_id")
                if isinstance(update_id, int) and (self.offset is None or update_id + 1 > self.offset):
                    self.offset = update_id + 1
                    dirty = True
            msg = parse_update(update)
            if msg is not None:
                # last_chat_id (the persisted unattended-speak destination) is
                # updated post-auth via remember_peer, never from the raw poll —
                # a stranger's DM must not hijack the speak destination.
                inbox.put(msg)
                count += 1
        if dirty:
            self._save_state()
        return count

    def run(self, inbox: "queue.Queue[InboundMessage]") -> None:
        # The one-shot startup check rides the same retry cadence as the poll
        # loop: a transient API/network blip must not permanently kill inbound.
        # Only startup_check's own RuntimeError (rejected token / malformed
        # getMe) stays fatal — never a retry loop on a bad token.
        while not self._closed.is_set():
            try:
                self.startup_check()
                break
            except TelegramAPIError as e:
                if self._closed.is_set():
                    break
                _log.warning("Telegram getMe failed (%s); retrying in 5s", e)
                self._sleep(5.0)
            except RuntimeError:
                raise
            except Exception as e:
                if self._closed.is_set():
                    break
                _log.warning("Telegram startup network error (%s: %s); retrying in 5s", type(e).__name__, e)
                self._sleep(5.0)
        while not self._closed.is_set():
            try:
                self.poll_once(inbox)
            except TelegramAPIError as e:
                if self._closed.is_set():
                    break
                if e.status == 401:
                    raise RuntimeError(
                        "Telegram bot token rejected (401 Unauthorized): "
                        "check adapters.telegram.bot_token in messaging.json"
                    ) from None
                if e.status == 409:
                    # Another getUpdates client (or a webhook) holds the slot;
                    # hermes sees this when an old process' long poll lingers.
                    _log.error("Telegram getUpdates conflict (409): another bot instance or webhook is active; retrying in 10s")
                    self._sleep(10.0)
                    continue
                if e.status == 429:
                    wait = min(e.retry_after if e.retry_after else 5.0, 60.0)
                    _log.warning("Telegram getUpdates flood control: waiting %.0fs (retry_after)", wait)
                    self._sleep(wait)
                    continue
                _log.warning("Telegram getUpdates error: %s; retrying in 5s", e)
                self._sleep(5.0)
            except RuntimeError:
                raise
            except Exception as e:
                if self._closed.is_set():
                    break
                _log.warning("Telegram getUpdates network error (%s: %s); retrying in 5s", type(e).__name__, e)
                self._sleep(5.0)

    def send(self, text: str) -> None:
        now = self._monotonic()
        if now < self._flood_until:
            raise DeliveryDeferred(
                f"Telegram flood control active for another {self._flood_until - now:.0f}s; "
                "this message was dropped, not queued"
            )
        target = self._reply_target or self.last_chat_id
        if not target:
            raise DeliveryDeferred(
                "Telegram bots cannot message a user first; "
                "waiting for the human to say hi (no chat id on record)"
            )
        chat_id: int | str = int(target) if target.lstrip("-").isdigit() else target
        try:
            self._api.call("sendMessage", {"chat_id": chat_id, "text": text})
        except TelegramAPIError as e:
            if e.status == 429:
                # Honor retry_after WITHOUT sleeping the gateway loop: open a
                # flood window so sends until then defer visibly, and drop this
                # one (DeliveryDeferred = logged non-delivery, no queueing).
                wait = e.retry_after if e.retry_after is not None else 1.0
                self._flood_until = self._monotonic() + wait
                _log.warning("Telegram flood control on send (retry_after=%.0fs); message dropped, not queued", wait)
                raise DeliveryDeferred(
                    f"Telegram flood control: retry after {wait:.0f}s; this message was dropped, not queued"
                ) from None
            raise
        except OSError as e:
            # Network failure (DNS, refused connection, socket timeout): a
            # visible deferral, mirroring the QQ disconnected-send semantics.
            raise DeliveryDeferred(
                f"Telegram sendMessage network failure ({type(e).__name__}: {e}); "
                "this message was dropped, not queued"
            ) from None

    def close(self) -> None:
        self._closed.set()
