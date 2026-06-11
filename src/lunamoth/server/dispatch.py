"""Shared JSON-RPC dispatch for the remote gateway transports.

Both stdio and WebSocket adapters feed requests into this module and write the
same response/notification frames back out. The runtime surface is deliberately
only :class:`lunamoth.protocol.api.CharaHandle`; transport code never reaches
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

_log = logging.getLogger("lunamoth.server.dispatch")

FrameWriter = Callable[[dict[str, Any]], object]


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
        self._lock = threading.RLock()
        self._stream_thread: threading.Thread | None = None
        self._stream_interrupt = threading.Event()
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._attached = False
        self._closed = False
        self.should_close = False

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

    # ---- method handlers -----------------------------------------------------

    def _attach(self, params: dict[str, Any]) -> Any:
        with self._lock:
            if self._attached:
                raise RpcError(-32010, "a client is already attached")
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
        self._start_stream("send", rid, wants_response, lambda: self.handle.stream_user(text))
        return None

    def _idle(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        self._require_attached()
        if params:
            raise RpcError(-32602, "idle takes no params")
        self._start_stream("idle", rid, wants_response, self.handle.stream_idle)
        return None

    def _event(self, rid: Any, params: dict[str, Any], wants_response: bool) -> None:
        """A world event turn (e.g. the card's on_attach arrival line)."""
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
            active = self._is_streaming_locked()
            if active:
                self._stream_interrupt.set()
                self._cancel_pending_permissions_locked()
        return {"ok": True, "interrupted": active}

    def _command(self, params: dict[str, Any]) -> Any:
        self._require_attached()
        line = params.get("line")
        if not isinstance(line, str):
            raise RpcError(-32602, "command.line must be a string")
        return self.handle.command(line)

    def _snapshot(self, params: dict[str, Any]) -> Any:
        if params:
            raise RpcError(-32602, "snapshot takes no params")
        return self.handle.snapshot(fresh=True)

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

    # ---- streaming -----------------------------------------------------------

    def _start_stream(
        self,
        kind: str,
        rid: Any,
        wants_response: bool,
        make_events: Callable[[], Iterator[Any]],
    ) -> None:
        with self._lock:
            if self._is_streaming_locked():
                raise RpcError(-32011, "a stream is already in flight")
            self._stream_interrupt.clear()
            thread = threading.Thread(
                target=self._stream_worker,
                args=(kind, rid, wants_response, make_events),
                name=f"gateway-{kind}",
                daemon=True,
            )
            self._stream_thread = thread
            thread.start()

    def _stream_worker(
        self,
        kind: str,
        rid: Any,
        wants_response: bool,
        make_events: Callable[[], Iterator[Any]],
    ) -> None:
        interrupted = False
        events: Iterator[Any] | None = None
        try:
            events = make_events()
            for ev in events:
                if self._stream_interrupt.is_set():
                    interrupted = True
                    break
                if not self._emit("event", to_dict(ev)):
                    interrupted = True
                    self._stream_interrupt.set()
                    break
                if self._stream_interrupt.is_set():
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
            with self._lock:
                if threading.current_thread() is self._stream_thread:
                    self._stream_thread = None
                self._stream_interrupt.clear()

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

    def _cancel_pending_permissions_locked(self) -> None:
        for pending in self._pending_permissions.values():
            pending.granted = False
            pending.event.set()

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
