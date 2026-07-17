"""Stdio transport for the JSON-RPC gateway."""
from __future__ import annotations

import json
import signal
import sys
import threading
from typing import Any, TextIO

from .dispatch import JsonRpcDispatcher, hello_frame, parse_error_response


class _StdoutFrames:
    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, frame: dict[str, Any]) -> bool:
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self._stream.write(line)
                self._stream.flush()
            except (BrokenPipeError, ValueError):
                return False
        return True


def serve() -> int:
    """Serve JSON-RPC on stdin/stdout until EOF, detach, or a broken pipe."""

    # Turn the supervisor's `stop` (SIGTERM) into a CLEAN exit so the `finally`
    # cleanup + atexit hooks run — in particular the ProcessRegistry reaps the
    # chara's background process groups (its servers) instead of orphaning them.
    # Best-effort: only installable from the main thread.
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (ValueError, OSError):
        pass

    protocol_stdout = sys.stdout
    # Reserve stdout for JSON frames. Any accidental print from imported code is
    # kept off the protocol stream; diagnostics themselves are file-backed in obs/.
    sys.stdout = sys.stderr
    out = _StdoutFrames(protocol_stdout)
    dispatch = JsonRpcDispatcher(out.write)
    # The messaging host shares this dispatcher's ONE agent: an inbound WeChat
    # message runs a turn on the same handle the desktop app drives, so the
    # exchange streams into the chat window AND the reply goes back to WeChat.
    # Toggling is runtime via the messaging.start/stop RPCs (no child restart).
    try:
        from .messaging_host import MessagingHost
        from ..messaging.gateway import config_path as _messaging_config_path

        host = MessagingHost(dispatch, _messaging_config_path())
        dispatch.set_messaging_host(host)
        host.start()  # no-op unless messaging.json has enabled: true
    except Exception:
        import logging
        logging.getLogger("chara.server.stdio").exception("messaging host init failed")
    if not out.write(hello_frame()):
        dispatch.close()
        return 0
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                if not out.write(parse_error_response()):
                    return 0
                continue
            resp = dispatch.dispatch(req)
            if resp is not None and not out.write(resp):
                return 0
            if dispatch.should_close:
                break
    finally:
        dispatch.close()
    return 0
