"""Per-chara goal list — charas are goal-driven agents.

Design synthesis of three references:

- Claude Code tasks: structured items with states, SELF-managed by the agent
  through tools (add_goal / set_goal_status) — no separate completion-checker
  calls (SillyTavern's Objective extension doubles API cost doing that; our
  rules layer's honesty standard governs self-reported completion instead).
- SillyTavern Objective: the active goals are injected into the system prompt
  so they steer every turn.
- AstrBot's future-task awakener: goals + the idle loop (empty user message =
  unattended time) mean a daemon'd chara works toward its goals on its own —
  the awakener is the forever loop we already have.

Goals come from two owners: the operator (/goal — shown with a ⭑) and the
chara itself (its own ambitions, via tools). Both live in one file in the
session sandbox: goals.json. Best-effort persistence — a failing disk must
never kill the host loop.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

STATUSES = ("active", "done", "dropped")


class GoalStore:
    def __init__(self, path: Path):
        self.path = path

    # ---- storage --------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("goals"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"seq": 0, "goals": []}

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- operations -----------------------------------------------------------------

    def add(self, text: str, by: str = "chara") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("goal text is empty")
        owner = (by or "chara").strip() or "chara"
        data = self._load()
        data["seq"] = int(data.get("seq", 0)) + 1
        goal = {
            "id": f"g{data['seq']}",
            "text": text[:500],
            "status": "active",
            "by": owner,
            "ts": time.time(),
        }
        data["goals"].append(goal)
        self._save(data)
        return goal

    def is_empty(self) -> bool:
        return not self._load()["goals"]

    def seed_once(self, goals: list[str], by: str = "card") -> list[dict[str, Any]]:
        """Seed an initial goal list only for a brand-new/empty store."""
        if not goals or not self.is_empty():
            return []
        added: list[dict[str, Any]] = []
        for text in goals:
            if str(text).strip():
                added.append(self.add(str(text), by=by))
        return added

    def set_status(self, goal_id: str, status: str) -> dict[str, Any]:
        status = (status or "").strip().lower()
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        data = self._load()
        for goal in data["goals"]:
            if goal.get("id") == goal_id:
                goal["status"] = status
                goal["ts"] = time.time()
                self._save(data)
                return goal
        raise ValueError(f"no goal with id {goal_id!r}")

    def all(self) -> list[dict[str, Any]]:
        return list(self._load()["goals"])

    def active(self) -> list[dict[str, Any]]:
        return [g for g in self.all() if g.get("status") == "active"]

    # ---- prompt block ---------------------------------------------------------------

    def render_block(self) -> str:
        """The system-prompt wishes block ('' when no active wishes).

        Operator wishes are marked; the framing stays functional — how a chara
        pursues a wish is its card's business, never the engine's.
        """
        active = self.active()
        if not active:
            return ""
        lines = ["Your current wishes (⭑ = set by the operator):"]
        for g in active:
            mark = "⭑ " if g.get("by") == "operator" else ""
            lines.append(f"  {g['id']}: {mark}{g['text']}")
        lines.append(
            "Pursue them in your own way and pace. When one is truly fulfilled, mark it with "
            "set_wish_status — never claim completion that isn't real. You may add wishes of "
            "your own with add_wish, and drop ones that no longer matter."
        )
        return "\n".join(lines)
