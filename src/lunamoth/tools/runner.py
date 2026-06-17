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

import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..obs import get_logger
from ..session.isolation import (
    _base_env,
    _linux_jail,
    _linux_landlock_jail,
    _macos_jail,
    backend,
    landlock_available,
    os_sandbox_available,
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


def run_terminal(
    command: str,
    workspace: Path,
    *,
    isolation: str | None = None,
    allow_network: bool = False,
    writable_paths: "list[str] | tuple[str, ...]" = (),
    timeout: int = DEFAULT_TIMEOUT,
    workdir: str | None = None,
) -> str:
    """Execute *command* in a shell under the active isolation mechanism."""
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
    cmd: list[str]
    run_cwd: str | None
    if isolation == "admin":
        # Explicit opt-out of the jail (operator chose `admin`): full-machine
        # read/write, cwd in the workspace (trusted operator).
        cmd = ["/bin/bash", "-c", command]
        run_cwd = str(cwd)
    elif isolation == "sandbox":
        # Isolation ladder: native OS jail (bwrap/seatbelt) → Landlock → refuse.
        # NEVER degrade to directory trust — under it the chara could read the
        # whole container, incl. the global key in ~/.lunamoth (OPEN-WORK SEC-low).
        if os_sandbox_available():
            cmd = (_macos_jail if sys.platform == "darwin" else _linux_jail)(command, workspace, allow_network, writable)
            run_cwd = str(cwd) if sys.platform == "darwin" else None  # bwrap sets its own chdir
        elif landlock_available():
            cmd = _linux_landlock_jail(command, workspace, allow_network, writable)
            run_cwd = str(cwd)
            if not allow_network:
                # Honest: Landlock ABI v1 confines the filesystem only — it cannot
                # gate the network, so `/net off` is NOT enforced under this tier.
                note += "\n[lunamoth: Landlock jail — filesystem confined, but network not gated (ABI v1)]"
        else:
            return ("[lunamoth: refused — sandbox isolation requested but no jail is available "
                    "(no bwrap user namespaces, no Landlock ≥5.13). Not running unconfined. "
                    "Install bubblewrap, run on a Landlock-capable kernel, or set isolation=admin "
                    "to explicitly opt out of the jail.]" + note).strip()
    else:
        return (f"[lunamoth: refused — unknown isolation {isolation!r}]" + note).strip()

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
        return f"[runner error: {e}]{note}"
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
        return ("\n".join(parts) + note).strip()
    _log.info("terminal (%s, net=%s) exit=%d in %.1fs: %.120s",
              isolation, "on" if allow_network else "off", proc.returncode, time.monotonic() - t0, command)

    out = truncate_middle(strip_ansi((out_b or b"").decode("utf-8", errors="replace")), _OUTPUT_CAP)
    err = truncate_middle(strip_ansi((err_b or b"").decode("utf-8", errors="replace")), _STDERR_CAP)
    parts = [f"exit={proc.returncode}" + _exit_code_note(command, proc.returncode, err)]
    if out:
        parts.append(f"STDOUT:\n{out}")
    if err:
        parts.append(f"STDERR:\n{err}")
    return ("\n".join(parts) + note).strip()
