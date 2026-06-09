from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_STATUS = {
    "containment_level": 3,
    "trust": 10,
    "hostility": 35,
    "memory_integrity": 92,
    "network_access": False,
    "shell_access": False,
    "tool_access": ["inspect_cell", "read_memory", "write_memory", "list_files", "read_file", "write_file", "write_log"],
}


class ContainmentState:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_STATUS)

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return dict(DEFAULT_STATUS)

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def adjust(self, trust_delta: int = 0, hostility_delta: int = 0) -> dict[str, Any]:
        data = self.load()
        data["trust"] = max(0, min(100, int(data.get("trust", 0)) + trust_delta))
        data["hostility"] = max(0, min(100, int(data.get("hostility", 0)) + hostility_delta))
        self.save(data)
        return data
