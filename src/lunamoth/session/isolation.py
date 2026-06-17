"""OS jail builders — the one place isolation mechanics live.

Shared by two callers with the same security contract:

* ``tools/runner.py`` — the agent's `terminal` tool (one-shot ``bash -c``).
* ``server/pty.py`` + the supervisor's ``/chara/<name>/pty`` endpoint — an
  interactive operator shell streamed to a browser terminal.

This module is deliberately **stdlib-only** (``session/`` imports only
config/stdlib) so the supervisor can build jails without touching ``core/``
or ``tools/``.

Two isolation mechanisms (chosen per session, see ``sessions.py``):

    admin    no jail — full-machine read/write with your user's privileges, cwd
             in the workspace (for the trusted operator). The renamed legacy
             ``dir``/``local`` mode.
    sandbox  OS jail: sandbox-exec (macOS) / bubblewrap (Linux) / Landlock.
             Writes confined to the workspace (+ allow-listed paths); network
             gated.

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
    """Isolation mechanism for this session (LUNAMOTH_PY_BACKEND: sandbox|admin).

    Legacy values (``dir``/``local``/``docker``) normalize to ``admin`` so old
    session configs keep working without migration.
    """
    raw = os.environ.get("LUNAMOTH_PY_BACKEND", os.environ.get("LUNAMOSS_PY_BACKEND", "sandbox")).strip().lower()
    return "admin" if raw in {"admin", "dir", "local", "docker"} else raw


def os_sandbox_available() -> bool:
    if sys.platform == "darwin":
        return bool(shutil.which("sandbox-exec"))
    if sys.platform == "linux":
        return bool(shutil.which("bwrap"))
    return False


def landlock_available() -> bool:
    """True if the Landlock LSM (≥ ABI 1) can confine a child on this kernel.

    The Linux fallback when bwrap can't run (hardened container: no user
    namespaces). Filesystem-only confinement — see ``session/landlock.py``.
    """
    if sys.platform != "linux":
        return False
    try:
        from . import landlock
        return landlock.available()
    except Exception:
        return False


def _lunamoth_home() -> Path:
    """The dir holding the global key / login hash / all sessions — the thing a
    jailed chara must NOT be able to read (only its own workspace+assets)."""
    return Path(os.environ.get("LUNAMOTH_HOME", str(Path.home() / ".lunamoth"))).expanduser()


def _base_env(workspace: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_BLOCKLIST}
    env["TMPDIR"] = str(workspace)  # keep temp files inside the writable jail
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin")
    return env


def _macos_profile(workspace: Path, allow_network: bool, writable: list[Path], *, interactive: bool = False) -> str:
    writes = "\n".join(f'(allow file-write* (subpath "{p}"))' for p in [workspace, *writable])
    net = "(allow network*)" if allow_network else "(deny network*)"
    # Tighten reads: a shell needs to read system libs/binaries, so we keep the
    # broad read allow — but DENY the LunaMoth home (the global key in
    # desktop.json, the login hash in auth.json, every OTHER chara's session),
    # then re-allow only THIS chara's own workspace + assets shelf (both sit
    # under the home). Parity with the Linux jails, which never expose the home.
    home = _lunamoth_home()
    assets = workspace.parent / "assets"
    reads = (
        '(allow file-read*)\n'
        f'(deny file-read* (subpath "{home}"))\n'
        f'(allow file-read* (subpath "{workspace}"))\n'
        f'(allow file-read* (subpath "{assets}"))'
    )
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
{reads}
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


def _linux_landlock_argv(inner: list[str], workspace: Path, allow_network: bool, writable: list[Path]) -> list[str]:
    """Run *inner* behind a Landlock allow-list — the no-userns Linux tier.

    Same read/write surface as the bwrap jail (system paths + assets read-only,
    workspace + writable read-write), built by re-exec'ing through
    ``python -m lunamoth.session.landlock`` so the restriction is inherited.
    NOTE: ABI v1 can't gate the network — ``allow_network`` is accepted for a
    uniform signature but NOT enforced here (the caller surfaces that).
    """
    ws = str(workspace)
    args = [sys.executable, "-m", "lunamoth.session.landlock"]
    for ro in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", sys.prefix):
        args += ["--ro", ro]
    assets = workspace.parent / "assets"
    if assets.is_dir():
        args += ["--ro", str(assets)]
    args += ["--rw", ws]
    for p in writable:
        args += ["--rw", str(p)]
    args += ["--", *inner]
    return args


def _linux_landlock_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path]) -> list[str]:
    return _linux_landlock_argv(["/bin/bash", "-c", command], workspace, allow_network, writable)


def interactive_shell_argv(
    isolation: str,
    workspace: Path,
    *,
    allow_network: bool = False,
    writable_paths: Sequence[str] = (),
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
    if isolation in {"dir", "local", "docker"}:
        isolation = "admin"
    writable = [Path(p).resolve() for p in writable_paths]
    env = _base_env(workspace)

    if isolation == "admin":
        return ["/bin/bash", "-i"], str(workspace), env
    if isolation == "sandbox":
        if sys.platform == "darwin":
            if not os_sandbox_available():
                raise JailUnavailableError("sandbox isolation requested but sandbox-exec is not available on this host")
            profile = _macos_profile(workspace, allow_network, writable, interactive=True)
            return ["sandbox-exec", "-p", profile, "/bin/bash", "-i"], str(workspace), env
        if os_sandbox_available():  # Linux + bwrap (user namespaces available)
            return _linux_jail_argv(["/bin/bash", "-i"], workspace, allow_network, writable), str(workspace), env
        if landlock_available():    # hardened container (no userns) → Landlock fs confinement
            return _linux_landlock_argv(["/bin/bash", "-i"], workspace, allow_network, writable), str(workspace), env
        raise JailUnavailableError("sandbox isolation requested but neither bwrap nor Landlock is available on this host")
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
