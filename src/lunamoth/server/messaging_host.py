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
from ..messaging.media import deliver_attachment
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
from ..protocol import SAY, Attachment, TextDelta
from .dispatch import RpcError

_log = logging.getLogger("lunamoth.server.messaging_host")

# One bounded retry for a failed adapter.send() (mirrors the standalone gateway).
_SEND_RETRY_DELAY = 3.0

# When an inbound turn can't start because another turn (the desktop app) is
# mid-flight, WAIT and retry instead of dropping the message (the -32011 collision
# that silently lost WeChat messages). ~3s total, then an honest "still busy" note.
_TURN_WAIT_DELAY = 0.3
_TURN_WAIT_ATTEMPTS = 10


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
        self._ack: str | None = None  # cached "got it" receipt (char name + lang)
        # Proactive superchat buffer: a chara's idle/self-work `speak` (an idle
        # turn the SUPERVISOR drives, never this host) is observed via the
        # dispatcher's stream tap and pushed to the gateway at turn end — so a
        # superchat reaches the user on WeChat too, not just the desktop window.
        self._proactive_say: list[str] = []
        self._proactive_atts: list[Attachment] = []

    def _ack_text(self) -> str:
        """A one-line receipt sent the moment an inbound message arrives, so the
        operator knows the gateway is live; the chara's real reply follows. Cached
        — char name + language are stable for the session."""
        if self._ack is None:
            name, zh = "", False
            try:
                snap = self._dispatcher.handle.snapshot()
                name, zh = (snap.char_name or ""), (snap.lang == "zh")
            except Exception:  # noqa: BLE001 — a missing snapshot must not block delivery
                pass
            self._ack = (f"{name}收到，思考/工作中，请稍等…" if zh
                         else f"{name} got it — thinking, one moment…")
        return self._ack

    def _busy_text(self) -> str:
        """Honest note when an inbound turn couldn't get the shared agent within
        the wait window (a long desktop turn held it) — never silent."""
        zh = self._ack is not None and "收到" in self._ack
        return "（还在忙，稍后回复你）" if zh else "(still busy — I'll get back to you shortly)"

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
                # Observe the chara's PROACTIVE turns (supervisor idle/self-work)
                # so a superchat reaches the gateway, not just the desktop window.
                with contextlib.suppress(Exception):
                    self._dispatcher.set_stream_observer(self._on_stream_event)
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
            with contextlib.suppress(Exception):
                self._dispatcher.set_stream_observer(None)
            self._proactive_say.clear()
            self._proactive_atts.clear()
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
            atts: list[Attachment] = []

            def collect(ev: Any) -> None:
                if isinstance(ev, TextDelta) and ev.channel == SAY:
                    chunks.append(ev.text)
                elif isinstance(ev, Attachment) and ev.channel == SAY:
                    atts.append(ev)  # a send_file — deliver it (or an honest note)

            # Show the incoming message in the app window first (an incoming
            # bubble), THEN stream the chara's reply — so the conversation reads
            # whole from every channel, not just the chara's half. The text
            # already has media markers ([图片]/[文件: ...]/[表情]) folded in by
            # the adapter, so an inbound photo shows the marker in the window.
            peer_text = text or "[媒体]"
            with contextlib.suppress(Exception):
                self._dispatcher.emit_peer_message(peer_text, source=adapter.name, sender=msg.sender_name or sender)
            # Immediate receipt so the operator knows the gateway is live; the
            # chara's real reply follows once its turn completes. Sent directly
            # (not via the agent) so it never enters the conversation, and its own
            # getupdates echo is dropped by the adapter's send-dedup.
            with contextlib.suppress(Exception):
                self._send(adapter, self._ack_text())
            # Route through the dispatcher so the turn ALSO streams to the app
            # window (one agent, one conversation, seen from every channel).
            # Pass attachments through to the agent's ingest path; keep the
            # legacy single-arg call when there are none so existing handles
            # (and stubs) that take only `text` still work.
            make = (
                (lambda: self._dispatcher.handle.stream_user(text, attachments=attachments))
                if attachments
                else (lambda: self._dispatcher.handle.stream_user(text))
            )
            # A concurrent desktop turn (run_stream_sync supersedes IDLE, but a
            # human 'send' raises -32011): wait for it and retry rather than
            # dropping this WeChat message. Only if it's still busy after the
            # window do we surface an honest "busy" note — never silence.
            ran = False
            for attempt in range(_TURN_WAIT_ATTEMPTS):
                try:
                    self._dispatcher.run_stream_sync("wechat", make, collect)
                    ran = True
                    break
                except RpcError as e:
                    if getattr(e, "code", None) == -32011 and not self._stop.is_set():
                        if attempt < _TURN_WAIT_ATTEMPTS - 1:
                            time.sleep(_TURN_WAIT_DELAY)
                            continue
                        break  # window exhausted → fall through to the honest busy note
                    raise
            if not ran:
                self._send(adapter, self._busy_text())
                return
            say = "".join(chunks).strip()
            if say:
                self._send(adapter, say)
            for att in atts:
                self._send_attachment(adapter, att)
        finally:
            adapter.clear_reply_target()

    def _send_attachment(self, adapter: Adapter, att: Attachment) -> None:
        """Deliver a send_file attachment via the shared media helper (real upload
        if the adapter supports it, else an honest 'file generated' note)."""
        zh = self._ack is not None and "收到" in (self._ack or "")
        deliver_attachment(adapter, att, lambda t: self._send(adapter, t), zh=zh)

    def _on_stream_event(self, kind: str, ev: Any, turn_end: bool, interrupted: bool) -> None:
        """Dispatcher stream tap. Only PROACTIVE self-work turns (kind=='idle',
        driven by the supervisor — never this host's own 'wechat' turns) are
        forwarded: buffer their say-channel output and, at turn end, push it to
        the gateway as a superchat. The host's inbound replies are handled in
        _process; desktop 'send' turns are operator-local and never pushed."""
        if kind != "idle" or not self._adapters:
            return
        if turn_end:
            if interrupted:
                with self._lock:
                    self._proactive_say.clear()
                    self._proactive_atts.clear()
                return
            self._flush_proactive()
            return
        if isinstance(ev, TextDelta) and ev.channel == SAY:
            with self._lock:
                self._proactive_say.append(ev.text)
        elif isinstance(ev, Attachment) and ev.channel == SAY:
            with self._lock:
                self._proactive_atts.append(ev)

    def _flush_proactive(self) -> None:
        """Push a completed idle turn's buffered superchat to every adapter (no
        reply target → the adapter's default/last peer). Honest: an adapter with
        no destination yet raises DeliveryDeferred, caught in _send."""
        with self._lock:
            text = "".join(self._proactive_say).strip()
            atts = list(self._proactive_atts)
            adapters = list(self._adapters)
            self._proactive_say.clear()
            self._proactive_atts.clear()
        if not text and not atts:
            return
        for adapter in adapters:
            if text:
                self._send(adapter, text)
            for att in atts:
                self._send_attachment(adapter, att)

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
