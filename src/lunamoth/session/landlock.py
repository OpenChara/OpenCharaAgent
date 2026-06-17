"""Landlock LSM filesystem confinement (Linux ≥ 5.13), stdlib-only via ctypes.

The no-userns fallback tier of the ``sandbox`` isolation ladder (see
``session/isolation.py``) — e.g. inside a hardened deploy container.
``bwrap`` needs a user namespace, which a hardened container forbids
(Docker seccomp + no-new-privileges → ``unshare(CLONE_NEWUSER)`` = EPERM, even
when the *host* allows it). Landlock instead lets an **unprivileged** process
restrict its OWN filesystem access to an allow-list, then ``exec`` the command —
no namespaces, no root. The restriction is inherited across ``execve`` and by
all descendants, so a ``bash -c`` child (and anything it spawns) is confined.

Confinement contract (matches the bwrap jail as closely as ABI v1 allows):

* **read + execute** on the system paths a shell needs (``/usr /lib /lib64
  /bin /sbin /etc`` + the Python prefix) and the read-only ``assets`` shelf;
* **read + write** on the session ``workspace`` (+ any operator-opted-in paths)
  and ``/dev`` (device nodes — ``/dev/null`` etc., nothing secret);
* **everything else is denied** — notably the chara can no longer read
  ``~/.lunamoth/desktop.json`` (the global key), ``auth.json`` (the login
  hash), other charas' workspaces, or ``/proc/<pid>/environ`` (``/proc`` is
  deliberately NOT granted, so the supervisor's env/token stays unreadable).

Limits: ABI v1 covers the filesystem only — it does NOT gate the network
(``LANDLOCK_ACCESS_NET_*`` arrived in ABI v4 / kernel 6.7). A caller that needs
``/net off`` enforced must layer that elsewhere; under this tier the network is
not restricted. Non-Linux or pre-5.13 kernels: :func:`abi_version` returns 0 and
:func:`available` is False, so the ladder falls through to refuse.
"""
from __future__ import annotations

import ctypes
import os
import sys

# Landlock syscalls live in the arch-generic table — same numbers on x86_64
# and arm64 (the two platforms we target).
_NR_landlock_create_ruleset = 444
_NR_landlock_add_rule = 445
_NR_landlock_restrict_self = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1  # flag: "just report the ABI version"
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# LANDLOCK_ACCESS_FS_* bit positions (uapi/linux/landlock.h).
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3

# Read-only subtree: read files + list dirs + execute binaries.
_RO = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
# A device tree (/dev): read + write existing nodes, no create/remove.
_DEV = _FS_READ_FILE | _FS_WRITE_FILE | _FS_READ_DIR


class _ruleset_attr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _path_beneath_attr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long  # default c_int truncates 64-bit returns/ptrs
    return libc


def _handled_fs(abi: int) -> int:
    """The full filesystem-access mask supported by *abi* (deny-by-default set)."""
    mask = (
        _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_READ_DIR
        | _FS_REMOVE_DIR | _FS_REMOVE_FILE | _FS_MAKE_CHAR | _FS_MAKE_DIR
        | _FS_MAKE_REG | _FS_MAKE_SOCK | _FS_MAKE_FIFO | _FS_MAKE_BLOCK | _FS_MAKE_SYM
    )
    if abi >= 2:
        mask |= _FS_REFER
    if abi >= 3:
        mask |= _FS_TRUNCATE
    return mask


def abi_version() -> int:
    """Landlock ABI version supported by the running kernel, or 0 if unavailable."""
    if sys.platform != "linux":
        return 0
    try:
        libc = _libc()
        v = libc.syscall(
            ctypes.c_long(_NR_landlock_create_ruleset),
            ctypes.c_void_p(0),
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
        return int(v) if v and int(v) > 0 else 0
    except Exception:
        return 0


def available() -> bool:
    return abi_version() >= 1


class LandlockError(RuntimeError):
    """Landlock restriction could not be applied — the caller must NOT proceed
    to run the command unconfined (that would be a silent jail escape)."""


def restrict(ro_paths: list[str], rw_paths: list[str], dev_paths: list[str] | None = None) -> None:
    """Apply a Landlock filesystem allow-list to the CURRENT process (inherited
    across exec). Read/execute on *ro_paths*, read/write on *rw_paths*, device
    access on *dev_paths* (default ``["/dev"]``); everything else denied.

    Raises :class:`LandlockError` on any failure — never returns having applied
    only a partial restriction the caller might mistake for success.
    """
    abi = abi_version()
    if abi < 1:
        raise LandlockError("Landlock is not available on this kernel")
    if dev_paths is None:
        dev_paths = ["/dev"]
    libc = _libc()
    handled = _handled_fs(abi)

    attr = _ruleset_attr(handled_access_fs=handled, handled_access_net=0)
    ruleset_fd = libc.syscall(
        ctypes.c_long(_NR_landlock_create_ruleset),
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise LandlockError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")

    try:
        grants = (
            [(p, _RO) for p in ro_paths]
            + [(p, handled) for p in rw_paths]   # full access inside the workspace
            + [(p, _DEV & handled) for p in dev_paths]
        )
        for path, access in grants:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue  # like bwrap's --ro-bind-try: skip paths that don't exist
            try:
                pb = _path_beneath_attr(allowed_access=access & handled, parent_fd=fd)
                rc = libc.syscall(
                    ctypes.c_long(_NR_landlock_add_rule),
                    ctypes.c_int(ruleset_fd),
                    ctypes.c_uint32(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(pb),
                    ctypes.c_uint32(0),
                )
                if rc != 0:
                    raise LandlockError(
                        f"landlock_add_rule({path}) failed: {os.strerror(ctypes.get_errno())}"
                    )
            finally:
                os.close(fd)

        # restrict_self requires no-new-privileges first.
        if libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1),
                      ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            raise LandlockError(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
        rc = libc.syscall(
            ctypes.c_long(_NR_landlock_restrict_self),
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        )
        if rc != 0:
            raise LandlockError(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset_fd)


def _main(argv: list[str]) -> int:
    """``python -m lunamoth.session.landlock --ro P ... --rw P ... -- cmd ...``

    Applies the allow-list to this process, then execs the command (so the
    restriction is inherited). On any Landlock failure it exits non-zero WITHOUT
    exec — the jail must never be silently skipped.
    """
    ro: list[str] = []
    rw: list[str] = []
    i, n = 0, len(argv)
    cmd: list[str] = []
    while i < n:
        a = argv[i]
        if a == "--ro" and i + 1 < n:
            ro.append(argv[i + 1]); i += 2
        elif a == "--rw" and i + 1 < n:
            rw.append(argv[i + 1]); i += 2
        elif a == "--":
            cmd = argv[i + 1:]; break
        else:
            i += 1
    if not cmd:
        sys.stderr.write("landlock: no command after --\n")
        return 2
    try:
        restrict(ro, rw)
    except LandlockError as e:
        sys.stderr.write(f"landlock: refusing to run unconfined: {e}\n")
        return 3
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        sys.stderr.write(f"landlock: exec {cmd[0]!r} failed: {e}\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
