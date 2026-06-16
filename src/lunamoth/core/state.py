from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Neutral runtime environment state — character-agnostic. Roleplay flavor
# belongs in the character card and world book, never in the engine.
DEFAULT_STATUS = {
    "isolation": "sandbox",          # dir | sandbox | docker (informational)
    "network_access": True,          # ON by default (owner 2026-06-15); operator can /net off
    "writable_paths": [],            # extra dirs the terminal tool may write to
    "user_present": False,           # is an operator attached right now? (set by TUI/daemon)
    "rest_until": 0.0,               # epoch until which the chara chose to rest (rest tool)
}
# NOTE: there is deliberately NO per-session `tool_access` list here. Which tools
# a chara can call is `registry ∩ pack` (the toolpack is the allowlist), gated in
# tools/gateway.py. A separate hand-kept list was a redundant 4th owner that
# silently deleted newly-registered tools; it was retired 2026-06-16. Runtime
# capability toggles (e.g. `/net off`) gate at call time via `network_access`.

# Legacy keys to drop from any persisted state written by old builds.
_LEGACY_KEYS = (
    "_".join(("con" + "tainment", "level")),
    "tr" + "ust",
    "host" + "ility",
    "memory_" + "integrity",
)


class EnvState:
    """Persisted, mutable, neutral environment state for a session."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_STATUS)

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return dict(DEFAULT_STATUS)
        # Migrate state files written by older builds.
        changed = False
        for key in _LEGACY_KEYS:
            if key in data:
                data.pop(key, None)
                changed = True
        # tool_access was retired (gating is registry ∩ pack now) — drop any
        # leftover from old state files so it can't mislead a reader.
        if "tool_access" in data:
            data.pop("tool_access", None)
            changed = True
        data.setdefault("isolation", "sandbox")
        data.setdefault("user_present", False)
        data.setdefault("rest_until", 0.0)
        if changed:
            self.save(data)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_present(self, present: bool) -> dict[str, Any]:
        data = self.load()
        data["user_present"] = bool(present)
        self.save(data)
        return data

    def set_network(self, allowed: bool) -> dict[str, Any]:
        data = self.load()
        data["network_access"] = bool(allowed)
        self.save(data)
        return data

    def set_rest_until(self, when: float) -> dict[str, Any]:
        data = self.load()
        data["rest_until"] = float(when)
        self.save(data)
        return data

    def clear_rest(self) -> None:
        """A word from the user always wakes the chara early. Cheap no-op when
        not resting (no disk write)."""
        data = self.load()
        if data.get("rest_until"):
            data["rest_until"] = 0.0
            self.save(data)

    def add_writable_path(self, path: str) -> dict[str, Any]:
        data = self.load()
        paths = list(data.get("writable_paths", []))
        if path not in paths:
            paths.append(path)
        data["writable_paths"] = paths
        self.save(data)
        return data
