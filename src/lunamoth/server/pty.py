"""PTY bridge: a child process behind a pseudo-terminal, streamed over WS.

Wraps an interactive shell (built by ``session/isolation.py``) behind a pty so
its ANSI output can be streamed to a browser-side terminal emulator and typed
keystrokes fed back in. The only caller is the supervisor's
``/chara/<name>/pty`` WebSocket endpoint.

Design constraints:

* **stdlib only** (``pty``/``fcntl``/``termios``/``select``/``signal``) —
  LunaMoth is macOS/Linux only, so no ``ptyprocess`` dependency.
* **Byte-safe I/O.** Reads and writes go through the pty master fd directly
  and never decode: streaming ANSI is byte-oriented and UTF-8 boundaries land
  mid-read.
* **Not thread-safe by contract.** One bridge is owned by the WebSocket
  handler that spawned it; the reader runs in an executor thread while writes
  happen on the event-loop thread — fine, because the kernel pty is the
  actual synchronization point and we only call ``os.read``/``os.write`` on
  the master fd.

macOS caveat (Darwin 25, observed): an interactive bash that is the session
leader of a pty can hang in exit teardown until the master fd is closed —
SIGKILL alone does not finish it, and ``killpg`` can return EPERM while it is
mid-exit. :meth:`PtyBridge.close` therefore closes the master fd *before* the
final reap and falls back from ``killpg`` to ``kill``.
"""
from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Sequence

__all__ = ["PtyBridge"]

# ``struct winsize`` packs rows/cols as unsigned short (0..65535). We clamp
# well below that ceiling: real terminals never exceed a couple thousand
# columns, and a bigger value is a broken probe (some hosts report
# columns=131072) rather than a genuine ultrawide. Lower bound is 1 — a
# zero/negative dimension is the classic "no size yet" signal.
_MIN_DIMENSION = 1
_MAX_COLS = 2000
_MAX_ROWS = 1000

_GRACE_SECONDS = 0.5


def _clamp_dimension(value: object, maximum: int) -> int:
    """Clamp a reported terminal dimension into ``[1, maximum]``.

    Non-integer garbage falls back to 1 so a bad probe can never reach
    ``struct.pack`` and raise ``struct.error``.
    """
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return _MIN_DIMENSION
    return max(_MIN_DIMENSION, min(n, maximum))


def _winsize(cols: int, rows: int) -> bytes:
    # struct winsize: rows, cols, xpixel, ypixel (all unsigned short)
    return struct.pack(
        "HHHH",
        _clamp_dimension(rows, _MAX_ROWS),
        _clamp_dimension(cols, _MAX_COLS),
        0,
        0,
    )


def _acquire_controlling_tty() -> None:
    """preexec: make the pty slave (already dup'd to fd 0) the controlling tty.

    Runs in the child after ``start_new_session`` performed setsid. Without
    this, an interactive bash reports "no job control in this shell". Only a
    syscall — safe between fork and exec.
    """
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


class PtyBridge:
    """A child process on a pty master, with byte-stream I/O and a hard close."""

    def __init__(self, proc: subprocess.Popen, master_fd: int) -> None:
        self._proc = proc
        self._fd = master_fd
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> "PtyBridge":
        """Spawn ``argv`` behind a new pty and return a bridge.

        Raises :class:`FileNotFoundError` / :class:`OSError` for ordinary
        exec failures (missing binary, bad cwd) — the caller surfaces those.
        """
        spawn_env = dict(os.environ) if env is None else dict(env)
        if not spawn_env.get("TERM"):
            # pty-hosted programs expect TERM to describe the terminal type;
            # daemons often run without one.
            spawn_env["TERM"] = "xterm-256color"
        master, slave = os.openpty()
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, _winsize(cols, rows))
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=spawn_env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                preexec_fn=_acquire_controlling_tty,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        return cls(proc, master)

    @property
    def pid(self) -> int:
        return int(self._proc.pid)

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def is_alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    # -- I/O --------------------------------------------------------------

    def read(self, timeout: float = 0.2) -> bytes | None:
        """Read up to 64 KiB of raw bytes from the pty master.

        Returns bytes (child output), ``b""`` (no data within *timeout*), or
        ``None`` (EOF / child gone). Never blocks longer than *timeout*.
        Safe to call after :meth:`close`; returns ``None`` then.
        """
        if self._closed:
            return None
        try:
            readable, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not readable:
            return b""
        try:
            data = os.read(self._fd, 65536)
        except OSError as exc:
            # EIO = slave side closed (Linux); EBADF = master already closed.
            if exc.errno in {errno.EIO, errno.EBADF}:
                return None
            raise
        return data or None

    def write(self, data: bytes) -> None:
        """Write raw bytes to the pty master (the child's stdin)."""
        if self._closed or not data:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self._fd, view)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF, errno.EPIPE}:
                    return
                raise
            if n <= 0:
                return
            view = view[n:]

    def resize(self, cols: object, rows: object) -> None:
        """Forward a terminal resize to the child via ``TIOCSWINSZ``.

        Dimensions are clamped first — broken probes report garbage like
        columns=131072, and unclamped values blow up ``struct.pack("HHHH")``.
        """
        if self._closed:
            return
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, _winsize(cols, rows))  # type: ignore[arg-type]
        except OSError:
            pass

    # -- teardown ---------------------------------------------------------

    def close(self) -> None:
        """Terminate the child and reap it. Idempotent, leaves no zombie.

        SIGHUP (the conventional "your terminal went away") → SIGTERM →
        SIGKILL, each to the whole process GROUP with a short grace, so
        helpers spawned by the shell die with it. Then the master fd is
        closed (see module docstring: a hung exit may need that) and the
        child reaped.
        """
        if self._closed:
            return
        self._closed = True
        try:
            pgid = os.getpgid(self._proc.pid)
        except OSError:
            pgid = None
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            if self._proc.poll() is not None:
                break
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    self._proc.send_signal(sig)
            except OSError:
                # killpg returns EPERM for a group mid-exit on macOS; fall
                # back to signalling the leader directly.
                try:
                    self._proc.send_signal(sig)
                except OSError:
                    pass
            deadline = time.monotonic() + _GRACE_SECONDS
            while self._proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    # Context-manager sugar — handy in tests.
    def __enter__(self) -> "PtyBridge":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
