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
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

# Warn the operator at most once per process when /net off can't actually be
# enforced under the Landlock tier (below) — the note alone only reaches the model.
_LANDLOCK_NETOFF_WARNED = False

# Don't hand the agent our own provider/credentials through the environment.
# Applies to EVERY child we spawn — one-shot commands and interactive shells.
# This is a NAME-PATTERN denylist, not a fixed 6-name list: any var whose name
# looks like a secret (API keys, tokens, passwords, cloud creds — including OUR
# OWN OpenRouter/Ark/DashScope keys and AWS_*/HF_* the old list missed) is
# stripped, plus the explicit provider vars we set ourselves. (A strict allowlist
# would be tighter but routinely breaks programs needing locale/proxy/XDG env; a
# pattern denylist removes the leak with no functional breakage.)
_ENV_EXPLICIT_DENY = frozenset({
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN", "LLM_PROVIDER",
})
_ENV_SECRET_RE = re.compile(
    r"(API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|_KEY$|^AWS_|^HF_)",
    re.IGNORECASE,
)


def _is_secret_env(name: str) -> bool:
    """True if an env var NAME looks like a credential we must not hand the child."""
    return name in _ENV_EXPLICIT_DENY or bool(_ENV_SECRET_RE.search(name))


class JailUnavailableError(RuntimeError):
    """The requested OS jail cannot be provided on this host.

    Interactive shells must FAIL VISIBLY on this — an operator shell that
    silently escapes its jail is a security lie. (The one-shot terminal tool
    instead degrades with an explicit note; that behavior lives in runner.py.)
    """


def force_sandbox() -> bool:
    """Distribution lock: when ``LUNAMOTH_FORCE_SANDBOX`` is set at startup, EVERY chara
    is pinned to the sandbox jail and ``admin`` is refused — so a hosted/shared LunaMoth
    can't be talked (or configured) out of its jail. Read live from the env so the
    supervisor + every chara child (which inherit it) agree."""
    return os.environ.get("LUNAMOTH_FORCE_SANDBOX", "").strip().lower() in {"1", "true", "yes", "on"}


def backend() -> str:
    """Isolation mechanism for this session (LUNAMOTH_PY_BACKEND: sandbox|admin).

    Legacy values (``dir``/``local``/``docker``) normalize to ``admin`` so old
    session configs keep working without migration. When force_sandbox() is on, an
    ``admin`` resolution is clamped to ``sandbox`` HERE — the one authority every tool
    runner + permissions() read — so even a stale env/config can never run unconfined.
    """
    raw = os.environ.get("LUNAMOTH_PY_BACKEND", os.environ.get("LUNAMOSS_PY_BACKEND", "sandbox")).strip().lower()
    b = "admin" if raw in {"admin", "dir", "local", "docker"} else raw
    if b == "admin" and force_sandbox():
        return "sandbox"
    return b


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


def _darwin_user_temp() -> str:
    """The macOS per-user temp dir (``/private/var/folders/<id>/T``). Chromium's
    ProcessSingleton socket dir and agent-browser's Chrome user-data-dir land
    here (NOT in $TMPDIR), so the browser jail must allow writes to it. Resolved
    (``/var`` → ``/private/var``) because Seatbelt matches the real path."""
    return os.path.realpath(tempfile.gettempdir())


def _base_env(workspace: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
    env["TMPDIR"] = str(workspace)  # keep temp files inside the writable jail
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin")
    return env


def _sbpl(path) -> str:
    """A filesystem path as a SAFE Seatbelt string-literal body (what goes between
    the quotes in ``(subpath "…")``). SBPL literals are double-quoted; an unescaped
    ``"`` or ``\\`` — both legal in macOS filenames, and ``writable`` paths come from
    the operator's ``/allow-dir`` — could close the literal and inject profile
    directives (e.g. ``(allow default)``), neutralizing the jail. Escape both; reject
    control chars no legitimate jail target needs."""
    s = str(path)
    if any(c in s for c in ("\n", "\r", "\x00")):
        raise JailUnavailableError(f"path not representable in a sandbox profile: {s!r}")
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _macos_profile(workspace: Path, allow_network: bool, writable: list[Path], *,
                   interactive: bool = False, browser: bool = False) -> str:
    home = _lunamoth_home()
    user_home = Path.home()
    assets = workspace.parent / "assets"
    if browser:
        # A real Chromium needs latitude the deny-default shell profile doesn't
        # grant (iokit-open, posix-shm, extra mach services). Enumerating every
        # Chromium need is fragile, so INVERT the profile: allow by default, then
        # keep ONLY the two properties that actually matter for a jailed chara —
        # (1) writes confined to the workspace (+ the temp dirs the browser
        # scratches in: the per-user Darwin temp holds Chrome's user-data-dir and
        # the ProcessSingleton socket; /private/tmp holds agent-browser's socket),
        # and (2) the secret home (global key / other sessions) unreadable.
        # Verified end-to-end on macOS 2026-06-19 (agent-browser + system Chrome).
        #
        # METADATA traversal: the workspace+assets sit UNDER the denied home, so
        # opening an ABSOLUTE path there (file:///…/.lunamoth/…/workspace/x.html —
        # the chara browsing its own generated files) requires stat/lookup on each
        # ancestor (.lunamoth, sessions, <name>, sandbox). The shell escapes this
        # by running with cwd=workspace (relative access from an open fd); Chrome
        # can't. Re-allow metadata-ONLY on home so traversal works while file
        # CONTENTS stay denied (file-read-data not re-allowed → the key is safe).
        b_writes = "\n".join(
            f'(allow file-write* (subpath "{_sbpl(p)}"))'
            for p in [workspace, *writable, Path(_darwin_user_temp()), Path("/private/tmp")]
        )
        b_net = "" if allow_network else "(deny network*)"  # AF_UNIX (local socket) is unaffected
        # The browser jail is allow-default (Chromium needs broad latitude, and its
        # own binary lives under ~/.agent-browser), so we can't deny all of $HOME
        # like the shell jail does. But the highest-value operator secrets are cheap
        # to deny surgically — they're never anything a browser legitimately reads.
        secret_dirs = "\n".join(
            f'(deny file-read* (subpath "{_sbpl(user_home / d)}"))'
            for d in (".ssh", ".aws", ".gnupg", ".kube", ".config/gcloud", ".docker")
        )
        return f'''
(version 1)
(allow default)
(deny file-read* (subpath "{_sbpl(home)}"))
(allow file-read-metadata (subpath "{_sbpl(home)}"))
{secret_dirs}
(allow file-read* (subpath "{_sbpl(workspace)}"))
(allow file-read* (subpath "{_sbpl(assets)}"))
(deny file-write*)
{b_writes}
(allow file-write* (literal "/dev/null") (literal "/dev/tty") (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/dtracehelper"))
{b_net}'''
    writes = "\n".join(f'(allow file-write* (subpath "{_sbpl(p)}"))' for p in [workspace, *writable])
    net = "(allow network*)" if allow_network else "(deny network*)"
    # Tighten reads: a shell needs system libs/binaries, so keep the broad read
    # allow for /usr,/bin,/System,… — but DENY the user's whole HOME (so
    # ~/.ssh, ~/.aws, ~/.config, shell history, browser cookies are unreadable)
    # AND the LunaMoth home (the global key in desktop.json, the login hash in
    # auth.json, every OTHER chara's session), then re-allow ONLY this chara's
    # own workspace + assets. Previously only ~/.lunamoth was denied, so a chara's
    # terminal/read_file could read the operator's ~/.ssh — the macOS shell jail
    # is now as tight as the Linux bwrap jail, which never exposes $HOME. PATH is
    # system-only (_base_env), so interpreters still resolve under /usr. The
    # operator-opted-in `writable` paths are re-allowed for READ too (they may sit
    # under the denied $HOME; last-match-wins restores parity with Linux, which
    # --binds them read+write — otherwise /allow-dir would be write-but-not-read).
    reads = (
        '(allow file-read*)\n'
        f'(deny file-read* (subpath "{_sbpl(user_home)}"))\n'
        f'(deny file-read* (subpath "{_sbpl(home)}"))\n'
        + "".join(f'(allow file-read* (subpath "{_sbpl(p)}"))\n' for p in [workspace, *writable])
        + f'(allow file-read* (subpath "{_sbpl(assets)}"))'
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


def _macos_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path],
                *, browser: bool = False, interactive: bool = False) -> list[str]:
    return ["sandbox-exec", "-p",
            _macos_profile(workspace, allow_network, writable, browser=browser, interactive=interactive),
            "/bin/bash", "-c", command]


def _linux_jail_argv(inner: list[str], workspace: Path, allow_network: bool, writable: list[Path],
                     *, browser: bool = False) -> list[str]:
    ws = str(workspace)
    if browser:
        # The browser jail, VALIDATED end-to-end on a real Linux host (Ubuntu
        # 22.04, kernel 5.15, bwrap 0.6.1) 2026-06-19. Two hard constraints learned
        # there, both different from the shell jail:
        #  1. NO PID-namespace unshare. agent-browser's Chromium runs as a DETACHED
        #     daemon that must outlive the per-call bwrap (the next browser_* call
        #     reconnects to the live page via the /tmp socket). Under --unshare-pid
        #     the launcher is PID 1, and when it returns the namespace is torn down
        #     and "Chrome exited early without DevToolsActivePort". So we unshare
        #     user/ipc/uts/cgroup but KEEP the host PID namespace (also no
        #     --die-with-parent). Trade-off: host /proc is visible to the browser
        #     process — acceptable because only Chromium runs here (it won't read
        #     /proc/<pid>/environ), the SHELL terminal keeps full --unshare-all, and
        #     the secret home is still hidden below.
        #  2. Chromium + node + agent-browser + its Chrome live all over the host
        #     (/usr, ~/.agent-browser/browsers), so bind the WHOLE root read-only,
        #     then re-confine writes: an empty tmpfs hides the secret home (global
        #     key / other sessions), the workspace/assets are re-exposed over it,
        #     and /tmp is shared rw (agent-browser's socket + Chrome's user-data-dir
        #     land there and must persist + be shared across calls). Chromium also
        #     needs a real /dev/shm (--dev's minimal /dev omits it).
        cmd = ["bwrap", "--unshare-user-try", "--unshare-ipc",
               "--unshare-uts", "--unshare-cgroup-try"]
        if not allow_network:
            cmd += ["--unshare-net"]  # net ON = shared (no unshare); OFF = isolate
        cmd += ["--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm"]
        cmd += ["--tmpfs", str(_lunamoth_home())]          # hide global key + other sessions
        cmd += ["--bind", ws, ws]                           # re-expose this chara's workspace rw
        assets = workspace.parent / "assets"
        if assets.is_dir():
            cmd += ["--ro-bind-try", str(assets), str(assets)]
        cmd += ["--bind", "/tmp", "/tmp"]                   # shared rw temp (socket + profile)
        for p in writable:
            cmd += ["--bind", str(p), str(p)]
        cmd += ["--chdir", ws, *inner]
        return cmd
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


def _linux_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path],
                *, browser: bool = False) -> list[str]:
    return _linux_jail_argv(["/bin/bash", "-c", command], workspace, allow_network, writable, browser=browser)


def _linux_landlock_argv(inner: list[str], workspace: Path, allow_network: bool, writable: list[Path],
                         *, browser: bool = False) -> list[str]:
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
    if browser:
        # Landlock is allow-list only (ABI v1 has no deny-carve-out), so we can't
        # bind "/" ro and subtract the secret like bwrap does. Instead add the
        # specific extra reads Chromium/node/agent-browser need (their install
        # roots) + shared rw /tmp (socket + Chrome profile). ~/.lunamoth is simply
        # never added, so it stays unreadable. VALIDATED end-to-end inside Docker
        # (Landlock tier) 2026-06-19 — Chrome's renderer needs FULL /proc (it
        # opendir's /proc/self/fd + reads /proc/self/maps; --ro is not enough →
        # FATAL proc_util.cc), plus /sys and /dev/shm. Pairs with the crashpad
        # --database shim (see _browser_driver.ensure_crashpad_db_fix).
        home = Path.home()
        for ro in (home / ".agent-browser", home / ".nvm", home / ".cache",
                   home / ".npm-global", "/opt", "/Applications", "/usr/local",
                   os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")):
            if ro and os.path.isdir(str(ro)):
                args += ["--ro", str(ro)]
        args += ["--rw", "/tmp", "--rw", "/proc", "--rw", "/sys", "--rw", "/dev/shm", "--rw", "/dev"]
    assets = workspace.parent / "assets"
    if assets.is_dir():
        args += ["--ro", str(assets)]
    args += ["--rw", ws]
    for p in writable:
        args += ["--rw", str(p)]
    args += ["--", *inner]
    return args


def _linux_landlock_jail(command: str, workspace: Path, allow_network: bool, writable: list[Path],
                         *, browser: bool = False) -> list[str]:
    return _linux_landlock_argv(["/bin/bash", "-c", command], workspace, allow_network, writable, browser=browser)


def build_jail_command(
    command: str,
    workspace: Path,
    isolation: str,
    *,
    allow_network: bool = False,
    writable: Sequence[Path] = (),
    browser: bool = False,
    interactive: bool = False,
) -> tuple[list[str], str | None, str]:
    """The ONE isolation-ladder selector for a non-interactive ``bash -c`` run.

    Shared by ``tools/runner.run_terminal`` (foreground) and
    ``tools/builtin/_process_registry`` (background) so a command is jailed
    IDENTICALLY either way — the security contract can't drift between them.

    Returns ``(cmd, run_cwd, note)``:

      * ``admin`` (and the legacy ``dir``/``local``/``docker`` aliases) → no jail,
        ``run_cwd`` is the workspace (the caller may override it with an explicit
        cwd). The explicit operator opt-out.
      * ``sandbox`` with a native OS jail (bwrap on Linux / sandbox-exec on macOS)
        → the jail argv; ``run_cwd`` is the workspace on macOS, ``None`` on Linux
        (bwrap sets its own ``--chdir``).
      * ``sandbox`` with no native jail but Landlock available (Linux no-userns) →
        the Landlock re-exec argv, ``run_cwd`` the workspace, plus a ``note`` when
        ``allow_network`` is False (ABI v1 can't gate the network).

    Raises :class:`JailUnavailableError` when ``sandbox`` is requested but NEITHER
    a native jail NOR Landlock is available — the command must NOT run unconfined
    (a chara could read the global key in ~/.lunamoth). NEVER degrades to
    directory trust; callers turn the refusal into a visible error.

    ``interactive=True`` is for the PTY path (``run_terminal_pty``): the child
    sits on a pty SLAVE (``/dev/ttysNN`` on macOS), so the macOS Seatbelt
    profile must also allow ioctl/read/write on that device node (the default
    profile only opens ``/dev/tty``). No-op on Linux (bwrap's ``--dev`` provides
    a devpts) and under ``admin``.
    """
    isolation = (isolation or backend()).lower()
    if isolation in {"dir", "local", "docker"}:  # legacy values → admin (no jail)
        isolation = "admin"
    writable_list = list(writable)
    if isolation == "admin":
        return ["/bin/bash", "-c", command], str(workspace), ""
    if isolation == "sandbox":
        # Isolation ladder: native OS jail (bwrap/seatbelt) → Landlock → refuse.
        if os_sandbox_available():
            if sys.platform == "darwin":
                cmd = _macos_jail(command, workspace, allow_network, writable_list,
                                  browser=browser, interactive=interactive)
            else:
                cmd = _linux_jail(command, workspace, allow_network, writable_list, browser=browser)
            run_cwd = str(workspace) if sys.platform == "darwin" else None
            return cmd, run_cwd, ""
        if landlock_available():
            cmd = _linux_landlock_jail(command, workspace, allow_network, writable_list, browser=browser)
            note = ""
            if not allow_network:
                # Honest: Landlock ABI v1 confines the filesystem only — it cannot
                # gate the network, so `/net off` is NOT enforced under this tier.
                note = "\n[lunamoth: Landlock jail — filesystem confined, but network not gated (ABI v1)]"
                # The note above only reaches the MODEL. Surface it to the OPERATOR
                # too (once per process) — they set /net off believing the agent is
                # offline, but under this tier it is still network-capable.
                global _LANDLOCK_NETOFF_WARNED
                if not _LANDLOCK_NETOFF_WARNED:
                    _LANDLOCK_NETOFF_WARNED = True
                    logging.getLogger("lunamoth.isolation").warning(
                        "isolation=sandbox is using the Landlock tier, which (ABI v1) "
                        "cannot gate the network: '/net off' is NOT enforced and this "
                        "chara remains network-capable. Run on a bwrap-capable host to "
                        "enforce network-off."
                    )
            return cmd, str(workspace), note
        raise JailUnavailableError(
            "sandbox isolation requested but no jail is available "
            "(no bwrap user namespaces, no Landlock >=5.13). Not running unconfined. "
            "Install bubblewrap, run on a Landlock-capable kernel, or set isolation=admin "
            "to explicitly opt out of the jail."
        )
    raise JailUnavailableError(f"unknown isolation mechanism {isolation!r}")


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
    ``/net on`` and ``/allow-dir`` mutate it). Missing key / missing / garbled
    file = the DEFAULT_STATUS defaults (core/state.py): network ON — network is
    on by default (owner 2026-06-15) and defaulting False here silently flipped
    a chara's PTY offline while its tools were online — and no extra writable
    paths. (Hardcoded True rather than imported: core imports session, so this
    stdlib-only leaf can't reach back into core/state without a cycle.)
    """
    try:
        data = json.loads((sandbox_dir / "env_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, []
    if not isinstance(data, dict):
        return True, []
    paths = data.get("writable_paths")
    writable = [str(p) for p in paths] if isinstance(paths, list) else []
    return bool(data.get("network_access", True)), writable
