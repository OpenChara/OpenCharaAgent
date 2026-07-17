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

    def keyword_matches(self, scan_text: str, *, lowered: bool = False) -> bool:
        """For non-constant entries, any primary key must appear.

        When `selective` with secondary keys, at least one secondary key must
        also appear (a small subset of ST's AND/NOT logic — enough to be useful).

        ``lowered=True`` marks *scan_text* as already lowercased, so a caller
        scanning many entries (``Lorebook.recall_entries``) lowercases the —
        potentially large — scan text once per scan instead of once per entry.
        """
        if not self.enabled or self.constant or not self.keys:
            return False
        haystack = scan_text if lowered else scan_text.lower()
        primary_hit = any(k.lower() in haystack for k in self.keys if k)
        if not primary_hit:
            return False
        if self.selective and self.secondary_keys:
            return any(k.lower() in haystack for k in self.secondary_keys if k)
        return True


def entry_to_book_dict(d: dict[str, Any], entry_id: int = 0) -> dict[str, Any]:
    """Normalize one entry (standalone-world or character_book shape) to the
    standard ST V3 `character_book` entry dict. Used when folding a world book
    into a card — the card is the ONE external file."""
    e = WorldEntry.from_dict(d, entry_id)
    return {
        "id": e.entry_id,
        "keys": e.keys,
        "secondary_keys": e.secondary_keys,
        "comment": e.comment,
        "content": e.content,
        "constant": e.constant,
        "selective": e.selective,
        "enabled": e.enabled,
        "insertion_order": e.order,
    }


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
        """Stable always-on entries — the world's fixed overview. These belong in
        the CACHED prefix: they are the cheap, permanent base layer, while
        everything keyword-shaped goes through ``recall_entries``."""
        hits = [e for e in self.entries if e.enabled and e.constant and e.content.strip()]
        hits.sort(key=lambda e: e.order)
        return [apply_macros(e.content, char, user).strip() for e in hits]

    def recall_entries(self, scan_text: str) -> list[WorldEntry]:
        """World memory recall: the keyword entries relevant to the recent context.

        The retrieval seam for DYNAMIC world information — a future GM model
        replaces the matching below without touching the prompt assembly.
        Today's matcher: an entry activates when a primary key appears in
        *scan_text*. No sticky state: the shallow scan window itself smooths
        recall across turns. Constant entries are the static base layer and
        live in the cached prefix instead (``constant_blocks``)."""
        haystack = scan_text.lower()  # one pass per scan, not one per entry
        out: list[WorldEntry] = []
        for entry in self.entries:
            if not entry.enabled or not entry.content.strip():
                continue
            if entry.keyword_matches(haystack, lowered=True):
                out.append(entry)
        return out

