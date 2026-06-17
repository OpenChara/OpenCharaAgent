"""Background-process registry for the `terminal(background=true)` / `process`
tools — re-implemented apple-to-apple from hermes-agent
(reference/hermes-agent/tools/process_registry.py) against LunaMoth's runtime.

hermes is multi-backend + multi-task; LunaMoth is one-process-one-chara, so the
whole multi-environment machinery (spawn_via_env log-poll, task_id registry,
checkpoint/crash-recovery owned by the supervisor, sudo, gateway watcher routing)
collapses. What stays is the self-contained, stdlib-only core:

  * ``ProcessSession`` — a tracked Popen with a 200KB rolling output buffer.
  * a reader thread draining stdout into that buffer + scanning watch_patterns.
  * the per-session + global watch rate-limiter (all five hermes constants).
  * ``_reconcile_local_exit`` — the #17327 orphaned-pipe drain, calling the
    EXISTING ``runner._drain_nonblocking`` primitive.
  * ``kill`` via the EXISTING ``runner._kill_group`` (SIGTERM->grace->SIGKILL to
    the whole process group) — the group-kill hermes reaches for psutil to fake.
  * LRU prune (MAX_PROCESSES=64) + 30-min finished TTL.

Spawn reuses the same isolation builders as ``runner.run_terminal`` (sandbox /
admin), so a background command is jailed identically to a foreground one.

Watch-pattern + completion events are placed on ``completion_queue`` as plain
dicts and exposed via ``drain_notifications()`` — NO protocol coupling here; the
agent layer re-targets them onto the stream. ``terminal`` also surfaces matched
``watch_patterns`` inline as notes in its returned JSON (the spec's "re-emit as
notes" requirement).
"""
from __future__ import annotations

import queue as _queue_mod
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ...session.isolation import (
    _base_env,
    _linux_jail,
    _macos_jail,
    backend,
    os_sandbox_available,
)
from ..runner import _drain_nonblocking, _kill_group, strip_ansi

# ---- Limits (hermes process_registry.py:57-60) ----
MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # keep finished processes 30 minutes
MAX_PROCESSES = 64              # max tracked processes (LRU prune)

# ---- Watch-pattern rate limiting, PER SESSION (hermes:62-75) ----
WATCH_MIN_INTERVAL_SECONDS = 15   # min spacing between consecutive watch matches
WATCH_STRIKE_LIMIT = 3            # strikes in a row -> disable + promote to notify_on_complete

# ---- Global circuit breaker, across all sessions ----
WATCH_GLOBAL_MAX_PER_WINDOW = 15
WATCH_GLOBAL_WINDOW_SECONDS = 10
WATCH_GLOBAL_COOLDOWN_SECONDS = 30

_SHELL_NOISE_SUBSTRINGS = (
    "bash: cannot set terminal process group",
    "bash: no job control in this shell",
    "no job control in this shell",
    "cannot set terminal process group",
    "tcsetattr: Inappropriate ioctl for device",
)


@dataclass
class ProcessSession:
    """A tracked background process with output buffering."""

    id: str
    command: str
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    cwd: Optional[str] = None
    started_at: float = 0.0
    exited: bool = False
    exit_code: Optional[int] = None
    output_buffer: str = ""
    max_output_chars: int = MAX_OUTPUT_CHARS
    notify_on_complete: bool = False
    watch_patterns: List[str] = field(default_factory=list)
    # per-session watch rate-limit counters (hermes:118-131)
    _watch_hits: int = field(default=0, repr=False)
    _watch_suppressed: int = field(default=0, repr=False)
    _watch_disabled: bool = field(default=False, repr=False)
    _watch_last_emit_at: float = field(default=0.0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)


def _build_isolation_cmd(
    command: str,
    workspace: Path,
    isolation: str,
    allow_network: bool,
    writable: list[Path],
) -> tuple[list[str], Optional[str], str]:
    """argv / run_cwd / note for a background command under the session jail.

    Mirrors ``runner.run_terminal``'s dispatch so a background command is jailed
    exactly like a foreground one. Returns ``(cmd, run_cwd, note)`` where *note*
    is a degradation message when a requested jail is unavailable (never a silent
    escape — same contract as the foreground runner).
    """
    isolation = (isolation or backend()).lower()
    if isolation in {"dir", "local", "docker"}:  # legacy values → admin (no jail)
        isolation = "admin"
    note = ""
    if isolation == "sandbox" and os_sandbox_available():
        cmd = (_macos_jail if sys.platform == "darwin" else _linux_jail)(
            command, workspace, allow_network, writable
        )
        run_cwd = str(workspace) if sys.platform == "darwin" else None
    else:
        if isolation != "admin":
            note = f"\n[lunamoth: '{isolation}' jail unavailable, ran with directory trust]"
        cmd = ["/bin/bash", "-c", command]
        run_cwd = str(workspace)
    return cmd, run_cwd, note


class ProcessRegistry:
    """In-memory registry of running and finished background processes.

    Thread-safe. Stashed on ``ctx.processes`` (lazily); one per chara session.
    """

    def __init__(self) -> None:
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = threading.Lock()
        self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()
        self._completion_consumed: set = set()
        # global watch breaker state
        self._global_watch_lock = threading.Lock()
        self._global_watch_window_start: float = 0.0
        self._global_watch_window_hits: int = 0
        self._global_watch_tripped_until: float = 0.0
        self._global_watch_suppressed_during_trip: int = 0

    # ----- spawn -----

    def spawn(
        self,
        command: str,
        workspace: Path,
        *,
        isolation: str,
        allow_network: bool = False,
        writable_paths: "list[str] | tuple[str, ...]" = (),
        cwd: Optional[Path] = None,
        notify_on_complete: bool = False,
        watch_patterns: Optional[List[str]] = None,
    ) -> ProcessSession:
        """Spawn a background process under the session's isolation.

        Spawned with ``stdin=PIPE`` (so process write/submit/close work) and
        ``start_new_session=True`` (own process group, killpg-able — the same
        discipline the foreground runner uses).
        """
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        writable = [Path(p).resolve() for p in writable_paths]
        run_cwd_default = str(cwd.resolve()) if cwd else str(workspace)
        cmd, run_cwd, _note = _build_isolation_cmd(
            command, workspace, isolation, allow_network, writable
        )
        # For the admin / fallback path, honor an explicit cwd; jailed paths
        # chdir themselves (bwrap --chdir, macOS sandbox via run_cwd).
        if run_cwd is not None and cwd is not None:
            run_cwd = run_cwd_default

        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            cwd=run_cwd or str(workspace),
            started_at=time.time(),
            notify_on_complete=notify_on_complete,
            watch_patterns=list(watch_patterns or []),
        )

        bg_env = _base_env(workspace)
        bg_env["PYTHONUNBUFFERED"] = "1"  # unbuffered so poll() sees live output

        proc = subprocess.Popen(
            cmd,
            cwd=run_cwd,
            env=bg_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        session.process = proc
        session.pid = proc.pid

        try:
            reader = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()
            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session
        except Exception:
            # Setup failed post-spawn — kill the orphan (and its group) before
            # re-raising so it doesn't leak untracked.
            try:
                _kill_group(proc)
            except Exception:
                pass
            raise
        return session

    # ----- reader thread -----

    def _reader_loop(self, session: ProcessSession) -> None:
        """Drain stdout into the rolling buffer; scan watch patterns."""
        first_chunk = True
        try:
            stdout = session.process.stdout
            while True:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                if first_chunk:
                    text = self._clean_shell_noise(text)
                    first_chunk = False
                with session._lock:
                    session.output_buffer += text
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
                self._check_watch_patterns(session, text)
        except Exception:
            pass
        finally:
            try:
                session.process.wait(timeout=5)
            except Exception:
                pass
            session.exited = True
            session.exit_code = session.process.returncode
            self._move_to_finished(session)

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        lines = text.split("\n")
        while lines and any(noise in lines[0] for noise in _SHELL_NOISE_SUBSTRINGS):
            lines.pop(0)
        return "\n".join(lines)

    # ----- watch patterns -----

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """Scan a fresh chunk for watch patterns; rate-limit + queue matches.

        Faithful to hermes (process_registry.py:191-317): per-session 15s
        cooldown, 3-strike disable+promote, global circuit breaker.
        """
        if not session.watch_patterns or session._watch_disabled:
            return
        if session.exited:
            return  # post-exit noise — drop to avoid stale notifications

        matched_lines: list[str] = []
        matched_pattern: Optional[str] = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break
        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        suppressed = 0
        with session._lock:
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        session.notify_on_complete = True
                        should_disable = True
                return_early = True
            else:
                if session._watch_cooldown_until and not session._watch_strike_candidate:
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False
                session._watch_last_emit_at = now
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                session._watch_hits += 1
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                return_early = False

        if return_early:
            if should_disable:
                self.completion_queue.put({
                    "session_id": session.id,
                    "command": session.command,
                    "type": "watch_disabled",
                    "suppressed": session._watch_suppressed,
                    "message": (
                        f"Watch patterns disabled for process {session.id} — "
                        f"{WATCH_STRIKE_LIMIT} consecutive rate-limit windows triggered "
                        f"(min spacing {WATCH_MIN_INTERVAL_SECONDS}s). Falling back to "
                        f"notify_on_complete; you'll get exactly one notification on exit."
                    ),
                })
            return

        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"

        if not self._global_watch_admit(now):
            return

        self.completion_queue.put({
            "session_id": session.id,
            "command": session.command,
            "type": "watch_match",
            "pattern": matched_pattern,
            "output": output,
            "suppressed": suppressed,
        })

    def _global_watch_admit(self, now: float) -> bool:
        """Global rolling-window breaker (hermes:319-404)."""
        release_msg = None
        trip_now = None
        with self._global_watch_lock:
            if self._global_watch_tripped_until and now >= self._global_watch_tripped_until:
                suppressed = self._global_watch_suppressed_during_trip
                self._global_watch_tripped_until = 0.0
                self._global_watch_suppressed_during_trip = 0
                self._global_watch_window_start = now
                self._global_watch_window_hits = 0
                if suppressed > 0:
                    release_msg = {
                        "session_id": "",
                        "command": "",
                        "type": "watch_overflow_released",
                        "suppressed": suppressed,
                        "message": (
                            f"Watch-pattern notifications resumed. "
                            f"{suppressed} match event(s) were suppressed during the flood."
                        ),
                    }

            if self._global_watch_tripped_until and now < self._global_watch_tripped_until:
                self._global_watch_suppressed_during_trip += 1
                admit = False
            else:
                if now - self._global_watch_window_start >= WATCH_GLOBAL_WINDOW_SECONDS:
                    self._global_watch_window_start = now
                    self._global_watch_window_hits = 0
                if self._global_watch_window_hits >= WATCH_GLOBAL_MAX_PER_WINDOW:
                    self._global_watch_tripped_until = now + WATCH_GLOBAL_COOLDOWN_SECONDS
                    self._global_watch_suppressed_during_trip += 1
                    trip_now = now
                    admit = False
                else:
                    self._global_watch_window_hits += 1
                    admit = True

        if release_msg is not None:
            self.completion_queue.put(release_msg)
        if trip_now is not None:
            self.completion_queue.put({
                "session_id": "",
                "command": "",
                "type": "watch_overflow_tripped",
                "message": (
                    f"Watch-pattern overflow: >{WATCH_GLOBAL_MAX_PER_WINDOW} "
                    f"notifications in {WATCH_GLOBAL_WINDOW_SECONDS}s across all processes. "
                    f"Suppressing further watch_match events for "
                    f"{WATCH_GLOBAL_COOLDOWN_SECONDS}s."
                ),
            })
        return admit

    # ----- lifecycle -----

    def _move_to_finished(self, session: ProcessSession) -> None:
        """Move running -> finished. Idempotent (kill can race the reader)."""
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            self._finished[session.id] = session
        if was_running and session.notify_on_complete:
            output_tail = strip_ansi(session.output_buffer[-2000:]) if session.output_buffer else ""
            self.completion_queue.put({
                "type": "completion",
                "session_id": session.id,
                "command": session.command,
                "exit_code": session.exit_code,
                "output": output_tail,
            })

    def _prune_if_needed(self) -> None:
        """Drop expired finished sessions + LRU-evict over MAX_PROCESSES. Hold _lock."""
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
            self._completion_consumed.discard(sid)
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest_id]
            self._completion_consumed.discard(oldest_id)
        tracked = self._running.keys() | self._finished.keys()
        stale = self._completion_consumed - tracked
        if stale:
            self._completion_consumed -= stale

    def _reconcile_local_exit(self, session: Optional[ProcessSession]) -> None:
        """The #17327 orphaned-pipe drain.

        The reader flips ``exited`` only in its ``finally`` after stdout EOF. If
        the direct child exited but a surviving grandchild holds the pipe open,
        the reader blocks forever and poll/wait report "running" indefinitely.
        When the direct child has a real exit code, drain whatever bytes are
        immediately available (via the existing ``runner._drain_nonblocking``)
        and flip ``exited``. The orphaned reader stays stuck but is a daemon.
        """
        if session is None or session.exited:
            return
        proc = session.process
        if proc is None:
            return
        try:
            rc = proc.poll()
        except Exception:
            return
        if rc is None:
            return  # direct child still running — the reader block is legitimate

        drained = ""
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            # Bounded non-blocking drain; reuse the runner primitive (it also
            # closes the stream, which is fine — the reader is abandoned).
            raw = _drain_nonblocking(stdout, 0.2)
            if raw:
                drained = raw.decode("utf-8", errors="replace")

        with session._lock:
            if drained:
                session.output_buffer += drained
                if len(session.output_buffer) > session.max_output_chars:
                    session.output_buffer = session.output_buffer[-session.max_output_chars:]
            session.exited = True
            session.exit_code = rc
        self._move_to_finished(session)

    # ----- queries -----

    def get(self, session_id: str) -> Optional[ProcessSession]:
        with self._lock:
            return self._running.get(session_id) or self._finished.get(session_id)

    def is_completion_consumed(self, session_id: str) -> bool:
        return session_id in self._completion_consumed

    def drain_notifications(self) -> list[dict]:
        """Pop all pending completion/watch events (skipping already-consumed
        completions). Returns the raw event dicts — the agent layer formats +
        re-targets them onto the protocol stream."""
        results: list[dict] = []
        while not self.completion_queue.empty():
            try:
                evt = self.completion_queue.get_nowait()
            except Exception:
                break
            sid = evt.get("session_id", "")
            if evt.get("type") == "completion" and self.is_completion_consumed(sid):
                continue
            results.append(evt)
        return results

    def poll(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        self._reconcile_local_exit(session)
        with session._lock:
            output_preview = strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""
        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": output_preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
            self._completion_consumed.add(session_id)
        return result

    def read_log(self, session_id: str, offset: int = 0, limit: int = 200) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        with session._lock:
            full_output = strip_ansi(session.output_buffer)
        lines = full_output.splitlines()
        total_lines = len(lines)
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset:offset + limit]
        result = {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }
        if session.exited:
            self._completion_consumed.add(session_id)
        return result

    def wait(self, session_id: str, timeout: Optional[int] = None,
             default_timeout: int = 180, interrupted=None) -> dict:
        """Block until exit / timeout / interrupt.

        ``interrupted`` is an optional zero-arg callable returning True when a
        new user turn arrived mid-wait (LunaMoth's engagement signal — the
        hermes ``is_interrupted()`` equivalent). When None, never interrupts.
        """
        max_timeout = default_timeout
        timeout_note = None
        if timeout and timeout > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {timeout}s was clamped to configured limit of {max_timeout}s"
            )
        else:
            effective_timeout = timeout or max_timeout

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            self._reconcile_local_exit(session)
            if session.exited:
                self._completion_consumed.add(session_id)
                result = {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result
            if interrupted is not None:
                try:
                    if interrupted():
                        result = {
                            "status": "interrupted",
                            "output": strip_ansi(session.output_buffer[-1000:]),
                            "note": "User sent a new message -- wait interrupted",
                        }
                        if timeout_note:
                            result["timeout_note"] = timeout_note
                        return result
                except Exception:
                    pass
            time.sleep(1)

        result = {
            "status": "timeout",
            "output": strip_ansi(session.output_buffer[-1000:]),
        }
        result["timeout_note"] = timeout_note or f"Waited {effective_timeout}s, process still running"
        return result

    def kill_process(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "exit_code": session.exit_code}
        try:
            if session.process:
                # Reuse the runner's group kill — SIGTERM->grace->SIGKILL to the
                # whole process group (the bg Popen is start_new_session=True).
                _kill_group(session.process)
            else:
                return {
                    "status": "error",
                    "error": "Process handle no longer available; cannot kill",
                }
            session.exited = True
            session.exit_code = -15  # SIGTERM
            self._move_to_finished(session)
            return {"status": "killed", "session_id": session.id}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}

    def write_stdin(self, session_id: str, data: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (stdin closed)"}
        try:
            payload = data.encode("utf-8") if isinstance(data, str) else data
            session.process.stdin.write(payload)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        return self.write_stdin(session_id, data + "\n")

    def close_stdin(self, session_id: str) -> dict:
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (stdin closed)"}
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}

    def list_sessions(self) -> list:
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())
        result = []
        for s in all_sessions:
            entry = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            if s.exited:
                entry["exit_code"] = s.exit_code
            result.append(entry)
        return result

    def count_running(self) -> int:
        try:
            return len(self._running)
        except Exception:
            return 0


def get_registry(ctx) -> ProcessRegistry:
    """Lazily create + stash the per-session registry on ``ctx.processes``."""
    reg = getattr(ctx, "processes", None)
    if not isinstance(reg, ProcessRegistry):
        reg = ProcessRegistry()
        ctx.processes = reg
    return reg
