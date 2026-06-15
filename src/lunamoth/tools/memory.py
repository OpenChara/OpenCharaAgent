"""Hermes-identical durable memory: two §-delimited stores the chara curates.

Apple-to-apple port of hermes-agent ``tools/memory_tool.py``, with ONE
intentional divergence: hermes resolves a single global ``~/.hermes/memories/``
home; LunaMoth is one-process-one-chara, so storage stays PER-CHARA under the
sandbox (``<root>/MEMORY.md`` and ``<root>/USER.md``). Everything else — the
schema, char budgets, the ``.lock`` sidecar flock + mkstemp→write→fsync→replace
durability, the external-edit drift guard with ``.bak.<ts>``, the over-limit
CONSOLIDATE (never truncate) behavior, and the strict threat scan at write and
load — mirrors hermes byte-for-byte.

Two stores (hermes MEMORY.md / USER.md):
  - "memory" — the chara's own notes: environment facts, conventions, lessons.
  - "user"   — durable facts ABOUT the operator (who they are, preferences).

Prompt-cache discipline: the agent loads a FROZEN snapshot once at session start
(``_system_prompt_snapshot`` / ``snapshot()``) and injects THAT — mid-session
writes hit disk + the tool response but never the cached prompt prefix. The
snapshot refreshes on the next session start / reconfigure / reset. See
agent._freeze_memory.

Public API the agent + chara-life tools rely on (kept stable):
  MemoryStore(root, limits) · snapshot() · set_limits(MemoryLimits) · entries() ·
  usage() · chars() · render() · is_empty() · .limits
"""
from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .builtin._threat_patterns import first_threat_message, scan_for_threats

# fcntl is Unix-only; on Windows fall back to msvcrt (LunaMoth is mac/linux, but
# the no-op-if-neither branch keeps the port faithful and portable).
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - non-unix
    fcntl = None
    try:
        import msvcrt  # type: ignore
    except ImportError:
        pass

ENTRY_DELIM = "\n§\n"
ENTRY_DELIMITER = ENTRY_DELIM  # hermes spelling, kept as an alias
TARGETS = ("memory", "user")


@dataclass(frozen=True)
class MemoryLimits:
    # Hermes defaults (model-independent CHAR counts): 2200 / 1375.
    memory_chars: int = 2200
    user_chars: int = 1375

    def cap(self, target: str) -> int:
        return self.user_chars if target == "user" else self.memory_chars


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Error string if blocked."""
    return first_threat_message(content, scope="strict")


def _drift_error(path: Path, bak_path: str) -> Dict[str, Any]:
    """The error dict returned when external drift is detected (hermes :83-110)."""
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


class MemoryStore:
    """Bounded curated memory with file persistence. One instance per agent.

    Maintains two parallel states:
      - ``_system_prompt_snapshot``: frozen at load time, injected into the system
        prompt. Never mutated mid-session — keeps the prefix cache stable.
      - ``memory_entries`` / ``user_entries``: live state, mutated by tool calls
        and persisted to disk. Tool responses always reflect this live state.

    LunaMoth divergence: the storage directory is per-chara (passed in), not a
    global home, and filenames are UPPER-case ``MEMORY.md`` / ``USER.md``.
    """

    def __init__(self, root: Path, limits: MemoryLimits | None = None):
        self.root = Path(root)
        self.limits = limits or MemoryLimits()
        self.root.mkdir(parents=True, exist_ok=True)
        self._migrate_lowercase_filenames()
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        self.load_from_disk()

    # ---- directory + paths ----------------------------------------------------

    def _migrate_lowercase_filenames(self) -> None:
        """One-time migration of the pre-hermes lower-case ``memory.md`` /
        ``user.md`` to hermes UPPER-case ``MEMORY.md`` / ``USER.md`` (content is
        already §-delimited and round-trip clean). Skips if the upper-case file
        already exists; best-effort (a failed rename never breaks startup)."""
        for lower, upper in (("memory.md", "MEMORY.md"), ("user.md", "USER.md")):
            old = self.root / lower
            new = self.root / upper
            # On a case-insensitive filesystem old and new resolve to the same
            # inode — nothing to do, and a rename would be a no-op/error.
            try:
                if old.exists() and not new.exists() and old.resolve() != new.resolve():
                    os.replace(old, new)
            except OSError:
                pass

    def _memory_dir(self) -> Path:
        # Resolved per call (hermes resolves dynamically); for LunaMoth it is the
        # per-chara root the agent passed in.
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _path_for(self, target: str) -> Path:
        if target not in TARGETS:
            raise ValueError(f"target must be one of {TARGETS}")
        mem_dir = self._memory_dir()
        return mem_dir / ("USER.md" if target == "user" else "MEMORY.md")

    # Back-compat alias for the old lower-case ``_path`` callers/tests.
    def _path(self, target: str) -> Path:
        return self._path_for(target)

    # ---- load + frozen snapshot -----------------------------------------------

    def load_from_disk(self) -> None:
        """Load entries from MEMORY.md/USER.md and capture the system-prompt snapshot.

        Each entry is scanned at snapshot-build time; ANY threat hit replaces the
        entry text IN THE SNAPSHOT ONLY with a ``[BLOCKED: …]`` placeholder. The
        live lists keep the raw text so the user can still read + remove it.
        """
        self._memory_dir()
        self.memory_entries = list(dict.fromkeys(self._read_file(self._path_for("memory"))))
        self.user_entries = list(dict.fromkeys(self._read_file(self._path_for("user"))))

        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """Replace any threat-matching entry with a ``[BLOCKED: …]`` placeholder."""
        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=read) to inspect and memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    # ---- locking --------------------------------------------------------------

    @contextmanager
    def _file_lock(self, path: Path):
        """Exclusive lock on a sidecar ``<file>.lock`` for read-modify-write safety.

        The lock is on the sidecar, not the data file, so the data file can still
        be atomically ``os.replace``d underneath it.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:  # pragma: no cover
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:  # pragma: no cover - windows
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            elif msvcrt:  # pragma: no cover - windows
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            fd.close()

    # ---- entry accessors ------------------------------------------------------

    def _entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        return self.limits.cap(target)

    def _reload_target(self, target: str) -> Optional[str]:
        """Re-read entries fresh from disk (under lock). Returns the .bak path on
        external drift (caller must abort the mutation), None on clean reload."""
        bak = self._detect_external_drift(target)
        fresh = list(dict.fromkeys(self._read_file(self._path_for(target))))
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str) -> None:
        self._memory_dir()
        self._write_file(self._path_for(target), self._entries_for(target))

    # ---- mutations (hermes dict-returning) ------------------------------------

    def add(self, target: str, content: str) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        old_text = (old_text or "").strip()
        new_content = (new_content or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            limit = self._char_limit(target)

            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        old_text = (old_text or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    # ---- system-prompt injection ----------------------------------------------

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """The frozen snapshot block for system-prompt injection (None if empty)."""
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        resp = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system-prompt block with the ═-bordered header + usage."""
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    # ---- file IO --------------------------------------------------------------

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries. No lock: writes are atomic
        renames, so a reader always sees a complete old/new file."""
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    def _detect_external_drift(self, target: str) -> Optional[str]:
        """Backup-path string if on-disk content shows external drift, else None.

        Two signals: (1) round-trip mismatch — re-parse+re-serialize doesn't equal
        the bytes on disk; (2) entry-size overflow — a single parsed entry exceeds
        the store's whole-file char limit (no tool-written entry can). On drift the
        file is snapshotted to ``<name>.bak.<ts>``.
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]) -> None:
        """Write entries atomically: mkstemp → write → flush → fsync → os.replace.

        A failed write RAISES (the gateway boundary turns it into an error) — the
        chara must never be told "saved" when nothing landed.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".mem_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}") from e

    # ---- LunaMoth public API the agent + chara-life tools rely on -------------

    def entries(self, target: str) -> list[str]:
        """Live entries for one store (re-read from disk for a fresh view)."""
        return self._read_file(self._path_for(target))

    def chars(self, target: str) -> int:
        return len(ENTRY_DELIMITER.join(self.entries(target)))

    def usage(self, target: str) -> str:
        used = self.chars(target)
        cap = self.limits.cap(target)
        pct = round(100 * used / cap) if cap else 0
        return f"{pct}% — {used}/{cap} chars"

    def set_limits(self, new_limits: "MemoryLimits") -> list[str]:
        """Apply new size limits at runtime. Hermes never silently drops content;
        a shrink that leaves an over-cap store on disk is surfaced as a warning (the
        chara consolidates via the memory tool / the drift guard catches it on the
        next write). Growing is silent. The prompt reflects the change next session
        (the snapshot is frozen — see agent._freeze_memory)."""
        old_limits = self.limits
        self.limits = new_limits
        warnings: list[str] = []
        for target in TARGETS:
            cap = new_limits.cap(target)
            used = self.chars(target)
            if used > cap and cap < old_limits.cap(target):
                warnings.append(
                    f"{target} memory is {used} chars but the new budget is {cap}. "
                    f"Nothing was discarded — consolidate it via the memory tool "
                    f"(replace/remove) so it fits."
                )
        return warnings

    def snapshot(self) -> dict[str, list[str]]:
        """Both stores' current entries — taken once at session start and frozen
        into the system prompt (see agent._freeze_memory)."""
        return {t: self.entries(t) for t in TARGETS}

    def is_empty(self) -> bool:
        return not any(self.entries(t) for t in TARGETS)

    def render(self) -> str:
        """A plain combined view of both stores (for /memory and the sidebar)."""
        out: list[str] = []
        for label, target in (("MEMORY", "memory"), ("USER", "user")):
            entries = self.entries(target)
            if entries:
                out.append(f"[{label}]  ({self.usage(target)})")
                out.extend(f"  · {e}" for e in entries)
        return "\n".join(out)
