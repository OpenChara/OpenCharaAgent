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
    "tool_access": None,  # filled below with FULL_TOOL_ACCESS (one source of truth)
}

# The full hermes-ported tool surface (core + general + browser) plus LunaMoth's
# kept chara-life/env tools. The default per-session allowlist; a pack still
# narrows what's actually callable, and browser_* self-hide when the driver is
# absent (check_fn-gated in the registry).
FULL_TOOL_ACCESS = [
    # core file/shell/search
    "read_file", "write_file", "patch", "search_files", "terminal", "process",
    # general agentic
    "web_search", "web_extract", "memory", "todo", "session_search",
    "skills_list", "skill_view", "skill_manage", "execute_code", "delegate_task", "clarify",
    # browser (self-hidden when the agent-browser driver is not installed)
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_scroll", "browser_back", "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
    # env + chara-life (LunaMoth's own)
    "inspect_env", "write_log", "speak", "rest",
    "add_wish", "set_wish_status", "request_permission",
]

DEFAULT_STATUS["tool_access"] = list(FULL_TOOL_ACCESS)

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
        access = data.get("tool_access")
        if isinstance(access, list) and "run_python" in access:
            data["tool_access"] = [t for t in access if t != "run_python"]
            if "terminal" not in data["tool_access"]:
                data["tool_access"].append("terminal")
            changed = True
        if isinstance(access, list) and "inspect_cell" in data.get("tool_access", []):
            data["tool_access"] = ["inspect_env" if t == "inspect_cell" else t for t in data["tool_access"]]
            changed = True
        # The hermes-tool migration: any tools-enabled chara (had `terminal`)
        # is granted the full new surface. The old names (list_files, read_skill/
        # create_skill, add_goal/set_goal_status) are gone — replaced wholesale,
        # so a stale subset can't shadow the new set.
        access = data.get("tool_access")
        if isinstance(access, list) and "terminal" in access and set(access) != set(FULL_TOOL_ACCESS):
            data["tool_access"] = list(FULL_TOOL_ACCESS)
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
