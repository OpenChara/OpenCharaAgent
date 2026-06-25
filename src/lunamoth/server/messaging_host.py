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

from ..messaging.access import RefusalThrottle, sender_allowed, warn_if_open_allowlist
from ..messaging.base import Adapter, DeliveryDeferred, InboundMessage
from ..messaging.media import deliver_image_url, deliver_media, extract_outbound, missing_media_note
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
        # Per-adapter threads, keyed by platform name, so one platform can be
        # added/removed without touching the others (no blip on a sibling toggle).
        self._threads_by_name: dict[str, threading.Thread] = {}
        # Names being torn down on PURPOSE — so _run_adapter doesn't mistake an
        # intentional single-platform stop for a crash and log a false error.
        self._intentional: set[str] = set()
        self._relay: threading.Thread | None = None
        self._stop = threading.Event()
        self._dedup = MessageDeduplicator()
        self._refusals = RefusalThrottle()
        self._platform = ""
        self._state = "stopped"
        self._detail = ""
        # Per-platform live state for the gateway overview (one row per platform):
        # ready→running, pending→needs_login. Rebuilt on every start/reconcile.
        self._ready_names: list[str] = []
        self._pending_names: list[str] = []
        self._ack: str | None = None  # cached "got it" receipt (char name + lang)
        # Proactive superchat buffer: a chara's idle/self-work `speak` (an idle
        # turn the SUPERVISOR drives, never this host) is observed via the
        # dispatcher's stream tap and pushed to the gateway at turn end — so a
        # superchat reaches the user on WeChat too, not just the desktop window.
        self._proactive_say: list[str] = []

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
            # Reconcile to the config WITHOUT a full teardown: a per-platform toggle
            # adds/removes only the platform that changed, so the platforms that stay
            # enabled keep their live connection (no reconnect blip on a sibling).
            try:
                cfg = load_config(self._config_path)
            except (OSError, ValueError):
                # No config / unreadable → nothing to run; not an error.
                self._teardown()
                self._state, self._detail = "stopped", ""
                return self.status()
            if not cfg.get("enabled"):
                # Host turned off entirely → stop every platform.
                self._teardown()
                self._state, self._detail = "stopped", ""
                return self.status()
            # make_adapters builds only ENABLED platforms; [] = all disabled →
            # stopped (not an error). It raises only on a malformed config.
            adapters = make_adapters(cfg)
            if not adapters:
                self._teardown()
                self._state, self._detail = "stopped", ""
                return self.status()
            allowed = cfg.get("allowed_senders", [])
            self._allowed = {str(x) for x in allowed} if isinstance(allowed, list) else set()
            self._refusal = str(cfg.get("refusal_text") or DEFAULT_REFUSAL)
            self._reconcile(adapters)
            _owner = next((a.owner_id() for a in self._adapters if a.owner_id()), "")
            warn_if_open_allowlist(self._allowed, channel=self._platform or "messaging", owner_id=_owner)
            _log.info("messaging host start: state=%s platform=%s", self._state, self._platform)
            return self.status()

    def _reconcile(self, desired: "list[Adapter]") -> None:
        """Bring the live adapter set to `desired` by DIFF, not rebuild: stop the
        platforms that left, start the ones that arrived, and leave the unchanged
        ones running untouched. A platform still needing an interactive login (a
        WeChat QR scan) is left PENDING — never spun up — exactly as before."""
        desired_by = {a.name: a for a in desired}
        live_names = {a.name for a in self._adapters}
        # 1. stop platforms no longer enabled — independently, others keep running.
        for name in live_names - set(desired_by):
            self._stop_one(name)
        self._pending_names = [n for n in self._pending_names if n in desired_by]
        # 2. add the newly-enabled (or promote a pending one that just logged in).
        for name, adapter in desired_by.items():
            if name in live_names:
                continue  # already running → untouched (the no-blip path)
            if name in self._pending_names:
                if adapter.needs_login():
                    continue  # still waiting on its QR
                self._pending_names.remove(name)  # logged in since → promote
            if adapter.needs_login():
                if name not in self._pending_names:
                    self._pending_names.append(name)
            else:
                self._start_one(adapter)
        self._recompute_state()

    def _ensure_relay(self) -> None:
        """Start the shared relay loop + proactive observer once; subsequent
        adapters reuse it (so adding a platform doesn't reset the inbox)."""
        if self._relay is not None and self._relay.is_alive():
            return
        self._stop.clear()
        self._inbox = queue.Queue()
        # The shared agent needs a session; the supervisor attaches the child in
        # the background, but be defensive when started direct.
        self._dispatcher.ensure_attached()
        self._relay = threading.Thread(
            target=self._relay_loop, name="lunamoth-messaging-relay", daemon=True,
        )
        self._relay.start()
        # Observe the chara's PROACTIVE turns (supervisor idle/self-work) so a
        # superchat reaches the gateway, not just the desktop window.
        with contextlib.suppress(Exception):
            self._dispatcher.set_stream_observer(self._on_stream_event)

    def _start_one(self, adapter: "Adapter") -> None:
        self._ensure_relay()
        th = threading.Thread(
            target=self._run_adapter, args=(adapter,),
            name=f"lunamoth-{adapter.name}-adapter", daemon=True,
        )
        th.start()
        self._adapters.append(adapter)
        self._threads_by_name[adapter.name] = th
        if adapter.name not in self._ready_names:
            self._ready_names.append(adapter.name)

    def _stop_one(self, name: str) -> None:
        # Mark intentional FIRST so the adapter thread's exit isn't logged as a
        # crash (and it skips the lock it would otherwise take — no deadlock with
        # the join below).
        self._intentional.add(name)
        adapter = next((a for a in self._adapters if a.name == name), None)
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                _log.exception("closing adapter %s failed", name)
        self._adapters = [a for a in self._adapters if a.name != name]
        self._ready_names = [n for n in self._ready_names if n != name]
        th = self._threads_by_name.pop(name, None)
        if th is not None:
            th.join(timeout=2.0)
        self._intentional.discard(name)
        if not self._adapters:
            self._stop_relay()  # last platform out → stop the shared relay

    def _stop_relay(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self._dispatcher.set_stream_observer(None)
        self._proactive_say.clear()
        self._relay = None

    def _recompute_state(self) -> None:
        self._platform = ",".join(sorted(
            [a.name for a in self._adapters] + list(self._pending_names)))
        if self._adapters:
            self._state = "running"
            self._detail = ("" if not self._pending_names
                            else f"awaiting login: {','.join(sorted(self._pending_names))}")
        elif self._pending_names:
            self._state = "needs_login"
            self._detail = ",".join(sorted(self._pending_names))
        else:
            self._state, self._detail = "stopped", ""

    def _teardown(self) -> None:
        """Full stop: tear down every adapter/thread (used by stop() and when the
        host is turned off entirely). Holds the lock via its callers."""
        self._stop.set()
        with contextlib.suppress(Exception):
            self._dispatcher.set_stream_observer(None)
        self._proactive_say.clear()
        for adapter in self._adapters:
            try:
                adapter.close()
            except Exception:
                _log.exception("closing adapter %s failed", adapter.name)
        self._adapters = []
        self._threads_by_name = {}
        self._intentional = set()
        self._relay = None
        self._ready_names = []
        self._pending_names = []

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._teardown()
            self._state, self._detail = "stopped", ""
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            # platforms = one entry per LIVE platform (running or waiting on a QR);
            # the child (GatewayChild) merges these with the configured-but-disabled
            # platforms so the overview can show one row per (chara, platform).
            platforms = (
                [{"platform": n, "state": "running", "detail": ""} for n in self._ready_names]
                + [{"platform": n, "state": "needs_login", "detail": ""} for n in self._pending_names]
            )
            return {"state": self._state, "platform": self._platform,
                    "detail": self._detail, "platforms": platforms}

    # ---- relay --------------------------------------------------------------

    def _run_adapter(self, adapter: Adapter) -> None:
        try:
            adapter.run(_AdapterSink(adapter, self._inbox))  # type: ignore[arg-type]
        except Exception:
            # A crash, NOT a clean shutdown: skip when the whole host is stopping
            # (self._stop) or this one platform is being stopped on purpose
            # (self._intentional) — otherwise a deliberate toggle would log a
            # false error and a lock-join would deadlock.
            if not self._stop.is_set() and adapter.name not in self._intentional:
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
            if not sender_allowed(sender, self._allowed, owner_id=adapter.owner_id()):
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
            speaks: list[str] = []  # the superchat-marked say — also pushed to the OTHER gateways

            def collect(ev: Any) -> None:
                if isinstance(ev, TextDelta) and ev.channel == SAY:
                    chunks.append(ev.text)
                    if getattr(ev, "superchat", False):
                        speaks.append(ev.text)

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
            self._emit_reply(adapter, "".join(chunks))
            # A speak made DURING this inbound turn already reached the sender via
            # the reply above (chunks include it); also push it to the OTHER
            # gateways so a deliberate speak lands on every platform. The source is
            # skipped, so nobody gets the same speak twice.
            speak_text = "".join(speaks).strip()
            if speak_text:
                with self._lock:
                    others = [a for a in self._adapters if a is not adapter]
                for other in others:
                    self._emit_reply(other, speak_text)
        finally:
            adapter.clear_reply_target()

    def _zh(self) -> bool:
        """Best-effort: is this chara speaking Chinese? (drives the honest notes)."""
        return self._ack is not None and "收到" in (self._ack or "")

    def _emit_reply(self, adapter: Adapter, raw_text: str) -> None:
        """Send one reply to *adapter* the hermes way: extract file markers from the
        COMPLETE text, send the cleaned words, then upload each resolved file (or an
        honest note for one that can't be sent / can't be found). Never silently
        drops a promised file."""
        resolve = (self._dispatcher.handle.resolve_media if self._dispatcher
                   else (lambda _rel: None))
        cleaned, files, image_urls, missing = extract_outbound(raw_text, resolve)
        zh = self._zh()
        say = cleaned.strip()
        if say:
            self._send(adapter, say)
        for path in files:
            deliver_media(adapter, path, lambda t: self._send(adapter, t), zh=zh)
        for url in image_urls:
            deliver_image_url(adapter, url, lambda t: self._send(adapter, t), zh=zh)
        for rel in missing:
            self._send(adapter, missing_media_note(rel, zh))

    def _on_stream_event(self, kind: str, ev: Any, turn_end: bool, interrupted: bool) -> None:
        """Dispatcher stream tap. Forward the chara's SPEAK (the superchat-marked
        say from the speak tool) to EVERY gateway — for autonomous self-work
        (kind=='idle'), world-event (kind=='event'), AND desktop (kind=='send')
        turns alike, so a deliberate speak always reaches the gateways no matter
        what triggered it. ONLY the speak is forwarded, never ordinary reply prose,
        so a desktop conversation is not echoed out to the gateways. Inbound
        platform turns (kind=='wechat') are handled in _process (the reply goes to
        the source, the speak to the OTHER gateways), so they're excluded here to
        avoid sending the same speak twice."""
        if kind not in ("idle", "send", "event") or not self._adapters:
            return
        if turn_end:
            if interrupted:
                with self._lock:
                    self._proactive_say.clear()
                return
            self._flush_proactive()
            return
        if isinstance(ev, TextDelta) and ev.channel == SAY and getattr(ev, "superchat", False):
            with self._lock:
                self._proactive_say.append(ev.text)

    def _flush_proactive(self) -> None:
        """Push a completed idle/send/event turn's buffered SPEAK to every adapter
        (no reply target → the adapter's default/last peer). Honest: an adapter
        with no destination yet raises DeliveryDeferred, caught in _send. File
        markers in the buffered text are extracted and delivered like a reply."""
        with self._lock:
            raw = "".join(self._proactive_say)
            adapters = list(self._adapters)
            self._proactive_say.clear()
        if not raw.strip():
            return
        for adapter in adapters:
            self._emit_reply(adapter, raw)

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
                        # Interruptible wait: a pending retry must not delay a
                        # clean shutdown (stop() sets self._stop). Returns early
                        # if stop fires — we then make the second attempt at once.
                        self._stop.wait(_SEND_RETRY_DELAY)
                        continue
                    _log.error("dropping outbound %s message after retry", adapter.name)
