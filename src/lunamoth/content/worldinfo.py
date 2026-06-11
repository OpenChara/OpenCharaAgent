from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def apply_macros(text: str, char: str, user: str) -> str:
    if not text:
        return text
    # SillyTavern's core macros. We deliberately keep this small.
    return (
        text.replace("{{char}}", char)
        .replace("{{user}}", user)
        .replace("<USER>", user)
        .replace("<BOT>", char)
    )


@dataclass
class WorldEntry:
    entry_id: int = 0
    keys: list[str] = field(default_factory=list)
    secondary_keys: list[str] = field(default_factory=list)
    content: str = ""
    constant: bool = False
    selective: bool = False
    enabled: bool = True
    order: int = 100
    comment: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any], entry_id: int = 0) -> "WorldEntry":
        # Accept both standalone-world fields and embedded character_book fields.
        keys = d.get("keys") or d.get("key") or []
        secondary = d.get("secondary_keys") or d.get("keysecondary") or []
        if isinstance(keys, str):
            keys = [keys]
        if isinstance(secondary, str):
            secondary = [secondary]
        enabled = d.get("enabled")
        if enabled is None:
            enabled = not bool(d.get("disable", False))
        order = d.get("insertion_order")
        if order is None:
            order = d.get("order", 100)
        return cls(
            entry_id=int(entry_id),
            keys=[str(k) for k in keys],
            secondary_keys=[str(k) for k in secondary],
            content=str(d.get("content", "")),
            constant=bool(d.get("constant", False)),
            selective=bool(d.get("selective", False)),
            enabled=bool(enabled),
            order=int(order) if str(order).lstrip("-").isdigit() else 100,
            comment=str(d.get("comment", "")),
        )

    def keyword_matches(self, scan_text: str) -> bool:
        """For non-constant entries, any primary key must appear.

        When `selective` with secondary keys, at least one secondary key must
        also appear (a small subset of ST's AND/NOT logic — enough to be useful).
        """
        if not self.enabled or self.constant or not self.keys:
            return False
        haystack = scan_text.lower()
        primary_hit = any(k.lower() in haystack for k in self.keys if k)
        if not primary_hit:
            return False
        if self.selective and self.secondary_keys:
            return any(k.lower() in haystack for k in self.secondary_keys if k)
        return True


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = max(0, len(text) - cjk)
    return cjk + other // 4


@dataclass
class Lorebook:
    name: str = ""
    entries: list[WorldEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any], name: str = "") -> "Lorebook":
        raw = d.get("entries", d if "entries" not in d else {})
        items: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            items = list(raw.values())
        elif isinstance(raw, list):
            items = list(raw)
        entries = [WorldEntry.from_dict(e, i) for i, e in enumerate(items) if isinstance(e, dict)]
        return cls(name=d.get("name", name), entries=entries)

    @classmethod
    def load(cls, path: str | Path) -> "Lorebook":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data, name=p.stem)

    def constant_blocks(self, char: str, user: str) -> list[str]:
        """Stable always-on entries. These belong in the cached prefix."""
        hits = [e for e in self.entries if e.enabled and e.constant and e.content.strip()]
        hits.sort(key=lambda e: e.order)
        return [apply_macros(e.content, char, user).strip() for e in hits]

    def keyword_entries(
        self,
        scan_text: str,
        *,
        sticky: dict[str, int] | None = None,
        namespace: str = "",
        sticky_turns: int = 4,
    ) -> list[WorldEntry]:
        """Volatile keyword-activated entries.

        `sticky` is caller-owned session state: activated entries remain active
        for `sticky_turns` subsequent turns even if the keyword disappears.
        """
        state = sticky if sticky is not None else {}
        prefix = namespace or self.name or "world"

        hits: set[int] = set()
        for entry in self.entries:
            if entry.keyword_matches(scan_text):
                hits.add(entry.entry_id)
                state[f"{prefix}:{entry.entry_id}"] = int(sticky_turns)

        active: list[WorldEntry] = []
        active_ids = {e.entry_id for e in self.entries if e.entry_id in hits}
        for entry in self.entries:
            if not entry.enabled or entry.constant or not entry.content.strip():
                continue
            key = f"{prefix}:{entry.entry_id}"
            if entry.entry_id in active_ids or state.get(key, 0) > 0:
                active.append(entry)

        # Consume one sticky turn for entries that did not freshly match. A
        # fresh match resets the counter and is not consumed until the next scan.
        known = {f"{prefix}:{e.entry_id}" for e in self.entries}
        for key in list(state):
            if key not in known:
                continue
            try:
                entry_id = int(key.rsplit(":", 1)[1])
            except ValueError:
                continue
            if entry_id in hits:
                continue
            remaining = int(state.get(key, 0) or 0)
            if remaining > 1:
                state[key] = remaining - 1
            else:
                state.pop(key, None)

        active.sort(key=lambda e: e.order)
        return active

    def keyword_blocks(
        self,
        scan_text: str,
        char: str,
        user: str,
        *,
        sticky: dict[str, int] | None = None,
        namespace: str = "",
        sticky_turns: int = 4,
        budget_tokens: int | None = None,
    ) -> list[str]:
        active = self.keyword_entries(
            scan_text, sticky=sticky, namespace=namespace, sticky_turns=sticky_turns
        )
        blocks = [apply_macros(e.content, char, user).strip() for e in active]
        if budget_tokens is not None and budget_tokens > 0:
            capped: list[str] = []
            used = 0
            for block in blocks:
                cost = _estimate_tokens(block) + 2
                if capped and used + cost > budget_tokens:
                    break
                if not capped and cost > budget_tokens:
                    break
                capped.append(block)
                used += cost
            blocks = capped
        return blocks

    def activate(self, scan_text: str, char: str, user: str) -> list[str]:
        """Backward-compatible full activation view."""
        return self.constant_blocks(char, user) + self.keyword_blocks(scan_text, char, user)

    def render(self, scan_text: str, char: str, user: str) -> str:
        blocks = self.activate(scan_text, char, user)
        if not blocks:
            return ""
        return "[World Info / 世界书]\n" + "\n\n".join(blocks)
