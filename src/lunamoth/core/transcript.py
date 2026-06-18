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
from typing import Any

# Markers of WAL-incompatible filesystems (from hermes-agent, MIT).
_WAL_INCOMPAT_MARKERS = ("locking protocol", "not authorized", "disk i/o error")


def _strip_inline_images(msg: dict) -> dict:
    """Return a copy of a context message with inline base64 image data URLs dropped
    (the accompanying text note is the handle that round-trips). Images ride FULL
    size in the in-memory context for the model to see, but bytes in the durable
    transcript are exactly what to avoid (commit 79eac31, "never persist bytes" —
    already applied to the read_file re-view; this extends it to the upload path).
    On reload an old image is a text handle anyway (compaction.strip_old_images keeps
    only the newest image's pixels live). Remote http(s) image URLs are tiny and
    kept. Never mutates the input; returns it unchanged when there is nothing to
    strip."""
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    kept: list = []
    changed = False
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            iv = part.get("image_url")
            url = iv.get("url", "") if isinstance(iv, dict) else (iv if isinstance(iv, str) else "")
            if isinstance(url, str) and url.startswith("data:"):
                changed = True
                continue  # drop the pixels; the text part carries the handle
        kept.append(part)
    if not changed:
        return msg
    has_text = any(isinstance(p, dict) and p.get("type") == "text"
                   and str(p.get("text") or "").strip() for p in kept)
    if not has_text:
        kept.append({"type": "text", "text": "[image attached]"})
    out = dict(msg)
    out["content"] = kept
    return out


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
        except (sqlite3.Error, OSError) as e:
            self.available = False
            from ..obs import get_logger

            get_logger("transcript").error(
                "transcript db unavailable at %s (%s) — conversation will NOT survive restarts", path, e
            )

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
    # Row kinds:
    #   chat    plain prose line; content is the text
    #   think   idle self-talk monologue; content is the text
    #   struct  full message dict serialized as JSON in content (assistant
    #           tool_calls, tool results, reasoning_content — hermes-style)
    #   tool    legacy forensic rows from older builds; never reloaded

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

    def append_message(self, msg: dict) -> None:
        """Persist one context message dict (the ContextBuffer persist hook)."""
        role = str(msg.get("role", ""))
        # A multimodal user message carries `content` as a list of parts; persist
        # it as JSON (the struct path) so it reloads intact instead of being
        # str()-ed into a broken "[{'type': ...}]" line.
        structured = isinstance(msg.get("content"), list) or any(
            k in msg for k in ("tool_calls", "tool_call_id", "reasoning_content", "reasoning_details", "name")
        )
        if msg.get("kind") == "summary":
            self.append(role, str(msg.get("content") or ""), kind="summary")
            return
        if structured:
            try:
                # Strip inline image bytes before persisting — full-size pixels live
                # only in the in-memory context, never in the durable transcript.
                self.append(role, json.dumps(_strip_inline_images(msg), ensure_ascii=False), kind="struct")
            except (TypeError, ValueError):
                self.append(role, str(msg.get("content") or ""), kind="chat")
            return
        # Self-work and chat assistant turns are recorded uniformly as "chat":
        # no per-message classification of the chara's own output (hermes-faithful).
        # ("summary"/"struct" above are structural infra, not output classes.)
        self.append(role, str(msg.get("content") or ""), kind="chat")

    def load(self, max_messages: int = 0) -> list[dict]:
        """Conversation messages of the current epoch, oldest first, as dicts."""
        if not self.available:
            return []
        try:
            with self._connect() as conn:
                epoch = self.epoch()
                row = conn.execute(
                    "SELECT MAX(id) FROM messages WHERE epoch=? AND kind='summary'",
                    (epoch,),
                ).fetchone()
                summary_id = int(row[0]) if row and row[0] else None
                sql = (
                    "SELECT role, content, kind FROM messages "
                    "WHERE epoch=? AND kind IN ('chat','think','struct','summary')"
                )
                params: tuple[int, ...] | tuple[int, int] = (epoch,)
                if summary_id is not None:
                    sql += " AND id>=?"
                    params = (epoch, summary_id)
                sql += " ORDER BY id"
                rows = conn.execute(sql, params).fetchall()
        except (sqlite3.Error, OSError):
            return []
        if max_messages > 0:
            if rows and rows[0][2] == "summary" and len(rows) > max_messages:
                rows = [rows[0]] if max_messages <= 1 else [rows[0]] + rows[-(max_messages - 1):]
            else:
                rows = rows[-max_messages:]
        out: list[dict] = []
        for role, content, kind in rows:
            if kind == "struct":
                try:
                    msg = json.loads(content)
                    if isinstance(msg, dict) and msg.get("role"):
                        out.append(msg)
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                out.append({"role": str(role), "content": str(content)})
            elif kind == "summary":
                out.append({"role": str(role), "content": str(content), "kind": "summary"})
            else:
                # Plain chat AND legacy "think" rows load as ordinary messages.
                out.append({"role": str(role), "content": str(content)})
        return out

    def load_display(self, max_messages: int = 0) -> list[dict]:
        """Like load(), but ALSO carries legacy kind='tool' forensic rows so a
        frontend can show the full recent history (tool calls + results +
        reasoning). The MODEL never sees this view — it is display-only, so the
        model's replayed context (load() → context.render()) is unchanged."""
        if not self.available:
            return []
        try:
            with self._connect() as conn:
                epoch = self.epoch()
                row = conn.execute(
                    "SELECT MAX(id) FROM messages WHERE epoch=? AND kind='summary'",
                    (epoch,),
                ).fetchone()
                summary_id = int(row[0]) if row and row[0] else None
                sql = (
                    "SELECT role, content, kind FROM messages "
                    "WHERE epoch=? AND kind IN ('chat','think','struct','summary','tool')"
                )
                params: tuple[int, ...] | tuple[int, int] = (epoch,)
                if summary_id is not None:
                    sql += " AND id>=?"
                    params = (epoch, summary_id)
                sql += " ORDER BY id"
                rows = conn.execute(sql, params).fetchall()
        except (sqlite3.Error, OSError):
            return []
        if max_messages > 0:
            if rows and rows[0][2] == "summary" and len(rows) > max_messages:
                rows = [rows[0]] if max_messages <= 1 else [rows[0]] + rows[-(max_messages - 1):]
            else:
                rows = rows[-max_messages:]
        out: list[dict] = []
        for role, content, kind in rows:
            if kind in ("struct", "tool"):
                try:
                    msg = json.loads(content)
                    if isinstance(msg, dict) and msg.get("role"):
                        out.append(msg)
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
                out.append({"role": str(role), "content": str(content)})
            elif kind == "summary":
                out.append({"role": str(role), "content": str(content), "kind": "summary"})
            else:
                # Plain chat AND legacy "think" rows render as ordinary messages.
                out.append({"role": str(role), "content": str(content)})
        return out

    def export_jsonl(self, path: Path) -> int:
        """Hermes-style complete conversation export of the CURRENT epoch.

        EVERY row (chat, think, struct, tool, summary) becomes one JSON object
        per line, oldest first — struct/tool rows expanded back to their full
        message dict (tool_calls / tool_call_id / reasoning_content / content).
        Opens the DB read-only, so it works while the chara is stopped. Returns
        the number of lines written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            # No DB yet → an empty export, not a fabricated one.
            path.write_text("", encoding="utf-8")
            return 0
        try:
            try:
                epoch_row = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
                epoch = int(epoch_row[0]) if epoch_row and epoch_row[0] else 0
            except (sqlite3.Error, ValueError):
                epoch = 0
            rows = conn.execute(
                "SELECT id, ts, role, content, kind FROM messages WHERE epoch=? ORDER BY id",
                (epoch,),
            ).fetchall()
        finally:
            conn.close()
        with path.open("w", encoding="utf-8") as fh:
            for row_id, ts, role, content, kind in rows:
                obj: dict[str, Any] = {"id": int(row_id), "ts": float(ts or 0.0),
                                       "role": str(role), "kind": str(kind)}
                if kind in ("struct", "tool"):
                    try:
                        msg = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        msg = None
                    if isinstance(msg, dict):
                        # Expand the full message dict; id/ts/kind from the row win.
                        for k, v in msg.items():
                            obj.setdefault(k, v)
                        obj["role"] = str(msg.get("role") or role)
                    else:
                        obj["content"] = str(content)
                else:
                    obj["content"] = str(content)
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                written += 1
        return written

    def last_timestamp(self) -> float:
        """Wall-clock time of the newest message in the current epoch (0 if none).
        Lets a restarted chara know how long the silence really was."""
        if not self.available:
            return 0.0
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM messages WHERE epoch=?", (self.epoch(),)
                ).fetchone()
            return float(row[0]) if row and row[0] else 0.0
        except (sqlite3.Error, OSError):
            return 0.0

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE epoch=? AND kind IN ('chat','think','struct','summary')",
                    (self.epoch(),),
                ).fetchone()
            return int(row[0]) if row else 0
        except (sqlite3.Error, OSError):
            return 0
