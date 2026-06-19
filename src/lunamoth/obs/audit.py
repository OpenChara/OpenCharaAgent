from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **data: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        # Ensure the directory exists AT WRITE TIME, not just at construction: a
        # session's sandbox/logs/ can be created lazily or wiped (reset/cleanup)
        # between constructing this log and the first tool call, and the audit
        # trail must never crash the tool loop.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"raw": line})
        return out
