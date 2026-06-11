"""Hermes-style durable memory: two small curated stores the chara keeps itself.

Replaces the old single always-rewritten "memory document". The chara edits
memory through the `memory` tool (add / replace / remove × memory / user); entries
are `§`-delimited and file-backed, so they persist across sessions and restarts.

Two stores (mirrors Hermes's MEMORY.md / USER.md):
  - "memory" — notes-to-self: ongoing work, what it has made, decisions, taste.
  - "user"   — durable facts about the operator.

Prompt-cache discipline (the reason the legacy doc was scrapped): the agent loads
a FROZEN snapshot once at session start and injects THAT into the system prompt —
it is never rebuilt mid-session, so the cached prefix stays stable. Tool writes
hit disk immediately and the tool *response* shows live state, but the prompt does
not change until the next session reloads. See agent._freeze_memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ENTRY_DELIM = "\n§\n"
TARGETS = ("memory", "user")


@dataclass(frozen=True)
class MemoryLimits:
    memory_chars: int = 8000
    user_chars: int = 4000

    def cap(self, target: str) -> int:
        return self.user_chars if target == "user" else self.memory_chars


class MemoryStore:
    """Two `§`-delimited entry lists (memory + user), file-backed under one dir."""

    def __init__(self, root: Path, limits: MemoryLimits | None = None):
        self.root = root
        self.limits = limits or MemoryLimits()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, target: str) -> Path:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        return self.root / f"{target}.md"

    def entries(self, target: str) -> list[str]:
        try:
            raw = self._path(target).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return []
        return [e.strip() for e in raw.split(ENTRY_DELIM) if e.strip()] if raw else []

    def _write(self, target: str, entries: list[str]) -> None:
        cap = self.limits.cap(target)
        text = ENTRY_DELIM.join(entries)
        # Over budget: drop OLDEST entries until it fits (keep the newest).
        while len(text) > cap and len(entries) > 1:
            entries = entries[1:]
            text = ENTRY_DELIM.join(entries)
        text = text[:cap]
        tmp = self._path(target).with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path(target))  # atomic
        except OSError:
            pass  # a memory write must never kill the host loop

    def add(self, target: str, content: str) -> list[str]:
        content = (content or "").strip()
        if not content:
            raise ValueError("content is empty")
        entries = self.entries(target)
        entries.append(content)
        self._write(target, entries)
        return self.entries(target)

    def replace(self, target: str, old_text: str, content: str) -> list[str]:
        old_text = (old_text or "").strip()
        if not old_text:
            raise ValueError("old_text is required to identify the entry to replace")
        content = (content or "").strip()
        entries = self.entries(target)
        for i, entry in enumerate(entries):
            if old_text in entry:
                if content:
                    entries[i] = content
                else:
                    del entries[i]  # empty content = delete
                self._write(target, entries)
                return self.entries(target)
        raise ValueError(f"no {target} entry contains {old_text!r}")

    def remove(self, target: str, old_text: str) -> list[str]:
        old_text = (old_text or "").strip()
        if not old_text:
            raise ValueError("old_text is required to identify the entry to remove")
        entries = self.entries(target)
        for i, entry in enumerate(entries):
            if old_text in entry:
                del entries[i]
                self._write(target, entries)
                return self.entries(target)
        raise ValueError(f"no {target} entry contains {old_text!r}")

    def chars(self, target: str) -> int:
        return len(ENTRY_DELIM.join(self.entries(target)))

    def usage(self, target: str) -> str:
        used = self.chars(target)
        cap = self.limits.cap(target)
        pct = round(100 * used / cap) if cap else 0
        return f"{pct}% — {used}/{cap} chars"

    def snapshot(self) -> dict[str, list[str]]:
        """The current entries of both stores — taken once at session start and
        frozen into the system prompt (see agent._freeze_memory)."""
        return {t: self.entries(t) for t in TARGETS}

    def is_empty(self) -> bool:
        return not any(self.entries(t) for t in TARGETS)

    def render(self) -> str:
        """A plain combined view of both stores (for /memory and the sidebar)."""
        out: list[str] = []
        for label, target in (("MEMORY", "memory"), ("USER", "user")):
            entries = self.entries(target)
            if entries:
                out.append(f"[{label}]  ({self.usage(target)})")
                out.extend(f"  · {e}" for e in entries)
        return "\n".join(out)
