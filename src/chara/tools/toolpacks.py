from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import content_dir


# Tool packs are an OpenCharaAgent concept (SillyTavern cards stay pure persona).
# A pack is a named, composable bundle of tool names that you bind to ANY persona
# at launch — so "what it is" (card) and "what it can do" (pack) are independent.
# content_dir: repo-root toolpacks/ in a dev checkout, the wheel-bundled copy in an install.
TOOLPACKS_DIR = content_dir("toolpacks")


@dataclass
class ToolPack:
    name: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    note: str = ""  # optional extra system guidance appended when this pack is active
    # MCP opt-in: configured server names this pack admits, or ["*"] for all.
    # (Configuring a server in mcp.json is the operator's trust decision; the
    # pack decides whether THIS capability bundle includes it.)
    mcp_servers: list[str] = field(default_factory=list)
    source_path: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "ToolPack":
        p = Path(path)
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(
            name=str(d.get("name", p.stem)),
            description=str(d.get("description", "")),
            tools=[str(t) for t in (d.get("tools", []) or [])],
            note=str(d.get("note", "")),
            mcp_servers=[str(x) for x in (d.get("mcp_servers", []) or [])],
            source_path=str(p),
        )


def resolve_toolpack_path(value: str) -> Path | None:
    """A toolpack setting is either a bare name ('sandbox') or a .json path."""
    v = (value or "").strip()
    if not v:
        return None
    if v.endswith(".json") or "/" in v or "\\" in v:
        p = Path(v).expanduser()
    else:
        p = TOOLPACKS_DIR / f"{v}.json"
    return p if p.exists() else None


def load_toolpack(value: str) -> ToolPack | None:
    p = resolve_toolpack_path(value)
    if p is None:
        return None
    return ToolPack.load(p)
