from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol import SAY, TextDelta
from ..protocol.api import CharaHandle
from .access import RefusalThrottle, sender_allowed
from .base import Adapter, DeliveryDeferred, InboundMessage
from .qq import QQAdapter
from .telegram import TelegramAdapter
from .text import split_text
from .wecom import WeComAdapter
from .weixin import WeixinAdapter
from .weixinpad import WeixinPadAdapter

_log = logging.getLogger("lunamoth.messaging.gateway")

DEFAULT_REFUSAL = (
    "Sorry, this LunaMoth messaging gateway only accepts messages from its "
    "configured contacts."
)

# One bounded retry for a failed adapter.send() before that message is dropped
# (hermes stream_consumer.py: failed sends retried once after 3 s, then degrade).
_SEND_RETRY_DELAY = 3.0

# Inbound dedup window (audit #30; hermes gateway/platforms/helpers.py
# MessageDeduplicator): WeCom retries callbacks that aren't answered in time
# and OneBot/NapCat redelivers after a reconnect — well inside 300 s.
_DEDUP_TTL = 300.0
_DEDUP_MAX = 2000


class MessageDeduplicator:
    """TTL cache over platform message ids, on a monotonic clock.

    `is_duplicate(key)` returns True when *key* was already seen within the
    TTL; otherwise it records the key and returns False. Bounded: expired
    entries are pruned when the cache overflows, and if everything is still
    fresh the oldest entries are evicted to honor `max_size`.
    """

    def __init__(self, max_size: int = _DEDUP_MAX, ttl_seconds: float = _DEDUP_TTL,
                 clock=time.monotonic) -> None:
        self._seen: dict[str, float] = {}
        self._max_size = int(max_size)
        self._ttl = float(ttl_seconds)
        self._clock = clock

    def is_duplicate(self, key: str) -> bool:
        if not key:
            return False  # no platform id: nothing safe to dedup on
        now = self._clock()
        seen_at = self._seen.get(key)
        if seen_at is not None:
            if now - seen_at < self._ttl:
                return True
            del self._seen[key]  # expired — treat as new
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            if len(self._seen) > self._max_size:
                # Everything still fresh: keep the newest to enforce the bound.
                newest = sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max_size:]
                self._seen = dict(newest)
        return False


def config_path() -> Path:
    root = os.getenv("LUNAMOTH_CONFIG_DIR")
    if root:
        return Path(root).expanduser().resolve() / "messaging.json"
    session = os.getenv("LUNAMOTH_SESSION", "")
    if session:
        return Path.home().expanduser() / ".lunamoth" / "sessions" / session / "messaging.json"
    raise RuntimeError("no active LunaMoth session; activate a session before loading messaging config")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path).expanduser().resolve() if path is not None else config_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a JSON object")
    return data


def make_adapters(config: dict[str, Any]) -> list[Adapter]:
    adapters = config.get("adapters", {})
    if not isinstance(adapters, dict):
        raise ValueError("messaging.json field 'adapters' must be an object")
    out: list[Adapter] = []
    for name, adapter_config in adapters.items():
        if not isinstance(adapter_config, dict):
            raise ValueError(f"adapter {name!r} config must be an object")
        if name == "wecom":
            out.append(WeComAdapter(adapter_config))
        elif name == "weixin":
            out.append(WeixinAdapter(adapter_config))
        elif name == "weixinpad":
            out.append(WeixinPadAdapter(adapter_config))
        elif name == "qq":
            out.append(QQAdapter(adapter_config))
        elif name == "telegram":
            out.append(TelegramAdapter(adapter_config))
        else:
            raise ValueError(f"unknown messaging adapter {name!r}")
    if not out:
        raise ValueError("messaging.json configures no adapters")
    return out


@dataclass(frozen=True)
class _Envelope:
    adapter: Adapter
    message: InboundMessage


class _AdapterSink:
    """Queue-like object handed to one adapter so we retain the source adapter."""

    def __init__(self, adapter: Adapter, out: "queue.Queue[_Envelope]") -> None:
        self._adapter = adapter
        self._out = out

    def put(self, item: InboundMessage, block: bool = True, timeout: float | None = None) -> None:
        self._out.put(_Envelope(self._adapter, item), block=block, timeout=timeout)

    def put_nowait(self, item: InboundMessage) -> None:
        self.put(item, block=False)


class MessagingGateway:
    """One-process, one-chara messaging gateway.

    The handle is the entire backend surface.  Only say-channel text crosses the
    adapter boundary; muse, thinking and tool events are intentionally dropped.
    """

    def __init__(
        self,
        handle: CharaHandle | None = None,
        adapters: list[Adapter] | None = None,
        *,
        allowed_senders: list[str] | set[str] | tuple[str, ...] = (),
        patience: float | None = None,
        refusal_text: str = DEFAULT_REFUSAL,
    ) -> None:
        self.handle = handle or CharaHandle()
        self.adapters = list(adapters or [])
        self.allowed_senders = {str(s) for s in allowed_senders}
        # None / missing → the chara's safe default (600s). NEVER a tiny default:
        # a 2s spontaneous cadence once burned a real key's daily limit.
        self.patience = max(0.0, float(600.0 if patience is None else patience))
        self.refusal_text = refusal_text
        self._inbox: "queue.Queue[_Envelope]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._errors: "queue.Queue[BaseException]" = queue.Queue()
        self._started = False
        self._attached = False
        self._refusals = RefusalThrottle()
        self._last_user_at = 0.0
        self._present = False
        self._next_idle_at = time.monotonic()
        self._dedup = MessageDeduplicator()

    @classmethod
    def from_config(cls, config: dict[str, Any], *, handle: CharaHandle | None = None, patience: float | None = None) -> "MessagingGateway":
        allowed = config.get("allowed_senders", [])
        if not isinstance(allowed, list):
            raise ValueError("messaging.json field 'allowed_senders' must be a list")
        return cls(
            handle=handle,
            adapters=make_adapters(config),
            allowed_senders=[str(x) for x in allowed],
            patience=patience,
            refusal_text=str(config.get("refusal_text") or DEFAULT_REFUSAL),
        )

    def start(self) -> None:
        if self._started:
            return
        if not self._attached:
            self.handle.attach(present=False)
            self.handle.set_present(False)
            self._attached = True
        for adapter in self.adapters:
            thread = threading.Thread(
                target=self._run_adapter,
                args=(adapter,),
                name=f"lunamoth-{adapter.name}-adapter",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        self._started = True

    def _run_adapter(self, adapter: Adapter) -> None:
        try:
            adapter.run(_AdapterSink(adapter, self._inbox))  # type: ignore[arg-type]
        except Exception as e:
            self._errors.put(e)
            _log.exception("messaging adapter %s stopped with an error", adapter.name)

    def close(self) -> None:
        for adapter in self.adapters:
            try:
                adapter.close()
            except Exception:
                _log.exception("failed to close messaging adapter %s", adapter.name)
        if self._attached:
            self.handle.set_present(False)

    def run(self, stop: threading.Event | None = None) -> None:
        self.start()
        stop = stop or threading.Event()
        try:
            while not stop.is_set():
                self.tick(timeout=0.1)
        finally:
            self.close()

    def tick(self, timeout: float = 0.1) -> bool:
        """Run one loop tick. Returns True when it processed a message or idle turn."""

        self.start()
        self._raise_adapter_error()
        timeout = max(0.0, timeout)
        wait = min(timeout, max(0.0, self._next_idle_at - time.monotonic()))
        try:
            env = self._inbox.get(timeout=wait)
        except queue.Empty:
            self._raise_adapter_error()
            return self._maybe_idle()
        self._process_inbound(env.adapter, env.message)
        self._raise_adapter_error()
        return True

    def _raise_adapter_error(self) -> None:
        try:
            raise self._errors.get_nowait()
        except queue.Empty:
            return

    def enqueue(self, adapter: Adapter, message: InboundMessage) -> None:
        """Test/embedding helper: inject a normalized message without adapter I/O."""

        self._inbox.put(_Envelope(adapter, message))

    def _allowed(self, sender_id: str) -> bool:
        return sender_allowed(sender_id, self.allowed_senders)

    def _process_inbound(self, adapter: Adapter, msg: InboundMessage) -> None:
        # Audit #30: WeCom retries unanswered callbacks, OneBot redelivers
        # after reconnect — a redelivered message must not run a second turn.
        if msg.message_id and self._dedup.is_duplicate(f"{adapter.name}:{msg.message_id}"):
            _log.info("dropped duplicate inbound %s message %s (platform redelivery)",
                      adapter.name, msg.message_id)
            return
        adapter.set_reply_target(msg)
        try:
            sender = str(msg.sender_id)
            if not self._allowed(sender):
                self._refuse_unknown_once_per_day(adapter, sender)
                _log.info("ignored unauthorized messaging sender %s (%s)", sender, msg.sender_name)
                return

            now = time.monotonic()
            self._last_user_at = now
            self._next_idle_at = now + self._cycle_pause()
            if not self._present:
                self.handle.set_present(True)
                self._present = True

            text = msg.text.strip()
            if not text:
                return
            if text.startswith("/"):
                reply = self.handle.command(text)
                if reply.text:
                    self._send(adapter, reply.text)
                return
            self._stream_to_adapter(adapter, self.handle.stream_user(text))
        finally:
            adapter.clear_reply_target()

    def _refuse_unknown_once_per_day(self, adapter: Adapter, sender_id: str) -> None:
        if self._refusals.allow(sender_id):
            self._send(adapter, self.refusal_text)

    def _quiet_seconds(self) -> int:
        return max(0, int(self.handle.snapshot().quiet))

    def _cycle_pause(self) -> float:
        return max(0.0, float(self.patience))

    def _resting(self) -> bool:
        return self.handle.snapshot().rest_until > time.time()

    def _maybe_idle(self) -> bool:
        now = time.monotonic()
        quiet = self._quiet_seconds()
        engaged = bool(self._last_user_at and now < self._last_user_at + quiet)
        if self._present and not engaged:
            self.handle.set_present(False)
            self._present = False
        if engaged or self._resting() or now < self._next_idle_at:
            return False
        text = self._collect_say_text(self.handle.stream_idle())
        if text:
            for adapter in self.adapters:
                self._send(adapter, text)
        self._next_idle_at = time.monotonic() + self._cycle_pause()
        return True

    def _stream_to_adapter(self, adapter: Adapter, events) -> None:
        text = self._collect_say_text(events, adapter=adapter)
        if text:
            self._send(adapter, text)

    def _collect_say_text(self, events, *, adapter: Adapter | None = None) -> str:
        chunks: list[str] = []
        try:
            for ev in events:
                if isinstance(ev, TextDelta) and ev.channel == SAY:
                    chunks.append(ev.text)
        except Exception as e:
            _log.exception("messaging turn failed")
            if adapter is not None:
                self._send(adapter, f"[gateway error] {e}")
            else:
                for out in self.adapters:
                    self._send(out, f"[gateway error] {e}")
            return ""
        return "".join(chunks).strip()

    def _send(self, adapter: Adapter, text: str) -> None:
        """Deliver one outbound message, containing send failures (audit #31).

        A transient socket error on SEND must never crash the gateway process
        (which would drop the whole inbox into a 60 s supervisor backoff): one
        bounded retry after _SEND_RETRY_DELAY, then THIS message is dropped
        with an ERROR log and the loop carries on. Configuration errors at
        startup (make_adapters / from_config) still crash — visible, correct.
        """
        max_len = int(getattr(adapter, "max_message_length", 0) or 0)
        parts = split_text(text, max_len) if max_len else [text]
        for part in parts:
            for attempt in (1, 2):
                try:
                    adapter.send(part)
                    break
                except DeliveryDeferred as e:
                    # The adapter consciously deferred (e.g. no reply window
                    # open) — retrying won't change that; log and move on.
                    _log.error("messaging adapter %s could not deliver: %s", adapter.name, e)
                    break
                except Exception as e:
                    if attempt == 1:
                        _log.warning(
                            "messaging adapter %s send failed (%s: %s) — one retry in %gs",
                            adapter.name, type(e).__name__, e, _SEND_RETRY_DELAY,
                        )
                        time.sleep(_SEND_RETRY_DELAY)
                        continue
                    _log.error(
                        "messaging adapter %s dropped a message after retry (%s: %s): %.80s",
                        adapter.name, type(e).__name__, e, part,
                    )
                    return  # drop the rest of THIS message; the gateway lives on
