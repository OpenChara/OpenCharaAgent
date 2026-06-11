from __future__ import annotations

import shutil

from .config import SANDBOX_ROOT


def clean_runtime_sandbox(clear_memory: bool = True) -> None:
    """Clean volatile runtime sandbox artifacts.

    This is intentionally conservative: it removes logs and transient workspace
    files, and optionally clears the durable memory dir. Static files in
    sandbox/files and env_status.json are preserved.
    """
    logs = SANDBOX_ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for p in logs.iterdir():
        if p.name == ".gitkeep":
            continue
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    (logs / ".gitkeep").touch()

    workspace = SANDBOX_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for p in workspace.iterdir():
        if p.name == ".gitkeep":
            continue
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    (workspace / ".gitkeep").touch()
    if clear_memory:
        # Durable memory dir (memory.md / user.md).
        shutil.rmtree(SANDBOX_ROOT / "memory", ignore_errors=True)
        # The durable transcript counts as memory: a clean exit zeroes it too
        # (WAL/SHM sidecars included).
        for suffix in ("", "-wal", "-shm"):
            (SANDBOX_ROOT / f"transcript.db{suffix}").unlink(missing_ok=True)
        (SANDBOX_ROOT / "goals.json").unlink(missing_ok=True)
