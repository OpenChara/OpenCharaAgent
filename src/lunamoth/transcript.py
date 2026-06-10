"""Durable per-chara conversation transcript — SQLite, survives restarts.

Every context line (user / assistant / system) is appended as it happens, so a
chara keeps its conversation across detach/attach and daemon handoffs. Tool
calls are recorded too (kind='tool') for audit/forensics, but are not reloaded
into the context window yet (that is the separate tool-call-retention item).

`/reset` does not delete history: it bumps an epoch, and only the current
epoch is reloaded — old epochs stay on disk for forensics, like everything
else in the sandbox.

Storage design adapted from hermes-agent's hermes_state.py (MIT License,
Nous Research): WAL journal mode for cheap concurrent reads, with a fallback
to DELETE mode on filesystems where WAL's shared-memory locking is broken
(NFS/SMB/some FUSE mounts).

Persistence is best-effort: a failing disk must degrade to an in-memory-only
conversation, never kill the host loop.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# Markers of WAL-incompatible filesystems (from hermes-agent, MIT).
_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized", "disk i/o error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_epoch ON messages(epoch, id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _apply_wal_with_fallback(conn: sqlite3.Connection) -> None:
    """journal_mode=WAL, falling back to DELETE on WAL-incompatible filesystems.

    Adapted from hermes-agent's apply_wal_with_fallback (MIT)."""
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row and row[0] == "wal":
            return
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if not any(m in str(exc).lower() for m in _WAL_INCOMPAT_MARKERS):
            raise
        conn.execute("PRAGMA journal_mode=DELETE")


class TranscriptStore:
    """Append-only conversation log for one chara.

    Connections are opened per call: writes come from UI worker threads and
    reads from the main thread, and short-lived connections sidestep SQLite's
    cross-thread rules entirely (WAL keeps them cheap)."""

    def __init__(self, path: Path):
        self.path = path
        self.available = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError):
            self.available = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        _apply_wal_with_fallback(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- epoch (/reset boundary) ---------------------------------------------------

    def epoch(self) -> int:
        if not self.available:
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            return int(row[0]) if row else 0
        except (sqlite3.Error, OSError, ValueError):
            return 0

    def reset(self) -> int:
        """Start a new epoch. Old messages stay on disk but are no longer loaded."""
        if not self.available:
            return 0
        new = self.epoch() + 1
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('epoch', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(new),),
                )
            return new
        except (sqlite3.Error, OSError):
            return new

    # ---- messages -------------------------------------------------------------------

    def append(self, role: str, content: str, kind: str = "chat") -> None:
        if not self.available or not content:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO messages(epoch, role, content, kind, ts) VALUES(?,?,?,?,?)",
                    (self.epoch(), role, content, kind, time.time()),
                )
        except (sqlite3.Error, OSError):
            pass  # best-effort: never kill the host loop over a transcript write

    def append_tool(self, name: str, args: dict, result: str) -> None:
        """Record a tool call for forensics (not reloaded into context yet)."""
        try:
            payload = json.dumps({"tool": name, "args": args, "result": result[:2000]}, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = json.dumps({"tool": name, "result": str(result)[:2000]}, ensure_ascii=False)
        self.append("tool", payload, kind="tool")

    def load(self, max_messages: int = 0) -> list[tuple[str, str]]:
        """Conversation rows of the current epoch, oldest first.

        Only kind='chat' rows return — tool rows are recorded but not yet fed
        back into the context window."""
        if not self.available:
            return []
        try:
            with self._connect() as conn:
                sql = (
                    "SELECT role, content FROM messages "
                    "WHERE epoch=? AND kind='chat' ORDER BY id"
                )
                rows = conn.execute(sql, (self.epoch(),)).fetchall()
        except (sqlite3.Error, OSError):
            return []
        if max_messages > 0:
            rows = rows[-max_messages:]
        return [(str(r), str(c)) for r, c in rows]

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE epoch=? AND kind='chat'", (self.epoch(),)
                ).fetchone()
            return int(row[0]) if row else 0
        except (sqlite3.Error, OSError):
            return 0
