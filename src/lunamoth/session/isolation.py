"""OS jail builders — the one place isolation mechanics live.

Shared by two callers with the same security contract:

* ``tools/runner.py`` — the agent's `terminal` tool (one-shot ``bash -c``).
* ``server/pty.py`` + the supervisor's ``/chara/<name>/pty`` endpoint — an
  interactive operator shell streamed to a browser terminal.

This module is deliberately **stdlib-only** (``session/`` imports only
config/stdlib) so the supervisor can build jails without touching ``core/``
or ``tools/``.

Three isolation mechanisms (chosen per session, see ``sessions.py``):

    dir      no jail — full user privileges, cwd in the workspace.
    sandbox  OS jail: sandbox-exec (macOS) / bubblewrap (Linux). Writes
             confined to the workspace (+ allow-listed paths); network gated.
    docker   container: read-only rootfs, bind-mounted workspace, network gated.

Permissions snapshot semantics differ by caller: ``run_terminal`` re-reads
allow_network / writable paths on EVERY call, so a mid-session ``/net on``
applies to the next command. ``interactive_shell_argv`` bakes them into the
jail ONCE at spawn — an already-open interactive shell keeps the permissions
it was born with; re-open the shell to pick up changes.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

# Don't hand the agent our own provider/credentials through the environment.
# Applies to EVERY child we spawn — one-shot commands and interactive shells.
_ENV_BLOCKLIST = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN", "LLM_PROVIDER",
)


class JailUnavailableError(RuntimeError):
    """The requested OS jail cannot be provided on this host.

    Interactive shells must FAIL VISIBLY on this — an operator shell that
    silently escapes its jail is a security lie. (The one-shot terminal tool
    instead degrades with an explicit note; that behavior lives in runner.py.)
    """


def backend() -> str:
    """Isolation mechanism for this session (LUNAMOTH_PY_BACKEND: dir|sandbox|docker)."""
    raw = os.environ.get("LUNAMOTH_PY_BACKEND", os.environ.get("LUNAMOSS_PY_BACKEND", "sandbox")).strip().lower()
    return "dir" if raw in {"dir", "local"} else raw


def os_sandbox_available() -> bool:
    if sys.platform == "darwin":
        return bool(shutil.which("sandbox-exec"))
    if sys.platform == "linux":
        return bool(shutil.which("bwrap"))
    return False


def _base_env(workspace: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_BLOCKLIST}
    env["TMPDIR"] = str(workspace)  # keep temp files inside the writable jail
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin")
    return env


def _macos_profile(workspace: Path, allow_network: bool, writable: list[Path], *, interactive: bool = False) -> str:
    writes = "\n".join(f'(allow file-write* (subpath "{p}"))' for p in [workspace, *writable])
    net = "(allow network*)" if allow_network else "(deny network*)"
    # An interactive shell sits on a pty SLAVE (/dev/ttysNN on macOS): job
    # control (TIOCSPGRP/TIOCGWINSZ) and termios need ioctl plus read/write on
    # that device node — the base profile only opens /dev/tty.
    tty = (
        '(allow file-ioctl (regex #"^/dev/ttys[0-9]+$"))\n'
        '(allow file-read* (regex #"^/dev/ttys[0-9]+$"))\n'
        '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))\n'
    ) if interactive else ""
    return f'''
(version 1)
(deny default)
(allow process*)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-ioctl (literal "/dev/dtracehelper") (literal "/dev/tty"))
{writes}
(allow file-write* (literal "/dev/null") (literal "/dev/tty") (literal "/dev/stdout") (literal "/dev/stderr"))
{net}
{tty}'''


def _macos_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path]) -> list[str]:
    return ["sandbox-exec", "-p", _macos_profile(workspace, allow_network, writable), "/bin/bash", "-c", command]


def _linux_jail_argv(inner: list[str], workspace: Path, allow_network: bool, writable: list[Path]) -> list[str]:
    ws = str(workspace)
    cmd = ["bwrap", "--die-with-parent", "--unshare-all"]
    if allow_network:
        cmd += ["--share-net", "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf"]
    cmd += ["--proc", "/proc", "--dev", "/dev"]  # --dev provides devpts for interactive shells
    for ro in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", sys.prefix):
        cmd += ["--ro-bind-try", ro, ro]
    cmd += ["--bind", ws, ws]
    # The read-only reference shelf is a SIBLING of the workspace (sandbox/assets).
    # Bind it read-only so a shelled rg/grep/cat can READ assets, never write them
    # (macOS's profile already allows reads globally; this gives bwrap parity).
    assets = workspace.parent / "assets"
    if assets.is_dir():
        cmd += ["--ro-bind-try", str(assets), str(assets)]
    for p in writable:
        cmd += ["--bind", str(p), str(p)]
    cmd += ["--chdir", ws, *inner]
    return cmd


def _linux_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path]) -> list[str]:
    return _linux_jail_argv(["/bin/bash", "-c", command], workspace, allow_network, writable)


def _docker_argv(inner: list[str], workspace: Path, allow_network: bool, image: str,
                 memory_mb: int, cpus: float, *, tty: bool = False) -> list[str]:
    # NOTE: disk isn't hard-capped here — the container is read-only except the
    # bind-mounted workspace (host disk). A hard quota needs --storage-opt, which
    # only some drivers accept; we skip it rather than risk failing to launch.
    return [
        "docker", "run", "--rm", "-it" if tty else "-i",
        "--network", "bridge" if allow_network else "none",
        "--memory", f"{memory_mb}m", "--cpus", str(cpus), "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=256m",
        "-v", f"{workspace}:/workspace:rw", "-w", "/workspace",
        image, *inner,
    ]


def _docker(command: str, workspace: Path, allow_network: bool, image: str, memory_mb: int, cpus: float) -> list[str]:
    return _docker_argv(["sh", "-c", command], workspace, allow_network, image, memory_mb, cpus)


def interactive_shell_argv(
    isolation: str,
    workspace: Path,
    *,
    allow_network: bool = False,
    writable_paths: Sequence[str] = (),
    image: str = "python:3.11-slim",
    memory_mb: int = 2048,
    cpus: float = 2,
) -> tuple[list[str], str | None, dict[str, str]]:
    """argv/cwd/env for an INTERACTIVE shell under the session's jail.

    Returns ``(argv, cwd, env)`` ready for a pty spawn. Raises
    :class:`JailUnavailableError` when the requested jail cannot be provided —
    NEVER degrades to directory trust (see the class docstring).

    allow_network / writable paths are read once by the caller and baked in
    here at spawn; a mid-session ``/net on`` does not affect an already-open
    shell (module docstring).
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolation = (isolation or backend()).lower()
    if isolation == "local":
        isolation = "dir"
    writable = [Path(p).resolve() for p in writable_paths]
    env = _base_env(workspace)

    if isolation == "dir":
        return ["/bin/bash", "-i"], str(workspace), env
    if isolation == "docker":
        if not shutil.which("docker"):
            raise JailUnavailableError("docker isolation requested but the `docker` CLI is not available")
        # The PTY wraps the docker CLIENT; -t gives the container its inner tty.
        argv = _docker_argv(["sh"], workspace, allow_network, image, memory_mb, cpus, tty=True)
        return argv, str(workspace), env
    if isolation == "sandbox":
        if not os_sandbox_available():
            tool = "sandbox-exec" if sys.platform == "darwin" else "bwrap"
            raise JailUnavailableError(f"sandbox isolation requested but {tool} is not available on this host")
        if sys.platform == "darwin":
            profile = _macos_profile(workspace, allow_network, writable, interactive=True)
            return ["sandbox-exec", "-p", profile, "/bin/bash", "-i"], str(workspace), env
        return _linux_jail_argv(["/bin/bash", "-i"], workspace, allow_network, writable), str(workspace), env
    raise JailUnavailableError(f"unknown isolation mechanism {isolation!r}")


def runtime_permissions(sandbox_dir: Path) -> tuple[bool, list[str]]:
    """allow_network + writable paths exactly as activation persisted them.

    Reads the session's ``env_status.json`` (written by the runtime's EnvState;
    ``/net on`` and ``/allow-dir`` mutate it). Missing/garbled file = the
    defaults: network off, no extra writable paths.
    """
    try:
        data = json.loads((sandbox_dir / "env_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, []
    if not isinstance(data, dict):
        return False, []
    paths = data.get("writable_paths")
    writable = [str(p) for p in paths] if isinstance(paths, list) else []
    return bool(data.get("network_access", False)), writable
