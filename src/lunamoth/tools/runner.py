"""Run shell commands for the agent's `terminal` tool — Hermes/Claude-Code style.

The agent is given ONE language-agnostic capability: run a shell command in its
session workspace. Isolation is provided by the OS, not by intercepting a
specific interpreter, so there is no Python-only guard and no language lock-in.

Two isolation mechanisms (chosen per session, see `sessions.py`):

    admin    no jail — the command runs with your user's full privileges,
             full-machine read/write, cwd in the workspace (Claude-Code-style
             "I trust this machine"; for the trusted operator). Network always
             available.
    sandbox  OS jail: sandbox-exec (macOS) / bubblewrap (Linux) / Landlock.
             Writes confined to the workspace (+ any allow-listed paths);
             network gated by the runtime `allow_network` permission. The default.

Permissions (allow_network, writable_paths) are read fresh on every call, so the
operator can flip them mid-session (TUI `/net on`, `/allow-dir`) without restart.

The jail builders themselves live in `session/isolation.py` (stdlib-only) so the
supervisor's PTY shell can share them without importing tools/.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from ..obs import get_logger
from ..session.isolation import (
    JailUnavailableError,
    _base_env,
    backend,
    build_jail_command,
    os_sandbox_available as os_sandbox_available,  # re-export for callers/tests
)

_log = get_logger("runner")

DEFAULT_TIMEOUT = 30
MIN_TIMEOUT = 1      # audit #17: the model-supplied timeout is clamped to
MAX_TIMEOUT = 600    # [1, 600] s so `timeout=999999` can't wedge an unattended cycle
_OUTPUT_CAP = 12000
_STDERR_CAP = 2000
_KILL_GRACE = 1.0    # seconds between SIGTERM and SIGKILL on timeout
_DRAIN_DEADLINE = 1.0  # bounded non-blocking pipe drain after the group is killed


# ---- ANSI/control stripping (audit #16; ported from hermes tools/ansi_strip.py) ----
# Command output is cleaned before it reaches the model: escape codes waste
# tokens, can derail weaker models, and — the hermes root cause — get copied
# verbatim into file writes. Covers the full ECMA-48 spec: CSI (including
# private-mode `?` prefix, colon-separated params, intermediate bytes), OSC
# (BEL and ST terminators), DCS/SOS/PM/APC string sequences, nF multi-byte
# escapes, Fp/Fe/Fs single-byte escapes, and 8-bit C1 control characters.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
        r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"       # CSI sequence
        r"|\][\s\S]*?(?:\x07|\x1b\\)"                  # OSC (BEL or ST terminator)
        r"|[PX^_][\s\S]*?(?:\x1b\\)"                   # DCS/SOS/PM/APC strings
        r"|[\x20-\x2f]+[\x30-\x7e]"                    # nF escape sequences
        r"|[\x30-\x7e]"                                # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"        # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                      # 8-bit OSC
    r"|[\x80-\x9f]",                                   # Other 8-bit C1 controls
    re.DOTALL,
)

# Fast-path check — skip the full regex when no escape-like bytes are present.
_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text.

    Returns the input unchanged (fast path) when no ESC or C1 bytes are
    present. Safe to call on any string — clean text passes through with
    negligible overhead.
    """
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


# Audit #18 (hermes terminal_tool.py:1609-1670): these commands use exit 1 as
# information ("no match" / "inputs differ"), not failure. A bare exit=1
# invites the model to waste turns investigating a non-error.
_EXIT1_IS_INFO = {
    "grep": "no match found",
    "egrep": "no match found",
    "fgrep": "no match found",
    "rg": "no match found",
    "diff": "the inputs differ",
    "cmp": "the inputs differ",
}


def _exit_code_note(command: str, returncode: int, stderr: str) -> str:
    """A one-line note when exit 1 from a search/compare command is informational.

    Only when the command's FIRST word is one of the known commands, exit is
    exactly 1 and stderr is empty — exit 2+, or exit 1 with stderr (e.g. a
    missing file), still reads as a real failure.
    """
    if returncode != 1 or stderr.strip():
        return ""
    words = command.strip().split()
    first = words[0].split("/")[-1] if words else ""
    meaning = _EXIT1_IS_INFO.get(first)
    if not meaning:
        return ""
    return f"\n[note: exit 1 from {first} means {meaning} — not a failure]"


def truncate_middle(text: str, cap: int) -> str:
    """Explicit head+tail truncation (audit #15; hermes terminal_tool.py:2406).

    Keeps 40% head (error messages often appear early) and 60% tail (the most
    recent output) around a marker stating how much was cut. A silent
    last-N-chars cut hides the head — exactly where compile/launch errors live —
    and reads as complete output, sending the model down wrong paths.
    """
    if len(text) <= cap:
        return text
    head = int(cap * 0.4)
    tail = cap - head
    omitted = len(text) - head - tail
    marker = (
        f"\n\n... [output truncated — {omitted} chars omitted out of {len(text)} total; "
        f"kept the first {head} and last {tail} chars] ...\n\n"
    )
    return text[:head] + marker + text[-tail:]


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM -> grace -> SIGKILL, to the whole process GROUP, then reap.

    `subprocess.run(timeout=)` kills only the leader; a grandchild keeps
    running (and keeps the stdout pipe open, blocking the reader forever —
    hermes scar #17327). Ordering discipline copied from server/pty.py
    PtyBridge.close: killpg returns EPERM for a group mid-exit on macOS, so
    fall back to signalling the leader directly.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            break
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        deadline = time.monotonic() + _KILL_GRACE
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
    try:
        proc.wait(timeout=_KILL_GRACE)
    except subprocess.TimeoutExpired:
        _log.error("terminal leader (pid %d) survived SIGKILL — abandoning, not blocking", proc.pid)


def _drain_nonblocking(stream, deadline: float) -> bytes:
    """Read whatever is immediately available from a pipe without ever blocking.

    Even after the group is killed, a descendant that escaped the group (e.g.
    a double-forked daemon) can hold the write end open — a blocking read
    would hang forever despite the timeout. O_NONBLOCK + a wall-clock deadline
    (the hermes _reconcile_local_exit drain shape).
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    try:
        fd = stream.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (OSError, ValueError):
        return b""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            time.sleep(0.02)  # writer still alive; give it a beat, bounded
            continue
        except (OSError, ValueError):
            break
        if not chunk:
            break  # EOF — every writer is gone
        chunks.append(chunk)
    try:
        stream.close()
    except OSError:
        pass
    return b"".join(chunks)


@dataclass(frozen=True)
class TerminalResult:
    """Structured result of a terminal run, so a caller (execute_code) can judge
    success on the REAL exit code instead of scanning the text for substrings.
    `text` is the same human/model-facing blob `run_terminal` returns.
    `exit_code` is None when the command never produced one (timed out, jail
    refused, or the runner binary was missing)."""
    text: str
    exit_code: int | None = None
    timed_out: bool = False
    refused: bool = False


def run_terminal(
    command: str,
    workspace: Path,
    *,
    isolation: str | None = None,
    allow_network: bool = False,
    writable_paths: "list[str] | tuple[str, ...]" = (),
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str | None = None,
    browser: bool = False,
) -> str:
    """Execute *command* and return the text blob (exit=…/STDOUT/STDERR). Thin
    wrapper over ``run_terminal_result`` for the many callers that only want text."""
    return run_terminal_result(
        command, workspace, isolation=isolation, allow_network=allow_network,
        writable_paths=writable_paths, timeout=timeout, workdir=workdir, browser=browser,
    ).text


def run_terminal_result(
    command: str,
    workspace: Path,
    *,
    isolation: str | None = None,
    allow_network: bool = False,
    writable_paths: "list[str] | tuple[str, ...]" = (),
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str | None = None,
    browser: bool = False,
) -> TerminalResult:
    """Execute *command* in a shell under the active isolation mechanism, returning
    a TerminalResult (text + real exit code + timed_out/refused flags).

    ``browser=True`` selects the browser-specific jail (a real Chromium needs
    more latitude than the deny-default shell profile; the jail keeps writes
    confined to the workspace+temp and the secret home unreadable). See
    ``session.isolation.build_jail_command``."""
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolation = (isolation or backend()).lower()
    if isolation in {"dir", "local", "docker"}:  # legacy values → admin (no jail)
        isolation = "admin"
    # Clamp the (model-supplied) timeout, with a note when clamped — the model
    # must learn the real bound instead of silently getting a different wait.
    requested = int(timeout)
    timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, requested))
    clamp_note = (
        f"\n[lunamoth: timeout clamped to {timeout}s (requested {requested}s; allowed {MIN_TIMEOUT}-{MAX_TIMEOUT}s)]"
        if timeout != requested else ""
    )
    writable = [Path(p).resolve() for p in writable_paths]
    cwd = workspace
    if workdir:
        cand = (workspace / workdir).resolve() if not os.path.isabs(workdir) else Path(workdir).resolve()
        if isolation == "admin" or cand == workspace or workspace in cand.parents or cand in writable:
            cwd = cand

    note = clamp_note
    # The isolation ladder (native OS jail → Landlock → refuse, never directory
    # trust) lives in ONE place — session/isolation.build_jail_command — shared
    # with the background process path so the security contract can't drift.
    try:
        cmd, jail_cwd, jail_note = build_jail_command(
            command, workspace, isolation, allow_network=allow_network, writable=writable,
            browser=browser, workdir=str(cwd),
        )
    except JailUnavailableError as e:
        # NEVER degrade to directory trust — under it the chara could read the
        # whole container, incl. the global key in ~/.lunamoth (OPEN-WORK SEC-low).
        return TerminalResult((f"[lunamoth: refused — {e}]" + note).strip(), refused=True)
    note += jail_note
    # admin / macOS sandbox / Landlock honor the resolved workdir via run_cwd;
    # Linux bwrap sets its own --chdir (jail_cwd is None there), targeting the
    # same validated workdir passed to build_jail_command above. Substitute the
    # resolved workdir when the jail allows a cwd.
    run_cwd = str(cwd) if jail_cwd is not None else None

    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=run_cwd,
            env=_base_env(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group so the timeout path can killpg it
        )
    except FileNotFoundError as e:
        _log.error("terminal runner unavailable (%s): %s", isolation, e)
        return TerminalResult(f"[runner error: {e}]{note}")
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log.warning("terminal command timed out after %ds (%s): %.120s", timeout, isolation, command)
        _kill_group(proc)
        try:
            # The group is dead, so the pipes EOF immediately and this recovers
            # the partial output communicate() had already buffered. Bounded:
            # a descendant that escaped the group (setsid daemon) can still
            # hold the pipes open, hence the timeout + non-blocking fallback.
            out_b, err_b = proc.communicate(timeout=_DRAIN_DEADLINE)
        except subprocess.TimeoutExpired:
            out_b = _drain_nonblocking(proc.stdout, _DRAIN_DEADLINE)
            err_b = _drain_nonblocking(proc.stderr, _DRAIN_DEADLINE)
        parts = [f"[timed out after {timeout}s]"]
        # Strip BEFORE truncating so the cap budgets clean text and the cut
        # can't land mid-escape-sequence and leave fragments behind.
        out = truncate_middle(strip_ansi(out_b.decode("utf-8", errors="replace")), _OUTPUT_CAP).strip()
        err = truncate_middle(strip_ansi(err_b.decode("utf-8", errors="replace")), _STDERR_CAP).strip()
        if out:
            parts.append(f"partial STDOUT:\n{out}")
        if err:
            parts.append(f"partial STDERR:\n{err}")
        return TerminalResult(("\n".join(parts) + note).strip(), timed_out=True)
    _log.info("terminal (%s, net=%s) exit=%d in %.1fs: %.120s",
              isolation, "on" if allow_network else "off", proc.returncode, time.monotonic() - t0, command)

    out = truncate_middle(strip_ansi((out_b or b"").decode("utf-8", errors="replace")), _OUTPUT_CAP)
    err = truncate_middle(strip_ansi((err_b or b"").decode("utf-8", errors="replace")), _STDERR_CAP)
    parts = [f"exit={proc.returncode}" + _exit_code_note(command, proc.returncode, err)]
    if out:
        parts.append(f"STDOUT:\n{out}")
    if err:
        parts.append(f"STDERR:\n{err}")
    return TerminalResult(("\n".join(parts) + note).strip(), exit_code=proc.returncode)


# ---- PTY path (interactive commands: vim/top/REPL/password prompts) ----------
# hermes runs an interactive `pty=true` command under a pseudo-terminal so a tty
# probe (`isatty`) sees a real terminal (hermes process_registry.spawn_local
# use_pty + terminal_tool's pty option). hermes leans on the ptyprocess library;
# LunaMoth is stdlib-only (macOS/Linux only), so we allocate the pty the same way
# server/pty.py PtyBridge does — os.openpty() + TIOCSWINSZ + a controlling-tty
# preexec — and run the SAME jailed argv build_jail_command produces, so the
# isolation contract is byte-identical to the pipe path. There is no stdin: this
# is a one-shot foreground run, not an attached session, so the child sees EOF on
# read (a password prompt that blocks on input simply times out, exactly as the
# pipe path would). Output is the merged tty stream (stdout+stderr share the
# slave), ANSI-stripped + head/tail truncated like the pipe path.
_PTY_COLS = 120   # hermes spawns its pty at 120x30; match it so wide TUIs lay out sanely
_PTY_ROWS = 30


def _pty_winsize(cols: int, rows: int) -> bytes:
    # struct winsize: rows, cols, xpixel, ypixel (unsigned short), as in PtyBridge.
    return struct.pack("HHHH", rows, cols, 0, 0)


def _acquire_controlling_tty() -> None:
    """preexec: make the pty slave (dup'd to fd 0) the controlling tty.

    Mirrors server/pty.py: without it an interactive bash reports "no job
    control in this shell". A single ioctl — safe between fork and exec.
    """
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


def _pty_kill_group(proc: subprocess.Popen, master_fd: int) -> None:
    """SIGHUP → SIGTERM → SIGKILL to the whole group, closing the master fd.

    Same ladder as runner._kill_group, plus the PtyBridge.close discipline: a
    pty session leader can hang in exit teardown until the master fd is closed
    (macOS Darwin scar), and killpg can return EPERM for a group mid-exit — fall
    back to signalling the leader directly.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            break
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        deadline = time.monotonic() + _KILL_GRACE
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_GRACE)
    except subprocess.TimeoutExpired:
        _log.error("pty terminal leader (pid %d) survived SIGKILL — abandoning", proc.pid)


def run_terminal_pty(
    command: str,
    workspace: Path,
    *,
    isolation: str | None = None,
    allow_network: bool = False,
    writable_paths: "list[str] | tuple[str, ...]" = (),
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str | None = None,
) -> str:
    """Execute *command* behind a real PTY, under the active isolation jail.

    The pty variant of :func:`run_terminal`: the command runs INSIDE the same
    sandbox/admin jail (``build_jail_command``), but on a pseudo-terminal so it
    detects a tty (interactive REPLs, vim/top, tools that branch on
    ``isatty``). Honors the same clamped timeout, ANSI strip, head/tail
    truncate, and group-kill ladder. No stdin is attached (one-shot run).
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    isolation = (isolation or backend()).lower()
    if isolation in {"dir", "local", "docker"}:  # legacy values → admin (no jail)
        isolation = "admin"
    requested = int(timeout)
    timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, requested))
    clamp_note = (
        f"\n[lunamoth: timeout clamped to {timeout}s (requested {requested}s; allowed {MIN_TIMEOUT}-{MAX_TIMEOUT}s)]"
        if timeout != requested else ""
    )
    writable = [Path(p).resolve() for p in writable_paths]
    cwd = workspace
    if workdir:
        cand = (workspace / workdir).resolve() if not os.path.isabs(workdir) else Path(workdir).resolve()
        if isolation == "admin" or cand == workspace or workspace in cand.parents or cand in writable:
            cwd = cand

    note = clamp_note
    try:
        cmd, jail_cwd, jail_note = build_jail_command(
            command, workspace, isolation, allow_network=allow_network, writable=writable,
            interactive=True,  # the child sits on a pty slave; macOS needs ttys ioctl
            workdir=str(cwd),  # validated above; bwrap honors it via --chdir
        )
    except JailUnavailableError as e:
        return (f"[lunamoth: refused — {e}]" + note).strip()
    note += jail_note
    run_cwd = str(cwd) if jail_cwd is not None else None

    env = _base_env(workspace)
    env.setdefault("TERM", "xterm-256color")  # pty-hosted programs expect a TERM

    master, slave = os.openpty()
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, _pty_winsize(_PTY_COLS, _PTY_ROWS))
    except OSError:
        pass
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=run_cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,   # own group so the timeout path can killpg it
            preexec_fn=_acquire_controlling_tty,
            close_fds=True,
        )
    except FileNotFoundError as e:
        os.close(master)
        os.close(slave)
        _log.error("pty terminal runner unavailable (%s): %s", isolation, e)
        return f"[runner error: {e}]{note}"
    except Exception:  # noqa: BLE001
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)  # the child holds the only other ref; we read the master end

    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    timed_out = False
    # Grace after the child exits: keep reading buffered bytes, but if the master
    # never EOFs (it can stay open on macOS after the slave closes — the session /
    # controlling-tty keeps a ref) stop once the child has been gone this long with
    # nothing new, instead of hanging to the timeout. Reset on each fresh read so an
    # actively-draining child isn't cut off.
    _EXIT_GRACE = 0.4
    exited_at: float | None = None
    # Read the merged tty stream until EOF (child gone, master EIO), the post-exit
    # grace elapses, or the timeout fires.
    while True:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        if exited_at is not None and now - exited_at >= _EXIT_GRACE:
            break  # child gone + grace drained, even if the master never EOFed
        try:
            readable, _, _ = select.select([master], [], [], 0.1)
        except (OSError, ValueError):
            break
        if readable:
            try:
                data = os.read(master, 65536)
            except OSError as exc:
                # EIO = slave side closed (child exited, Linux); EBADF = master closed.
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                raise
            if not data:
                break  # EOF
            chunks.append(data)
            total += len(data)
            if total > _OUTPUT_CAP * 4 and len(chunks) > 64:
                chunks = chunks[:32] + chunks[-32:]  # coarse middle-drop, refined below
            if exited_at is not None:
                exited_at = time.monotonic()  # got data after exit → extend the grace
            continue
        # nothing readable this slice → start the post-exit grace once the child is gone
        if exited_at is None and proc.poll() is not None:
            exited_at = time.monotonic()

    out_b = b"".join(chunks)
    if timed_out or proc.poll() is None:
        _log.warning("pty terminal command timed out after %ds (%s): %.120s", timeout, isolation, command)
        _pty_kill_group(proc, master)
        out = truncate_middle(strip_ansi(out_b.decode("utf-8", errors="replace")), _OUTPUT_CAP).strip()
        parts = [f"[timed out after {timeout}s]"]
        if out:
            parts.append(f"partial OUTPUT:\n{out}")
        return ("\n".join(parts) + note).strip()

    try:
        os.close(master)
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    rc = proc.poll()
    rc = rc if rc is not None else -1
    _log.info("pty terminal (%s, net=%s) exit=%d in %.1fs: %.120s",
              isolation, "on" if allow_network else "off", rc, time.monotonic() - t0, command)
    out = truncate_middle(strip_ansi(out_b.decode("utf-8", errors="replace")), _OUTPUT_CAP)
    parts = [f"exit={rc}" + _exit_code_note(command, rc, "")]
    if out:
        parts.append(f"OUTPUT:\n{out}")
    return ("\n".join(parts) + note).strip()
