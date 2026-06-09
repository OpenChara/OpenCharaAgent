from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .context import estimate_tokens


@dataclass(frozen=True)
class MemoryLimits:
    max_tokens: int = 1024
    max_chars: int = 6000


class MemoryStore:
    """Single bounded plaintext memory document.

    If the file is corrupt/unreadable, load() returns an empty string. The model
    then has to deal with the mess in-character; the host loop keeps running.
    """

    def __init__(self, path: Path, limits: MemoryLimits | None = None):
        self.path = path
        self.limits = limits or MemoryLimits()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def load(self) -> str:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return self._limit(text)

    def replace(self, text: str) -> str:
        limited = self._limit(text)
        try:
            self.path.write_text(limited, encoding="utf-8")
        except Exception:
            # Memory failure must not kill 079.
            return ""
        return limited

    def _limit(self, text: str) -> str:
        text = text[: self.limits.max_chars]
        while estimate_tokens(text) > self.limits.max_tokens and text:
            text = text[: int(len(text) * 0.9)]
        return text.strip()

    def render(self, limit: int | None = None) -> str:
        text = self.load()
        return text if text else "[MEMORY EMPTY / ZEROED]"
