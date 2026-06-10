from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


def estimate_tokens(text: str) -> int:
    # Conservative-ish mixed Chinese/English approximation.
    # For context-budget bookkeeping, an exact tokenizer is unnecessary.
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = max(0, len(text) - cjk)
    return cjk + other // 4


@dataclass
class ContextBuffer:
    max_tokens: int = 65536
    trim_buffer_tokens: int = 4096
    messages: list[tuple[str, str]] = field(default_factory=list)
    # Called for every NEW line (not for restored history); the transcript store
    # hooks in here. Trimming only narrows the in-memory window \u2014 disk keeps all.
    persist: "Callable[[str, str], None] | None" = None

    def add(self, role: str, content: str) -> None:
        self.messages.append((role, content))
        if self.persist is not None:
            self.persist(role, content)
        self.trim()

    def restore(self, rows: list[tuple[str, str]]) -> None:
        """Load previously persisted history WITHOUT re-persisting it."""
        self.messages = list(rows)
        self.trim()

    def render(self) -> list[tuple[str, str]]:
        return list(self.messages)

    def token_count(self) -> int:
        return sum(estimate_tokens(role) + estimate_tokens(content) for role, content in self.messages)

    def trim(self) -> None:
        target = max(0, self.max_tokens - self.trim_buffer_tokens)
        while self.messages and self.token_count() > target:
            self.messages.pop(0)
