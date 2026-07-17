"""In-memory ring of recent log lines (AstrBot's LogBroker pattern).

The TUI's log panel reads tail(); a future web client tails the same ring over
SSE. Purely additive — files remain the durable record (log.py)."""
from __future__ import annotations

import logging
from collections import deque

CACHED_LINES = 500


class LogBroker(logging.Handler):
    def __init__(self, maxlen: int = CACHED_LINES):
        super().__init__()
        self.ring: deque[str] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.append(self.format(record))
        except Exception:  # a broken log line must never break the program
            pass

    def tail(self, n: int = 100) -> list[str]:
        return list(self.ring)[-n:]


broker = LogBroker()
