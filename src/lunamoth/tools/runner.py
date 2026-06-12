"""Run shell commands for the agent's `terminal` tool — Hermes/Claude-Code style.

The agent is given ONE language-agnostic capability: run a shell command in its
session workspace. Isolation is provided by the OS, not by intercepting a
specific interpreter, so there is no Python-only guard and no language lock-in.

Three isolation mechanisms (chosen per session, see `sessions.py`):

    dir      no jail — the command runs with your user's full privileges, cwd in
             the workspace (Claude-Code-style "I trust this directory"). Network
             always available.
    sandbox  OS jail: sandbox-exec (macOS) / bubblewrap (Linux). Writes confined
             to the workspace (+ any allow-listed paths); network gated by the
             runtime `allow_network` permission. The default.
    docker   container: read-only rootfs, bind-mounted workspace, network gated.

Permissions (allow_network, writable_paths) are read fresh on every call, so the
operator can flip them mid-session (TUI `/net on`, `/allow-dir`) without restart.

The jail builders themselves live in `session/isolation.py` (stdlib-only) so the
supervisor's PTY shell can share them without importing tools/.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..obs import get_logger
from ..session.isolation import (  # noqa: F401 — backend/os_sandbox_available are this module's public API
    _base_env,
    _docker,
    _linux_jail,
    _macos_jail,
    backend,
    os_sandbox_available,
)

_log = get_logger("runner")

DEFAULT_TIMEOUT = 30
_OUTPUT_CAP = 12000


def run_terminal(
    command: str,
    workspace: Path,
    *,
    isolation: str | None = None,
    allow_network: bool = False,
    writable_paths: "list[str] | tuple[str, ...]" = (),
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str | None = None,
    image: str = "python:3.11-slim",
    memory_mb: int = 2048,
    cpus: float = 2,
) -> str:
    """Execute *command* in a shell under the active isolation mechanism."""
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolation = (isolation or backend()).lower()
    writable = [Path(p).resolve() for p in writable_paths]
    cwd = workspace
    if workdir:
        cand = (workspace / workdir).resolve() if not os.path.isabs(workdir) else Path(workdir).resolve()
        if isolation == "dir" or cand == workspace or workspace in cand.parents or cand in writable:
            cwd = cand

    note = ""
    if isolation == "docker" and shutil.which("docker"):
        cmd: list[str] = _docker(command, workspace, allow_network, image, memory_mb, cpus)
        run_cwd = None
    elif isolation == "sandbox" and os_sandbox_available():
        cmd = (_macos_jail if sys.platform == "darwin" else _linux_jail)(command, workspace, allow_network, writable)
        run_cwd = str(cwd) if sys.platform == "darwin" else None  # bwrap sets its own chdir
    else:
        if isolation != "dir":
            note = f"\n[lunamoth: '{isolation}' jail unavailable, ran with directory trust]"
        cmd = ["/bin/bash", "-c", command]
        run_cwd = str(cwd)

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=run_cwd,
            env=_base_env(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,  # own process group so timeout kills children
        )
    except subprocess.TimeoutExpired:
        _log.warning("terminal command timed out after %ds (%s): %.120s", timeout, isolation, command)
        return f"[timed out after {timeout}s]{note}"
    except FileNotFoundError as e:
        _log.error("terminal runner unavailable (%s): %s", isolation, e)
        return f"[runner error: {e}]{note}"
    _log.info("terminal (%s, net=%s) exit=%d in %.1fs: %.120s",
              isolation, "on" if allow_network else "off", proc.returncode, time.monotonic() - t0, command)

    out = (proc.stdout or "")[-_OUTPUT_CAP:]
    err = (proc.stderr or "")[-2000:]
    parts = [f"exit={proc.returncode}"]
    if out:
        parts.append(f"STDOUT:\n{out}")
    if err:
        parts.append(f"STDERR:\n{err}")
    return ("\n".join(parts) + note).strip()
