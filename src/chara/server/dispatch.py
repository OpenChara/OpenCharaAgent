"""Shared JSON-RPC dispatch for the remote gateway transports.

Both stdio and WebSocket adapters feed requests into this module and write the
same response/notification frames back out. The runtime surface is deliberately
only :class:`chara.protocol.api.CharaHandle`; transport code never reaches
behind that contract.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from ..protocol import PROTOCOL_VERSION, to_dict
from ..protocol.api import CharaHandle

_log = logging.getLogger("chara.server.dispatch")

FrameWriter = Callable[[dict[str, Any]], object]

# How long the clarify hook blocks a turn waiting for the client's answer.
# clarify has no model-supplied deadline (unlike request_permission); this
# bounds the worker thread so a client that never replies can't hang it.
_CLARIFY_WAIT_SECONDS = 300


class RpcError(Exception):
    """An error that should be serialized as a JSON-RPC error response."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _PendingPermission:
    event: threading.Event
    granted: bool = False


@dataclass
class _PendingClarify:
    event: threading.Event
    answer: str = ""


def hello_frame() -> dict[str, Any]:
    """Initial server notification sent after a transport is ready."""

    return {
        "jsonrpc": "2.0",
        "method": "hello",
        "params": {"protocol_version": PROTOCOL_VERSION},
    }


def ok_response(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": _jsonable(result)}


def error_response(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def parse_error_response() -> dict[str, Any]:
    return error_response(None, -32700, "parse error")


def _jsonable(value: Any) -> Any:
    """Convert dataclass/tuple containers to ordinary JSON-shaped values."""

    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _normalize_request(req: Any) -> tuple[Any, str, dict[str, Any], bool] | dict[str, Any]:
    if not isinstance(req, dict):
        return error_response(None, -32600, "invalid request: expected an object")
    rid = req.get("id")
    method = req.get("method")
    if not isinstance(method, str) or not method:
        return error_response(rid, -32600, "invalid request: method must be a non-empty string")
    params = req.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return error_response(rid, -32602, "invalid params: expected an object")
    return rid, method, params, "id" in req


class JsonRpcDispatcher:
    """One-session JSON-RPC dispatcher.

    Long-running turn methods (`send` and `idle`) are executed on a worker
    thread. The caller gets no immediate response; the worker streams `event`
    notifications and writes the original request's response when the turn ends.
    This keeps the transport read loop free to accept `interrupt` and
    `permission_reply` while a model/tool turn is in flight.
    """

    def __init__(self, write: FrameWriter, handle: CharaHandle | None = None):
        self._write = write
        self.handle = handle or CharaHandle()
        self.handle.set_permission_hook(self._permission_hook)
        self.handle.set_clarify_hook(self._clarify_hook)
        self._lock = threading.RLock()
        self._stream_thread: threading.Thread | None = None
        self._stream_kind: str = ""  # kind of the in-flight stream (send|event|idle|react)
        # Interrupt flag of the CURRENT turn. Each turn installs a FRESH Event
        # when it claims the slot (never clear()s the old one): after a timed-out
        # takeover the superseded worker is still running, and clearing a shared
        # flag would un-interrupt that zombie — two generators writing the one
        # ContextBuffer. The zombie's own Event stays set until it dies.
        self._stream_interrupt = threading.Event()
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._pending_clarifies: dict[str, _PendingClarify] = {}
        self._attached = False
        self._closed = False
        self.should_close = False
        self._messaging_host: Any = None
        self._stream_observer: Callable[[str, Any, bool, bool], None] | None = None

    def set_messaging_host(self, host: Any) -> None:
        """Bind the in-process messaging host so messaging.* RPCs can drive it."""
        self._messaging_host = host

    def set_stream_observer(self, cb: Callable[[str, Any, bool, bool], None] | None) -> None:
        """Register a passive tap on EVERY streamed turn — including ones this
        dispatcher didn't initiate (the supervisor-driven idle/self-work turns).
        Called as cb(kind, event, turn_end, interrupted): once per event with
        turn_end=False, then once at the end with event=None, turn_end=True. The
        messaging host uses it to push a chara's PROACTIVE superchat (an idle-turn
        say) out to the gateway — the host's own _process never sees idle turns."""
        self._stream_observer = cb

    def _observe(self, kind: str, ev: Any = None, *, turn_end: bool = False,
                 interrupted: bool = False) -> None:
        cb = self._stream_observer
        if cb is None:
            return
        try:
            cb(kind, ev, turn_end, interrupted)
        except Exception:  # noqa: BLE001 — an observer must never break the turn
            _log.exception("stream observer failed")

    # ---- public dispatch -----------------------------------------------------

    def dispatch(self, req: Any) -> dict[str, Any] | None:
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        rid, method, params, wants_response = normalized
        try:
            if method == "attach":
                result = self._attach(params)
            elif method == "send":
                return self._send(rid, params, wants_response)
            elif method == "idle":
                return self._idle(rid, params, wants_response)
            elif method == "react":
                return self._react(rid, params, wants_response)
            elif method == "event":
                return self._event(rid, params, wants_response)
            elif method == "greet":
                result = self._greet(params)
            elif method == "interrupt":
                result = self._interrupt()
            elif method == "command":
                result = self._command(params)
            elif method == "snapshot":
                result = self._snapshot(params)
            elif method == "permission_reply":
                result = self._permission_reply(params)
            elif method == "clarify_reply":
                result = self._clarify_reply(params)
            elif method == "presence.set":
                result = self._presence_set(params)
            elif method in ("messaging.start", "messaging.stop", "messaging.status"):
                result = self._messaging(method)
            elif method == "detach":
                result = self._detach()
            else:
                raise RpcError(-32601, f"unknown method: {method}")
        except RpcError as exc:
            return error_response(rid, exc.code, exc.message) if wants_response else None
        except Exception as exc:  # noqa: BLE001 - JSON-RPC is the public error boundary
            _log.exception("handler failed method=%s", method)
            return error_response(rid, -32000, f"handler error: {exc}") if wants_response else None
        return ok_response(rid, result) if wants_response else None

    def close(self) -> None:
        """Best-effort transport teardown; never raises."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.should_close = True
            self._stream_interrupt.set()
            self._cancel_pending_permissions_locked()
            attached = self._attached
            self._attached = False
        if attached:
            try:
                self.handle.detach()
            except Exception:
                _log.exception("detach during close failed")
        try:
            self.handle.set_permission_hook(None)
        except Exception:
            _log.exception("clearing permission hook failed")
        try:
            self.handle.set_clarify_hook(None)
        except Exception:
            _log.exception("clearing clarify hook failed")

    # ---- method handlers -----------------------------------------------------

    def _attach(self, params: dict[str, Any]) -> Any:
        # The handle owns the first-move decision (first_mes on an empty
        # transcript epoch, else silent): forward every attach to it and it
        # returns the right opening + a FRESH `restored` snapshot. Re-running is
        # safe — the session is built once, the greeting commits once (transcript
        # authority). `present` is accepted for protocol/back-compat but the chara
        # is independent of whether a human is watching.
        present = bool(params.get("present", True))
        info = self.handle.attach(present=present)
        with self._lock:
            self._attached = True
        return info

    def _send(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        self._require_attached()
        text = params.get("text")
        if not isinstance(text, str):
            raise RpcError(-32602, "send.text must be a string")
        attachments = params.get("attachments")
        if attachments is not None and not isinstance(attachments, list):
            raise RpcError(-32602, "send.attachments must be a list")
        # Only thread attachments when present so legacy single-arg handles
        # (and test stubs) keep working — the multimodal path is opt-in.
        if attachments:
            make = lambda: self.handle.stream_user(text, attachments)  # noqa: E731
        else:
            make = lambda: self.handle.stream_user(text)  # noqa: E731
        self._start_stream("send", rid, wants_response, make)
        return None

    def _idle(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        self._require_attached()
        if params:
            raise RpcError(-32602, "idle takes no params")
        self._start_stream("idle", rid, wants_response, self.handle.stream_idle)
        return None

    def _react(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        """Completion-wake turn: drain finished background jobs and react to them
        (a no-op stream if nothing is pending). Mode-independent — driven by the
        supervisor when the snapshot reports `pending_notices`."""
        self._require_attached()
        if params:
            raise RpcError(-32602, "react takes no params")
        self._start_stream("react", rid, wants_response, self.handle.stream_react)
        return None

    def _event(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        """A world event turn (reserved seam for a future GM/world-event layer)."""
        self._require_attached()
        text = params.get("text")
        if not isinstance(text, str):
            raise RpcError(-32602, "event.text must be a string")
        self._start_stream("event", rid, wants_response, lambda: self.handle.stream_event(text))
        return None

    def _greet(self, params: dict[str, Any]) -> dict[str, bool]:
        """Commit a card greeting the client displayed (AttachInfo opening='greeting')."""
        self._require_attached()
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise RpcError(-32602, "greet.text must be a non-empty string")
        self.handle.record_greeting(text)
        return {"ok": True}

    def _interrupt(self) -> dict[str, bool]:
        with self._lock:
            # A command-held slot is synchronous and never checks the Event —
            # reporting interrupted:true for it would be a fabrication.
            active = self._is_streaming_locked() and self._stream_kind != "command"
            if active:
                self._stream_interrupt.set()
                self._cancel_pending_permissions_locked()
        return {"ok": True, "interrupted": active}

    def _command(self, params: dict[str, Any]) -> Any:
        self._require_attached()
        line = params.get("line")
        if not isinstance(line, str):
            raise RpcError(-32602, "command.line must be a string")
        if not self.handle.command_is_exclusive(line):
            return self.handle.command(line)
        # A context/route-mutating command (/compact, /model …) must not
        # interleave with a streaming turn: it rewrites ctx.messages / swaps
        # agent.llm under the worker thread. Refuse while a turn is in flight,
        # and claim the stream slot for the duration so no turn starts mid-run.
        with self._lock:
            if self._is_streaming_locked():
                raise RpcError(-32011, "a turn is in flight — interrupt it or wait, then rerun the command")
            self._stream_interrupt = threading.Event()  # per-claim, like _start_stream
            self._stream_thread = threading.current_thread()
            self._stream_kind = "command"
        try:
            return self.handle.command(line)
        finally:
            with self._lock:
                if threading.current_thread() is self._stream_thread:
                    self._stream_thread = None
                    self._stream_kind = ""

    def _snapshot(self, params: dict[str, Any]) -> Any:
        if params:
            raise RpcError(-32602, "snapshot takes no params")
        return self.handle.snapshot(fresh=True)


    def _presence_set(self, params: dict[str, Any]) -> dict[str, bool]:
        # Presence was retired: the chara is independent of whether a human is
        # watching. The RPC stays as a no-op purely for wire/back-compat — old
        # clients that still send it get a clean ack, never an error. The only
        # real effect is making sure the shared agent has a session when a client
        # signals presence before any explicit attach.
        present = params.get("present")
        if not isinstance(present, bool):
            raise RpcError(-32602, "presence.set.present must be a boolean")
        if present:
            with self._lock:
                attached = self._attached
            if not attached:
                self.handle.attach(present=True)
                with self._lock:
                    self._attached = True
        return {"ok": True, "present": present}

    def _permission_reply(self, params: dict[str, Any]) -> dict[str, bool]:
        pid = params.get("id")
        granted = params.get("granted")
        if not isinstance(pid, str) or not pid:
            raise RpcError(-32602, "permission_reply.id must be a non-empty string")
        if not isinstance(granted, bool):
            raise RpcError(-32602, "permission_reply.granted must be a boolean")
        with self._lock:
            pending = self._pending_permissions.get(pid)
            if pending is None:
                raise RpcError(-32004, "unknown permission request")
            pending.granted = granted
            pending.event.set()
        return {"ok": True}

    def _clarify_reply(self, params: dict[str, Any]) -> dict[str, bool]:
        pid = params.get("id")
        answer = params.get("answer", "")
        if not isinstance(pid, str) or not pid:
            raise RpcError(-32602, "clarify_reply.id must be a non-empty string")
        if not isinstance(answer, str):
            raise RpcError(-32602, "clarify_reply.answer must be a string")
        with self._lock:
            pending = self._pending_clarifies.get(pid)
            if pending is None:
                raise RpcError(-32004, "unknown clarify request")
            pending.answer = answer
            pending.event.set()
        return {"ok": True}

    def _detach(self) -> dict[str, bool]:
        with self._lock:
            was_attached = self._attached
            self._attached = False
            self.should_close = True
            self._stream_interrupt.set()
            self._cancel_pending_permissions_locked()
        if was_attached:
            self.handle.detach()
        return {"ok": True}

    # ---- messaging host ------------------------------------------------------

    def ensure_attached(self) -> None:
        """Make sure the shared agent has a session (background presence).

        The messaging host shares this dispatcher's handle; under the
        supervisor the child is attached in the background, but a host started
        before any client must still have a session to run a turn on."""
        with self._lock:
            if self._attached:
                return
        self.handle.attach(present=False)
        with self._lock:
            self._attached = True

    def emit_peer_message(self, text: str, source: str = "", sender: str = "") -> None:
        """Tell an attached client a message arrived from another channel (e.g.
        WeChat) so it shows in the window as an incoming bubble. A messaging
        turn streams the chara's REPLY as events, but the inbound text itself is
        never an event — without this the app saw the reply but not the message
        that prompted it. A transport notification (like permission_ask), not a
        stream Event, so clients that don't know it simply ignore the frame."""
        self._emit("peer_message", {"text": str(text), "source": str(source), "sender": str(sender)})

    def _messaging(self, method: str) -> dict[str, Any]:
        host = self._messaging_host
        if host is None:
            return {"state": "stopped", "platform": "", "detail": "no messaging host"}
        if method == "messaging.start":
            return host.start()
        if method == "messaging.stop":
            return host.stop()
        return host.status()

    # ---- streaming -----------------------------------------------------------

    def run_stream_sync(
        self,
        kind: str,
        make_events: Callable[[], Iterator[Any]],
        on_event: Callable[[Any], None] | None = None,
    ) -> bool:
        """Run a turn on the shared handle synchronously, on the CALLING thread.

        Each event is emitted on the transport (so an attached client sees the
        turn live) AND passed to `on_event` (the messaging host collects the
        say-channel reply). Supersedes an in-flight idle turn like a human send
        does; raises -32011 if a human/messaging turn is already active. Returns
        False if the turn was interrupted. This is the seam that lets a WeChat
        message and the desktop app share ONE agent and ONE conversation."""
        superseding: threading.Thread | None = None
        with self._lock:
            if self._closed:
                raise RpcError(-32002, "session is closing")
            if self._is_streaming_locked():
                if self._stream_kind in ("idle", "react"):
                    self._stream_interrupt.set()
                    superseding = self._stream_thread
                else:
                    raise RpcError(-32011, "a stream is already in flight")
        if superseding is not None and superseding is not threading.current_thread():
            superseding.join(timeout=10.0)
        with self._lock:
            # TOCTOU guard: we dropped the lock to join, so another superseder
            # (the transport thread vs. this messaging-relay thread) may have
            # claimed the slot meanwhile. Claim only if the slot is free, or if
            # the idle turn we interrupted is still the holder (join timed out —
            # we force the takeover). Anyone ELSE holding it is a real two-turn
            # collision; refuse rather than run two turns on one shared agent.
            if self._closed:
                raise RpcError(-32002, "session is closing")
            if (self._is_streaming_locked()
                    and self._stream_thread is not superseding
                    and self._stream_thread is not threading.current_thread()):
                raise RpcError(-32011, "a stream is already in flight")
            interrupt = self._stream_interrupt = threading.Event()
            self._stream_thread = threading.current_thread()
            self._stream_kind = kind
        interrupted = False
        events: Iterator[Any] | None = None
        try:
            events = make_events()
            for ev in events:
                if interrupt.is_set():
                    interrupted = True
                    break
                if not self._emit("event", to_dict(ev)):
                    interrupted = True
                    interrupt.set()
                    break
                self._observe(kind, ev)
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:
                        _log.exception("messaging on_event failed")
            if interrupted and hasattr(events, "close"):
                try:
                    events.close()
                except Exception:
                    _log.exception("closing interrupted %s stream failed", kind)
        finally:
            self._emit("turn_end", {"kind": kind, "interrupted": interrupted})
            self._observe(kind, turn_end=True, interrupted=interrupted)
            with self._lock:
                # Release the slot only if this turn still owns it; never touch
                # the interrupt flag — it is per-turn, and a superseded zombie
                # clearing the CURRENT turn's flag would swallow an interrupt
                # aimed at that new turn.
                if threading.current_thread() is self._stream_thread:
                    self._stream_thread = None
                    self._stream_kind = ""
        return not interrupted

    def _start_stream(
        self,
        kind: str,
        rid: Any,
        wants_response: bool,
        make_events: Callable[[], Iterator[Any]],
    ) -> None:
        superseding: threading.Thread | None = None
        with self._lock:
            if self._is_streaming_locked():
                # A human turn (send/event) supersedes the chara's own background
                # work — an idle self-work turn OR a completion-wake react turn:
                # stop it and take over, rather than failing the operator with
                # "a stream is already in flight". A react never supersedes (if a
                # turn is in flight it raises and the supervisor skips — the running
                # turn drains the notice anyway). Two human turns at once still
                # collide (that is a real client bug).
                if kind in ("send", "event") and self._stream_kind in ("idle", "react"):
                    self._stream_interrupt.set()
                    superseding = self._stream_thread
                else:
                    raise RpcError(-32011, "a stream is already in flight")
        if superseding is not None:
            superseding.join(timeout=10.0)  # let the idle turn wind down + clear
        with self._lock:
            # TOCTOU guard (see run_stream_sync): re-check after the unlocked join.
            # Claim only if the slot is free or still held by the idle turn we
            # interrupted (timed-out takeover); otherwise another turn won the slot.
            if (self._is_streaming_locked()
                    and self._stream_thread is not superseding):
                raise RpcError(-32011, "a stream is already in flight")
            interrupt = self._stream_interrupt = threading.Event()
            thread = threading.Thread(
                target=self._stream_worker,
                args=(kind, rid, wants_response, make_events, interrupt),
                name=f"gateway-{kind}",
                daemon=True,
            )
            self._stream_thread = thread
            self._stream_kind = kind
            thread.start()

    def _stream_worker(
        self,
        kind: str,
        rid: Any,
        wants_response: bool,
        make_events: Callable[[], Iterator[Any]],
        interrupt: threading.Event,
    ) -> None:
        interrupted = False
        events: Iterator[Any] | None = None
        try:
            events = make_events()
            for ev in events:
                if interrupt.is_set():
                    interrupted = True
                    break
                if not self._emit("event", to_dict(ev)):
                    interrupted = True
                    interrupt.set()
                    break
                self._observe(kind, ev)
                if interrupt.is_set():
                    interrupted = True
                    break
            if interrupted and hasattr(events, "close"):
                try:
                    events.close()  # trigger backend generator interrupt bookkeeping
                except Exception:
                    _log.exception("closing interrupted %s stream failed", kind)
            if wants_response:
                self._write_frame(ok_response(rid, {"ok": True, "interrupted": interrupted}))
        except Exception as exc:  # noqa: BLE001 - stream failures are JSON-RPC errors
            _log.exception("%s stream failed", kind)
            if wants_response:
                self._write_frame(error_response(rid, -32000, f"{kind} failed: {exc}"))
        finally:
            # Tell observers the turn ended. Crucial for turns the client didn't
            # initiate (supervisor idle/self-work, a WeChat-driven turn): the
            # window turns its "generating…" indicator ON from the event stream
            # but has no completion to turn it OFF without this signal.
            self._emit("turn_end", {"kind": kind, "interrupted": interrupted})
            self._observe(kind, turn_end=True, interrupted=interrupted)
            with self._lock:
                # Release the slot only if this turn still owns it; the interrupt
                # flag is per-turn and must never be cleared here (a zombie would
                # swallow an interrupt aimed at the turn that superseded it).
                if threading.current_thread() is self._stream_thread:
                    self._stream_thread = None
                    self._stream_kind = ""

    def _is_streaming_locked(self) -> bool:
        return self._stream_thread is not None and self._stream_thread.is_alive()

    def _require_attached(self) -> None:
        with self._lock:
            if not self._attached:
                raise RpcError(-32001, "no client is attached")

    # ---- permission hook -----------------------------------------------------

    def _permission_hook(self, kind: str, reason: str, detail: str, wait_seconds: int) -> bool:
        with self._lock:
            if self._closed:
                return False
            pid = uuid.uuid4().hex
            pending = _PendingPermission(threading.Event())
            self._pending_permissions[pid] = pending
        wait = max(0, int(wait_seconds or 0))
        sent = self._emit(
            "permission_ask",
            {
                "id": pid,
                "kind": str(kind or ""),
                "reason": str(reason or ""),
                "detail": str(detail or ""),
                "wait_seconds": wait,
            },
        )
        if not sent:
            with self._lock:
                self._pending_permissions.pop(pid, None)
            return False
        answered = pending.event.wait(wait)
        with self._lock:
            self._pending_permissions.pop(pid, None)
        return bool(answered and pending.granted)

    def _clarify_hook(self, question: str, choices: "list | None") -> str:
        """The web/remote clarify round-trip, structurally identical to the
        permission hook: emit a `clarify_ask` notification carrying the question
        and choices, block the stream thread on an Event, return the answer the
        client sends via `clarify_reply`. clarify is presence-gated before this
        runs; a bounded wait keeps the worker from hanging forever if the client
        never answers (returns "" → the tool reports a clean no-answer error)."""
        with self._lock:
            if self._closed:
                return ""
            pid = uuid.uuid4().hex
            pending = _PendingClarify(threading.Event())
            self._pending_clarifies[pid] = pending
        opts = [str(c) for c in (choices or []) if str(c).strip()]
        sent = self._emit(
            "clarify_ask",
            {
                "id": pid,
                "question": str(question or ""),
                "choices": opts,
                "wait_seconds": _CLARIFY_WAIT_SECONDS,
            },
        )
        if not sent:
            with self._lock:
                self._pending_clarifies.pop(pid, None)
            return ""
        answered = pending.event.wait(_CLARIFY_WAIT_SECONDS)
        with self._lock:
            self._pending_clarifies.pop(pid, None)
        return pending.answer if answered else ""

    def _cancel_pending_permissions_locked(self) -> None:
        for pending in self._pending_permissions.values():
            pending.granted = False
            pending.event.set()
        for clarify in self._pending_clarifies.values():
            clarify.answer = ""
            clarify.event.set()

    # ---- outbound frames -----------------------------------------------------

    def _emit(self, method: str, params: dict[str, Any]) -> bool:
        return self._write_frame({"jsonrpc": "2.0", "method": method, "params": _jsonable(params)})

    def _write_frame(self, frame: dict[str, Any]) -> bool:
        try:
            result = self._write(frame)
            return result is not False
        except Exception:
            # Event delivery must never throw back into the agent/stream thread.
            _log.exception("gateway frame write failed")
            return False
