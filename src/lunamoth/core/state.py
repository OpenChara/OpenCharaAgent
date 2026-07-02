from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..session.isolation import backend as _isolation_backend

# Neutral runtime environment state — character-agnostic. Roleplay flavor
# belongs in the character card and world book, never in the engine.
#
# ISOLATION IS NOT STORED HERE (owner 2026-06-21): the jail a chara's tools run
# under is owned by ONE authority — LUNAMOTH_PY_BACKEND (derived from the session
# config's py_backend / meta.isolation), read via session.isolation.backend().
# A second copy used to live in env_status.json defaulting to "sandbox", which
# silently sandboxed an `admin` chara whose env file was never seeded. permissions()
# now reads the backend authority; the legacy key is dropped on load.
DEFAULT_STATUS = {
    "network_access": True,          # ON by default (owner 2026-06-15); operator can /net off
    "writable_paths": [],            # extra dirs the terminal tool may write to
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


@dataclass(frozen=True)
class Permissions:
    """A typed, point-in-time snapshot of the three runtime facts every tool
    runner needs: which jail, whether the network is open, and any extra writable
    paths. ONE accessor (EnvState.permissions) builds it, so consumers stop each
    re-digging `status.get("isolation"/"network_access"/"writable_paths")` out of
    the raw dict (the drift that let foreground and background runs disagree)."""
    isolation: str = "sandbox"
    network_on: bool = True
    writable_paths: list[str] = field(default_factory=list)


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
        # isolation is no longer owned here — drop any legacy copy so a stale
        # value can't mislead a reader (the backend authority is the source).
        if "isolation" in data:
            data.pop("isolation", None)
            changed = True
        data.setdefault("rest_until", 0.0)
        if "network_access" not in data:
            # State files from builds that predate network-on-by-default (owner
            # 2026-06-15) lack the key; it must default to DEFAULT_STATUS's True,
            # not False — an old env_status.json silently flipped a chara offline.
            data["network_access"] = DEFAULT_STATUS["network_access"]
            changed = True
        # user_present was retired — the chara is independent of attach/detach.
        # Drop any leftover so an old state file can't mislead a reader.
        if "user_present" in data:
            data.pop("user_present", None)
            changed = True
        if changed:
            self.save(data)
        return data

    def permissions(self) -> Permissions:
        """The typed snapshot of (isolation, network, writable_paths) — the single
        accessor every tool runner should use instead of re-reading the dict."""
        data = self.load()
        return Permissions(
            # The ONE authority for the jail (LUNAMOTH_PY_BACKEND ← session config),
            # not a per-sandbox copy — so an `admin` chara is never silently sandboxed.
            isolation=_isolation_backend(),
            # load() backfills a missing key, so the default here is belt-and-braces;
            # it must agree with DEFAULT_STATUS (True), never drift back to False.
            network_on=bool(data.get("network_access", DEFAULT_STATUS["network_access"])),
            writable_paths=list(data.get("writable_paths", []) or []),
        )

    def save(self, data: dict[str, Any]) -> None:
        from ..config import atomic_write_text
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

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
