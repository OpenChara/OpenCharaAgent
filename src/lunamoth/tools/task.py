"""Per-chara tasks — the chara's own life-threads, advanced toward its aspiration.

Codename `task` (user-facing term: 任务 / "Task"). This is the instrumental
"what I'm doing now" layer that sits UNDER the user-owned aspiration (`polaris`):

    aspiration (polaris)  — the why; user-owned, read-only, never "completed".
      ↑ advanced through
    task (this module)    — the chara's own threads it carries over time; it sets,
                            edits, and completes them. PERSISTENT (task.json),
                            rendered into every turn's volatile tail (active only),
                            survives restarts.
      ↑ broken into
    todo (tools.builtin.todo) — the immediate, in-session checklist of steps.
                            EPHEMERAL (in-memory, not persisted, not in the prompt).

A task is the arc; a todo is the moment's steps. They are deliberately distinct:
todo is the scratchpad for getting one thing done right now; a task is a thread of
the chara's life. (This replaces the old chara-mutable wish/goal list — but the
VALUE-direction still lives only in the user-owned aspiration; a task is purely the
instrumental "what I'm working on toward it", never a built-in value of its own.)

Completing a task SEALS it: it becomes an immutable record (shown collapsed in the
UI, never re-rendered into the prompt). The chara cannot edit, reopen, or delete a
sealed task. Active tasks are fully editable and can be abandoned (removed).

Best-effort persistence — a failing disk must never kill the host loop.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_MAX_CONTENT = 280   # rendered every turn — keep each task short
_MAX_ACTIVE = 12     # a few real threads, never a scatter of trivia
_MAX_DONE = 50       # sealed records kept for the UI fold; oldest trimmed beyond this


class TaskStore:
    """The chara's tasks — a persistent, chara-writable list of life-threads."""

    def __init__(self, path: Path):
        self.path = path

    # ---- persistence -------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"tasks": [], "seq": 0}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _cap(text: str) -> str:
        text = (text or "").strip()
        return text[:_MAX_CONTENT]

    # ---- queries -----------------------------------------------------------
    def _items(self) -> list[dict[str, Any]]:
        return [t for t in self._read().get("tasks", []) if isinstance(t, dict)]

    def active(self) -> list[dict[str, Any]]:
        return [t for t in self._items() if t.get("status") != "done"]

    def done(self) -> list[dict[str, Any]]:
        return [t for t in self._items() if t.get("status") == "done"]

    def payload(self) -> dict[str, list[dict[str, Any]]]:
        """For the frontend: active threads + sealed (done) records, newest done first."""
        done = sorted(self.done(), key=lambda t: t.get("done_at") or 0, reverse=True)
        return {"active": self.active(), "done": done}

    # ---- mutations (the chara's `task` tool) -------------------------------
    def add(self, content: str) -> dict[str, Any]:
        content = self._cap(content)
        if not content:
            raise ValueError("a task needs a description")
        data = self._read()
        if len(self.active()) >= _MAX_ACTIVE:
            raise ValueError(
                f"you already have {_MAX_ACTIVE} active tasks — finish or drop one "
                "before adding another (keep your threads few and real)"
            )
        seq = int(data.get("seq", 0)) + 1
        item = {"id": f"t{seq}", "content": content, "status": "active",
                "created": time.time()}
        data["tasks"].append(item)
        data["seq"] = seq
        self._write(data)
        return item

    def update(self, task_id: str, content: str) -> dict[str, Any]:
        content = self._cap(content)
        if not content:
            raise ValueError("a task needs a description")
        data = self._read()
        item = self._find(data, task_id)
        if item.get("status") == "done":
            raise ValueError(f"{task_id} is sealed (already done) and cannot be edited")
        item["content"] = content
        self._write(data)
        return item

    def complete(self, task_id: str) -> dict[str, Any]:
        data = self._read()
        item = self._find(data, task_id)
        if item.get("status") == "done":
            raise ValueError(f"{task_id} is already done")
        item["status"] = "done"
        item["done_at"] = time.time()
        self._trim_done(data)
        self._write(data)
        return item

    def remove(self, task_id: str) -> dict[str, Any]:
        data = self._read()
        item = self._find(data, task_id)
        if item.get("status") == "done":
            raise ValueError(f"{task_id} is sealed (done) — a finished task is a record, not deletable")
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != task_id]
        self._write(data)
        return item

    @staticmethod
    def _find(data: dict[str, Any], task_id: str) -> dict[str, Any]:
        for t in data.get("tasks", []):
            if isinstance(t, dict) and t.get("id") == task_id:
                return t
        raise ValueError(f"no task with id {task_id!r}")

    @staticmethod
    def _trim_done(data: dict[str, Any]) -> None:
        done = [t for t in data["tasks"] if t.get("status") == "done"]
        if len(done) <= _MAX_DONE:
            return
        drop = {id(t) for t in sorted(done, key=lambda t: t.get("done_at") or 0)[: len(done) - _MAX_DONE]}
        data["tasks"] = [t for t in data["tasks"] if id(t) not in drop]

    # ---- seeding from the card --------------------------------------------
    def seed_once(self, value: Any) -> bool:
        """Seed a starter task (or tasks) from the card, only when the store is
        still empty — so a chara's own later edits are never clobbered by a
        reconfigure. Accepts a string or a list of strings."""
        if self._items():
            return False
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = [str(v) for v in value]
        else:
            return False
        seeded = False
        for content in items:
            content = self._cap(content)
            if content:
                try:
                    self.add(content)
                    seeded = True
                except ValueError:
                    break
        return seeded

    # ---- the volatile-tail block (active only) -----------------------------
    def render_block(self, *, has_aspiration: bool = False) -> str:
        """The per-turn block for the system prompt. Active tasks only; sealed
        ones never re-enter the prompt. Empty state is a non-coercive invitation."""
        active = self.active()
        if active:
            lines = "\n".join(f"  [{t.get('id')}] {t.get('content', '')}" for t in active)
            return (
                "Your tasks — the threads you're advancing toward your aspiration "
                "(manage them with the `task` tool):\n" + lines
            )
        # No active task: invite, don't compel.
        if has_aspiration:
            return (
                "You have no task right now. If something you could move toward your "
                "aspiration comes to mind, set it with the `task` tool to keep a thread "
                "going — or have none, if this stretch of life is better lived at the "
                "pace of the moment."
            )
        return ""
