from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from .base import Adapter, DeliveryDeferred, InboundMessage

_log = logging.getLogger("lunamoth.messaging.qq")

QQ_TEXT_MAX = 4500


def onebot_message_text(message: Any) -> str:
    """Concatenate OneBot v11 text segments and ignore everything else."""

    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    ignored: set[str] = set()
    for segment in message:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "").strip()
        data = segment.get("data")
        if seg_type == "text" and isinstance(data, dict):
            parts.append(str(data.get("text") or ""))
        elif seg_type:
            ignored.add(seg_type)
    if ignored:
        _log.debug("ignored unsupported OneBot segment types: %s", ", ".join(sorted(ignored)))
    return "".join(parts).strip()


def parse_onebot_event(raw: str | bytes | bytearray) -> InboundMessage | None:
    try:
        if isinstance(raw, str):
            event = json.loads(raw)
        else:
            event = json.loads(bytes(raw).decode("utf-8", errors="replace"))
    except (TypeError, ValueError, json.JSONDecodeError):
        _log.debug("ignored non-JSON OneBot frame")
        return None
    if not isinstance(event, dict):
        return None
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "private":
        _log.debug("ignored OneBot message_type=%s (v1 handles private only)", event.get("message_type"))
        return None
    sender_id = str(event.get("user_id") or "").strip()
    if not sender_id:
        return None
    text = onebot_message_text(event.get("message"))
    if not text:
        return None
    sender = event.get("sender")
    sender_name = sender_id
    if isinstance(sender, dict):
        sender_name = str(sender.get("nickname") or sender.get("card") or sender_id)
    return InboundMessage(
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        reply={"user_id": sender_id},
        # NapCat/Lagrange can redeliver events after a reconnect; message_id
        # lets the gateway drop the duplicate instead of running a second turn.
        message_id=str(event.get("message_id") or ""),
    )


class QQAdapter(Adapter):
    """OneBot v11 forward-WebSocket client for NapCat/Lagrange.

    LunaMoth is the WebSocket client. QQ login and QR scanning happen entirely
    in the user-run NapCat/Lagrange WebUI; this adapter never opens a listener.
    """

    max_message_length = QQ_TEXT_MAX

    def __init__(
        self,
        config: dict[str, Any],
        *,
        connect_func: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        uuid_factory: Callable[[], str] | None = None,
        recv_timeout: float = 1.0,
    ) -> None:
        self.config = dict(config)
        self.url = str(self.config.get("url") or "").strip()
        self.access_token = str(self.config.get("access_token") or "").strip()
        self.peer_id = str(self.config.get("peer_id") or "").strip()
        self._connect_func = connect_func
        self._sleep = sleep
        self._uuid_factory = uuid_factory or (lambda: uuid.uuid4().hex)
        self._recv_timeout = recv_timeout
        self._closed = threading.Event()
        self._socket_lock = threading.RLock()
        self._socket: Any | None = None
        self._reply_target = ""

    @property
    def name(self) -> str:
        return "qq"

    def owner_id(self) -> str:
        # The configured peer (the intended human) is the owner — always allowed,
        # so an empty allow-list = owner-only rather than open to any stranger.
        return self.peer_id

    def _validate(self) -> None:
        if not self.url:
            raise ValueError("QQ adapter missing required config: url")

    def _connect(self) -> Any:
        connect_func = self._connect_func
        if connect_func is None:
            try:
                from websockets.sync.client import connect as ws_connect
            except ImportError as e:
                raise RuntimeError("QQ adapter requires the optional messaging extra: uv sync --extra messaging") from e
            connect_func = ws_connect
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if headers:
            return connect_func(self.url, additional_headers=headers)
        return connect_func(self.url)

    def set_reply_target(self, message: InboundMessage) -> None:
        self._reply_target = str(message.sender_id).strip()

    def clear_reply_target(self) -> None:
        self._reply_target = ""

    def _send_frame(self, frame: dict[str, Any]) -> None:
        """Send one OneBot frame, or defer visibly while disconnected (audit #32).

        The reconnect loop owns the socket; during a reconnect window there is
        nowhere to send. Simplest honest behavior: the message is DROPPED and
        the gateway logs the deferral (DeliveryDeferred is the existing
        logged-non-delivery semantics) — no queueing, no crash, no pretending
        it was delivered. The chara can speak again once the link is back.
        """
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        with self._socket_lock:
            sock = self._socket
            if sock is None:
                raise DeliveryDeferred(
                    "QQ OneBot WebSocket is disconnected (reconnect in progress); "
                    "this message was dropped, not queued"
                )
            sock.send(payload)

    def send(self, text: str) -> None:
        target = self._reply_target or self.peer_id
        if not target:
            # A permanent "nowhere to send" condition, not a transient fault:
            # raise DeliveryDeferred (logged-non-delivery) like every other
            # adapter, so the relay drops it once instead of treating a bare
            # RuntimeError as transient and burning a pointless retry + wait.
            raise DeliveryDeferred(
                "QQ has no target yet (no config.peer_id and no prior inbound sender); "
                "this message was dropped, not queued"
            )
        user_id: int | str = int(target) if target.isdigit() else target
        self._send_frame(
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": user_id,
                    "message": text,
                },
                "echo": self._uuid_factory(),
            }
        )

    def _recv_loop(self, sock: Any, inbox: "queue.Queue[InboundMessage]") -> None:
        while not self._closed.is_set():
            try:
                raw = sock.recv(timeout=self._recv_timeout)
            except TimeoutError:
                continue
            msg = parse_onebot_event(raw)
            if msg is not None:
                inbox.put(msg)

    def run(self, inbox: "queue.Queue[InboundMessage]") -> None:
        self._validate()
        backoff = 1.0
        while not self._closed.is_set():
            try:
                _log.info("QQ OneBot connecting to %s", self.url)
                with self._connect() as sock:
                    with self._socket_lock:
                        self._socket = sock
                    _log.info("QQ OneBot connected to %s", self.url)
                    backoff = 1.0
                    self._recv_loop(sock, inbox)
            except Exception as e:
                if self._closed.is_set():
                    break
                _log.warning("QQ OneBot WebSocket dropped/failed: %s; reconnecting in %.0fs", e, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                with self._socket_lock:
                    self._socket = None

    def close(self) -> None:
        self._closed.set()
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                _log.debug("QQ OneBot socket close failed", exc_info=True)
