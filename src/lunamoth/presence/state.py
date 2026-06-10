"""Per-session presence state: first-meeting flag + cross-process handoff event."""
from __future__ import annotations

import json
from pathlib import Path

STATE_FILENAME = "presence.json"


class PresenceState:
    """Tiny state file inside the session sandbox.

    `met` — whether the chara has ever seen the operator (first boot shows the
    card's first_mes; later attaches get a live arrival turn instead).
    `pending` — a context line queued by the process that detached, consumed by
    the next process that adopts the chara (e.g. the background daemon).
    """

    def __init__(self, sandbox_root: Path):
        self.path = sandbox_root / STATE_FILENAME

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # presence is best-effort; never kill the host loop

    def first_meeting(self) -> bool:
        """True until mark_met() — the chara has never seen the operator before."""
        return not self._load().get("met", False)

    def mark_met(self) -> None:
        data = self._load()
        if not data.get("met"):
            data["met"] = True
            self._save(data)

    def queue_event(self, text: str) -> None:
        """Leave a context line for the next process that adopts this chara."""
        data = self._load()
        data["pending"] = text
        self._save(data)

    def pop_event(self) -> str:
        """Consume the pending handoff line (empty string if none)."""
        data = self._load()
        pending = str(data.pop("pending", "") or "")
        if pending:
            self._save(data)
        return pending
