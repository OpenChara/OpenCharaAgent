"""WebSocket transport for the JSON-RPC gateway."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..protocol import PROTOCOL_VERSION
from .dispatch import (
    JsonRpcDispatcher,
    error_response,
    hello_frame,
    ok_response,
    parse_error_response,
)

_log = logging.getLogger("chara.server.ws")

_AUTH_TIMEOUT_SECONDS = 30.0
_WRITE_TIMEOUT_SECONDS = 10.0

# Slow-client backpressure (#28/#29). Event frames are produced on the agent
# thread (the stream worker) and must NEVER block it while a wedged browser
# stops reading: a single 10 s-per-frame stall on the old synchronous path
# could slow a whole streaming turn to a crawl. Instead the agent thread hands
# frames to a bounded buffer and a per-connection async drain task does the
# blocking `ws.send`. When the buffer overflows we evict the oldest event
# frame (the chara's words are append-only; an old delta is the cheapest thing
# to drop) and count a strike; after enough sustained overflow the client is
# declared wedged and the sink closes so the turn stops feeding a dead socket.
_SINK_BUFFER_MAX = 512
_SINK_OVERFLOW_STRIKES = _SINK_BUFFER_MAX * 4  # ~2048 dropped frames ⇒ give up


def query_token(path: str | None) -> str:
    """Extract a token query parameter from a WebSocket request path."""

    if not path:
        return ""
    qs = parse_qs(urlsplit(path).query)
    vals = qs.get("token") or qs.get("auth") or []
    return str(vals[0]) if vals else ""


def query_auth_ok(path: str | None, expected_token: str) -> bool:
    token = query_token(path)
    return bool(token) and hmac.compare_digest(token, expected_token)


def auth_message_ok(raw: str, expected_token: str) -> tuple[bool, dict[str, Any] | None]:
    """Validate the first auth frame.

    Supports the JSON-RPC form
    `{"jsonrpc":"2.0","id":1,"method":"auth","params":{"token":"..."}}`
    and a small pre-protocol convenience form `auth TOKEN`.
    """

    line = raw.strip()
    if line.startswith("auth "):
        token = line[5:].strip()
        return (True, None) if hmac.compare_digest(token, expected_token) else (
            False,
            error_response(None, -32021, "authentication failed"),
        )
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return False, parse_error_response()
    rid = data.get("id") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("method") != "auth":
        return False, error_response(rid, -32020, "authentication required")
    params = data.get("params", {})
    if params is None:
        params = {}
    token = ""
    if isinstance(params, dict):
        token = str(params.get("token") or "")
    if not token:
        token = str(data.get("token") or "")
    if not token:
        return False, error_response(rid, -32602, "auth token is required")
    if not hmac.compare_digest(token, expected_token):
        return False, error_response(rid, -32021, "authentication failed")
    return True, ok_response(rid, {"ok": True, "protocol_version": PROTOCOL_VERSION}) if "id" in data else None


class _WSSink:
    """Non-blocking sink between the agent thread and a WebSocket client.

    ``write`` is the cross-thread, agent-side path: it enqueues a frame into a
    bounded buffer and returns immediately — it NEVER blocks on a stalled
    browser. A per-connection async drain task (:meth:`start_drain`) owns the
    actual ``ws.send`` and applies the per-frame write timeout. ``write_async``
    is the on-loop path for the read loop's own responses (hello, parse errors,
    RPC results), which are awaited directly and unbuffered.
    """

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop):
        self._ws = ws
        self._loop = loop
        self._closed = False
        self._buffer: deque[dict[str, Any]] = deque()
        self._wake = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        self._dropped = 0  # cumulative evictions (slow-client telemetry)
        self._overflow_strikes = 0  # consecutive-overflow counter → wedged

    # ---- on-loop, awaited path (responses) -----------------------------------

    async def write_async(self, frame: dict[str, Any]) -> bool:
        if self._closed:
            return False
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
            return True
        except Exception:
            self._closed = True
            _log.exception("websocket send failed")
            return False

    # ---- cross-thread, non-blocking path (event stream) ----------------------

    def write(self, frame: dict[str, Any]) -> bool:
        """Hand a frame to the drain buffer. Never blocks the calling thread.

        Returns False only when the sink is already closed (a wedged/dropped
        client) so the stream worker can stop early; an enqueued-but-not-yet-
        sent frame still returns True — delivery is the drain task's job.
        """
        if self._closed:
            return False
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Already on the loop thread (hub/supervisor synchronous emits):
            # enqueue directly, no threadsafe hop needed.
            self._enqueue(frame)
        else:
            # Agent thread: schedule the enqueue on the loop and return at once.
            try:
                self._loop.call_soon_threadsafe(self._enqueue, frame)
            except RuntimeError:
                # Loop is gone (shutting down) — treat as a closed client.
                self._closed = True
                return False
        return True

    def _enqueue(self, frame: dict[str, Any]) -> None:
        """Append to the bounded buffer, evicting the oldest on overflow.

        Runs on the loop thread. When a stalled client lets the buffer fill we
        drop the OLDEST queued event (a stale delta is the cheapest loss) and
        count an overflow strike; sustained overflow declares the client wedged
        and closes the sink so we stop feeding a dead socket.
        """
        if self._closed:
            return
        if len(self._buffer) >= _SINK_BUFFER_MAX:
            self._buffer.popleft()
            self._dropped += 1
            self._overflow_strikes += 1
            if self._overflow_strikes >= _SINK_OVERFLOW_STRIKES:
                _log.warning(
                    "websocket client wedged: %d frames dropped, closing sink", self._dropped
                )
                self._closed = True
                self._wake.set()
                return
        else:
            self._overflow_strikes = 0
        self._buffer.append(frame)
        self._wake.set()

    def start_drain(self) -> None:
        """Start the background drain task. Call once, on the loop thread."""
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = self._loop.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        while not self._closed or self._buffer:
            if not self._buffer:
                self._wake.clear()
                if self._closed:
                    return
                await self._wake.wait()
                continue
            frame = self._buffer.popleft()
            try:
                await asyncio.wait_for(
                    self._ws.send(json.dumps(frame, ensure_ascii=False)),
                    timeout=_WRITE_TIMEOUT_SECONDS,
                )
            except Exception:
                # A failed/timed-out send means the client is gone: stop the
                # turn by closing the sink (the stream worker sees write→False).
                self._closed = True
                _log.debug("websocket drain send failed; client gone", exc_info=True)
                return

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        self._closed = True
        try:
            self._wake.set()
        except Exception:
            pass


def _path_from_ws(ws: Any, fallback: str = "") -> str:
    request = getattr(ws, "request", None)
    if request is not None:
        path = getattr(request, "path", "")
        if path:
            return str(path)
    path = getattr(ws, "path", "")
    return str(path or fallback or "")


async def _recv_text(ws: Any) -> str:
    raw = await ws.recv()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


async def _close_ws(ws: Any, code: int = 1000, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        _log.debug("websocket close failed", exc_info=True)


async def _authenticate(ws: Any, path: str, token: str) -> bool:
    if query_auth_ok(path, token):
        return True
    try:
        raw = await asyncio.wait_for(_recv_text(ws), timeout=_AUTH_TIMEOUT_SECONDS)
    except Exception:
        await _close_ws(ws, 4401, "authentication required")
        return False
    ok, response = auth_message_ok(raw, token)
    if response is not None:
        try:
            await ws.send(json.dumps(response, ensure_ascii=False))
        except Exception:
            return False
    if not ok:
        await _close_ws(ws, 4401, "authentication failed")
    return ok


async def _handle_connection(ws: Any, path: str, token: str) -> None:
    path = _path_from_ws(ws, path)
    if not await _authenticate(ws, path, token):
        return
    sink = _WSSink(ws, asyncio.get_running_loop())
    sink.start_drain()
    dispatch = JsonRpcDispatcher(sink.write)
    try:
        if not await sink.write_async(hello_frame()):
            return
        while True:
            try:
                line = (await _recv_text(ws)).strip()
            except Exception:
                break
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                if not await sink.write_async(parse_error_response()):
                    break
                continue
            resp = dispatch.dispatch(req)
            if resp is not None and not await sink.write_async(resp):
                break
            if dispatch.should_close:
                await _close_ws(ws, 1000, "detached")
                break
    finally:
        sink.close()
        dispatch.close()


async def serve_forever(host: str, port: int, token: str) -> None:
    """Run the optional WebSocket transport forever."""

    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("WebSocket transport requires websockets. Install with: uv sync --extra server") from exc

    active_client = False

    async def handler(ws: Any, path: str = "") -> None:
        nonlocal active_client
        if active_client:
            await _close_ws(ws, 4409, "another client is already connected")
            return
        active_client = True
        try:
            await _handle_connection(ws, path, token)
        finally:
            active_client = False

    async with websockets.serve(handler, host, int(port)):
        await asyncio.Future()
