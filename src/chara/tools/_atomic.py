"""Atomic text write shared by the small per-chara JSON stores (polaris, task) so
their persistence can't drift: write a temp sibling, fsync, then os.replace
(atomic on POSIX) — a crash mid-write leaves the old file intact, never a
half-written one. Best-effort callers wrap the call in ``try/except OSError``;
this helper does the crash-safe write and cleans up its temp on any failure.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f".{path.stem}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
