"""In-process messaging host for the serve child.

Unlike the standalone :class:`~lunamoth.messaging.gateway.MessagingGateway`
(its OWN agent + idle loop, a separate process), this host shares the serve
child's ONE agent. A WeChat message runs a turn on the SAME handle the desktop
app drives, so:

* the exchange streams into the chat window live (the turn's events are emitted
  on the child's stdio transport, exactly like an app-initiated turn), and
* the say-channel reply is collected and sent back to the messaging platform.

The supervisor owns idle / self-work (autonomy mode); this host never drives
idle — it only relays inbound ↔ say. One chara, reachable from every channel.
"""
from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..messaging.access import RefusalThrottle, sender_allowed
from ..messaging.base import Adapter, DeliveryDeferred, InboundMessage
from ..messaging.gateway import (
    DEFAULT_REFUSAL,
    MessageDeduplicator,
    _AdapterSink,
    _Envelope,
    load_config,
    make_adapters,
)
from ..messaging.filters import is_silence_narration
from ..messaging.text import split_text
from ..protocol import SAY, TextDelta

_log = logging.getLogger("lunamoth.server.messaging_host")

# One bounded retry for a failed adapter.send() (mirrors the standalone gateway).
_SEND_RETRY_DELAY = 3.0


class MessagingHost:
    """Adapters + an inbound relay, bound to a serve child's dispatcher."""

    def __init__(self, dispatcher: Any, config_path: str | Path) -> None:
        self._dispatcher = dispatcher
        self._config_path = Path(config_path)
        self._lock = threading.RLock()
        self._adapters: list[Adapter] = []
        self._allowed: set[str] = set()
        self._refusal = DEFAULT_REFUSAL
        self._inbox: "queue.Queue[_Envelope]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._relay: threading.Thread | None = None
        self._stop = threading.Event()
        self._dedup = MessageDeduplicator()
        self._refusals = RefusalThrottle()
        self._platform = ""
        self._state = "stopped"
        self._detail = ""

    # ---- control ------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "running":
                return self.status()
            try:
                cfg = load_config(self._config_path)
            except (OSError, ValueError):
                # No config / unreadable → nothing to run; not an error.
                self._state, self._detail = "stopped", ""
                return self.status()
            if not cfg.get("enabled"):
                self._state, self._detail = "stopped", ""
                return self.status()
            # make_adapters raises on a bad config — surfaced, never faked.
            adapters = make_adapters(cfg)
            allowed = cfg.get("allowed_senders", [])
            self._allowed = {str(x) for x in allowed} if isinstance(allowed, list) else set()
            self._refusal = str(cfg.get("refusal_text") or DEFAULT_REFUSAL)
            self._adapters = adapters
            self._platform = ",".join(sorted(a.name for a in adapters))
            self._stop.clear()
            self._inbox = queue.Queue()
            # Only start adapters that are ready: one still needing an interactive
            # login (a WeChat QR scan) is left PENDING — never spun up — so the
            # host never opens a second QR session competing with the app's QR
            # flow on the same account (the bug that made the QR die instantly).
            ready = [a for a in adapters if not a.needs_login()]
            pending = [a for a in adapters if a.needs_login()]
            self._threads = []
            if ready:
                # The shared agent needs a session; the supervisor attaches the
                # child in the background, but be defensive when started direct.
                self._dispatcher.ensure_attached()
                for adapter in ready:
                    th = threading.Thread(
                        target=self._run_adapter, args=(adapter,),
                        name=f"lunamoth-{adapter.name}-adapter", daemon=True,
                    )
                    th.start()
                    self._threads.append(th)
                self._relay = threading.Thread(
                    target=self._relay_loop, name="lunamoth-messaging-relay", daemon=True,
                )
                self._relay.start()
            if ready:
                self._state = "running"
                self._detail = (
                    "" if not pending
                    else f"awaiting login: {','.join(a.name for a in pending)}"
                )
            elif pending:
                # Honest status: enabled & configured, but waiting for the QR.
                self._state = "needs_login"
                self._detail = ",".join(a.name for a in pending)
            else:
                self._state = "stopped"
                self._detail = ""
            _log.info("messaging host start: state=%s platform=%s", self._state, self._platform)
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            for adapter in self._adapters:
                try:
                    adapter.close()
                except Exception:
                    _log.exception("closing adapter %s failed", adapter.name)
            self._adapters = []
            self._threads = []
            self._relay = None
            self._state, self._detail = "stopped", ""
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "platform": self._platform, "detail": self._detail}

    # ---- relay --------------------------------------------------------------

    def _run_adapter(self, adapter: Adapter) -> None:
        try:
            adapter.run(_AdapterSink(adapter, self._inbox))  # type: ignore[arg-type]
        except Exception:
            if not self._stop.is_set():
                _log.exception("messaging adapter %s stopped with an error", adapter.name)
                with self._lock:
                    self._detail = f"adapter {adapter.name} stopped"

    def _relay_loop(self) -> None:
        while not self._stop.is_set():
            try:
                env = self._inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(env.adapter, env.message)
            except Exception:
                _log.exception("messaging relay turn failed")

    def _process(self, adapter: Adapter, msg: InboundMessage) -> None:
        # A platform redelivery (OneBot reconnect, or any callback retry) must
        # not run a second turn on the shared agent.
        if msg.message_id and self._dedup.is_duplicate(f"{adapter.name}:{msg.message_id}"):
            return
        adapter.set_reply_target(msg)
        try:
            sender = str(msg.sender_id)
            if not sender_allowed(sender, self._allowed):
                if self._refusals.allow(sender):
                    self._send(adapter, self._refusal)
                _log.info("ignored unauthorized messaging sender %s (%s)", sender, msg.sender_name)
                return
            text = (msg.text or "").strip()
            attachments = list(msg.attachments)
            # A media-only message (a photo/file/sticker with no caption) has no
            # text but carries attachments and/or folded-in media markers —
            # don't drop it. Only a genuinely empty message (no text, no media)
            # is ignored.
            if not text and not attachments:
                return
            if text.startswith("/"):
                reply = self._dispatcher.handle.command(text)
                if reply.text:
                    self._send(adapter, reply.text)
                return
            chunks: list[str] = []

            def collect(ev: Any) -> None:
                if isinstance(ev, TextDelta) and ev.channel == SAY:
                    chunks.append(ev.text)

            # Show the incoming message in the app window first (an incoming
            # bubble), THEN stream the chara's reply — so the conversation reads
            # whole from every channel, not just the chara's half. The text
            # already has media markers ([图片]/[文件: ...]/[表情]) folded in by
            # the adapter, so an inbound photo shows the marker in the window.
            peer_text = text or "[媒体]"
            with contextlib.suppress(Exception):
                self._dispatcher.emit_peer_message(peer_text, source=adapter.name, sender=msg.sender_name or sender)
            # Route through the dispatcher so the turn ALSO streams to the app
            # window (one agent, one conversation, seen from every channel).
            # Pass attachments through to the agent's ingest path; keep the
            # legacy single-arg call when there are none so existing handles
            # (and stubs) that take only `text` still work.
            self._dispatcher.run_stream_sync(
                "wechat",
                (
                    (lambda: self._dispatcher.handle.stream_user(text, attachments=attachments))
                    if attachments
                    else (lambda: self._dispatcher.handle.stream_user(text))
                ),
                collect,
            )
            say = "".join(chunks).strip()
            if say:
                self._send(adapter, say)
        finally:
            adapter.clear_reply_target()

    def _send(self, adapter: Adapter, text: str) -> None:
        """Deliver one outbound message; a transient send error never crashes
        the relay (one retry after _SEND_RETRY_DELAY, then drop this message)."""
        # Anti-loop (audit #33): drop silence-narration tokens before delivery,
        # same guard the standalone gateway applies at its send chokepoint.
        if is_silence_narration(text):
            _log.info("dropped silence-narration token before delivery: %r", text[:40])
            return
        max_len = int(getattr(adapter, "max_message_length", 0) or 0)
        parts = split_text(text, max_len) if max_len else [text]
        for part in parts:
            for attempt in (1, 2):
                try:
                    adapter.send(part)
                    break
                except DeliveryDeferred as e:
                    _log.error("messaging adapter %s could not deliver: %s", adapter.name, e)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 1:
                        _log.warning(
                            "messaging adapter %s send failed (%s: %s) — one retry in %gs",
                            adapter.name, type(e).__name__, e, _SEND_RETRY_DELAY,
                        )
                        time.sleep(_SEND_RETRY_DELAY)
                        continue
                    _log.error("dropping outbound %s message after retry", adapter.name)
