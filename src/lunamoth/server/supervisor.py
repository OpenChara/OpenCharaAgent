"""Resident desktop supervisor (lunamothd).

The supervisor owns long-lived per-chara stdio children and exposes them to the
web renderer as a thin JSON-RPC pipe. It deliberately never imports core/tools:
chara work happens only inside ``lunamoth serve <name> --stdio`` children.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import gc
import http.server
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..obs.audit import AuditLog
from ..session import isolation as I
from ..session import sessions as S
from . import authpw as AUTHPW
from . import hub as H
from . import netsec as N
from .pty import PtyBridge
from .ws import _WSSink, _close_ws, _path_from_ws, _recv_text

_log = logging.getLogger("lunamoth.server.supervisor")

APP_DIR = Path(__file__).resolve().parents[3]
# The built React SPA (apps/web → `npm run build`). Gitignored, bundled into the
# wheel via package-data; `cd apps/web && npm run build` regenerates it in dev.
WEB_DIR = Path(__file__).resolve().parents[1] / "front" / "webui"
UPLOAD_MAX = 8 * 1024 * 1024

# Static files that may be served BEFORE auth — the SPA shell + its hashed JS/CSS
# bundle. They carry no secrets (the token arrives in the URL hash, never baked
# into the bundle) and must load so the page can run the `?token=` handshake.
# Everything else (/asset, /rpc, /upload, the data the app fetches) is gated.
_PREAUTH_EXACT = frozenset({"/", "/index.html", "/favicon.ico", "/authinfo", "/login"})
_PREAUTH_PREFIXES = ("/assets/",)


def _is_preauth_path(path: str) -> bool:
    return path in _PREAUTH_EXACT or any(path.startswith(p) for p in _PREAUTH_PREFIXES)
ISOLATION_TO_BACKEND = {"dir": "local", "sandbox": "sandbox", "docker": "docker"}

# Whole-frame resize escape consumed server-side by the PTY endpoint
# (hermes shape): \x1b[RESIZE:<cols>;<rows>] — full-match only.
_PTY_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_TIMEOUT = 0.2


# ---- shutdown forensics + resource canary (#28) ----------------------------
#
# A week-long lunamothd that leaks (cached children, MCP connections, tool
# schemas, transcript handles) is invisible until the OS OOM-kills it, and
# when it dies the daemon log says nothing about *why*. Two small, never-throw
# instruments — ported in shape from hermes' gateway/memory_monitor.py and
# gateway/shutdown_forensics.py, kept stdlib-only and trimmed to our needs:
#
#   * a 5-minute `[MEMORY] rss/gc/threads/uptime` line on a daemon thread, so a
#     slow climb is grep-able after the fact, and
#   * a fast (<10ms), non-blocking snapshot of who/what triggered shutdown,
#     logged synchronously from the signal path.

_BYTES_TO_MB = 1024 * 1024
_MEMORY_INTERVAL_SECONDS = 300.0


def _rss_mb() -> int | None:
    """Current process RSS in MB, or None if introspection is unavailable.

    Uses stdlib ``resource`` (Linux/macOS); ``ru_maxrss`` is bytes on macOS,
    KB on Linux. Never raises.
    """
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(maxrss / _BYTES_TO_MB)
        return int(maxrss / 1024)
    except Exception:
        return None


def log_memory_usage(prefix: str = "", *, start_time: float | None = None) -> None:
    """Emit a grep-friendly ``[MEMORY] ...`` line. Safe from any thread, never raises."""
    rss = _rss_mb()
    uptime = int(time.monotonic() - start_time) if start_time else 0
    try:
        gc_counts = gc.get_count()
    except Exception:
        gc_counts = (0, 0, 0)
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0
    tag = f"{prefix} " if prefix else ""
    rss_str = "unavailable" if rss is None else f"{rss}MB"
    _log.info("[MEMORY] %srss=%s gc=%s threads=%d uptime=%ds", tag, rss_str, gc_counts, thread_count, uptime)


class ResourceCanary:
    """Periodic `[MEMORY]` logger on a daemon thread (leak detection).

    Daemon thread → never blocks process exit; every iteration is wrapped so a
    failed log can never throw into the agent/serve path. A baseline line is
    emitted on start and a final ``shutdown`` line on stop.
    """

    def __init__(self, interval: float = _MEMORY_INTERVAL_SECONDS) -> None:
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float | None = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        if _rss_mb() is None:
            _log.warning("[MEMORY] resource canary unavailable (no resource.getrusage) — skipping")
            return False
        self._start_time = time.monotonic()
        self._stop.clear()
        log_memory_usage("baseline", start_time=self._start_time)
        self._thread = threading.Thread(target=self._loop, name="lunamothd-memory-canary", daemon=True)
        self._thread.start()
        _log.info("[MEMORY] resource canary started (interval=%ds)", int(self.interval))
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                log_memory_usage(start_time=self._start_time)
            except Exception:
                _log.debug("memory canary iteration failed", exc_info=True)

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        with contextlib.suppress(Exception):
            log_memory_usage("shutdown", start_time=self._start_time)
        self._stop.set()
        self._thread = None
        with contextlib.suppress(Exception):
            thread.join(timeout=timeout)


def snapshot_shutdown_context(received_signal: Any = None) -> dict[str, Any]:
    """Fast (<10ms), never-raising snapshot of who/what asked us to shut down.

    Captures the signal name/number, our pid/ppid + parent process info
    (cmdline on Linux via /proc), whether systemd is our parent, RSS and the
    1-min load average. Pure stdlib; nothing here blocks on a subprocess.
    """
    pid = os.getpid()
    ppid = os.getppid()
    ctx: dict[str, Any] = {
        "ts": time.time(),
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid,
        "ppid": ppid,
    }
    parent = _proc_summary(ppid)
    if parent:
        ctx["parent"] = parent
    invocation_id = os.environ.get("INVOCATION_ID")
    ctx["under_systemd"] = bool(invocation_id) or ppid == 1
    if invocation_id:
        ctx["systemd_invocation_id"] = invocation_id
    rss = _rss_mb()
    if rss is not None:
        ctx["rss_mb"] = rss
    try:
        ctx["loadavg_1m"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    return ctx


_SIGNAL_NAME_BY_NUM: dict[int, str] = {}
for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
    _val = getattr(signal, _name, None)
    if _val is not None:
        _SIGNAL_NAME_BY_NUM[int(_val)] = _name


def _signal_name(sig: Any) -> str:
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _proc_summary(pid: int) -> dict[str, Any]:
    """Compact /proc/<pid> snapshot (Linux only); empty dict elsewhere. Never raises."""
    if pid <= 0 or not sys.platform.startswith("linux"):
        return {}
    summary: dict[str, Any] = {"pid": pid}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Name:"):
                    summary["name"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            data = fh.read()
        if data:
            summary["cmdline"] = data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()[:300]
    except OSError:
        pass
    return summary


def format_shutdown_context(ctx: dict[str, Any]) -> str:
    """Render a shutdown context dict as one scannable log line."""
    parent = ctx.get("parent") or {}
    parts = [
        f"signal={ctx.get('signal', '?')}",
        f"under_systemd={'yes' if ctx.get('under_systemd') else 'no'}",
        f"parent_pid={parent.get('pid', '?')}",
        f"parent_name={parent.get('name', '?')}",
    ]
    if "rss_mb" in ctx:
        parts.append(f"rss={ctx['rss_mb']}MB")
    if "loadavg_1m" in ctx:
        parts.append(f"loadavg_1m={ctx['loadavg_1m']}")
    if parent.get("cmdline"):
        parts.append(f"parent_cmdline={parent['cmdline']!r}")
    return " ".join(parts)


# ---- small, unit-testable primitives ---------------------------------------

class FrameRing:
    """A fixed-size replay buffer with monotonic top-level seq injection."""

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = int(capacity)
        self._frames: deque[dict[str, Any]] = deque(maxlen=max(1, self.capacity))
        self.seq = 0

    def reset(self) -> None:
        self._frames.clear()
        self.seq = 0

    def push(self, frame: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        out = dict(frame)
        out["seq"] = self.seq
        self._frames.append(out)
        return out

    @property
    def oldest_seq(self) -> int:
        return int(self._frames[0]["seq"]) if self._frames else self.seq + 1

    def replay_after(self, last_seq: int) -> tuple[bool, list[dict[str, Any]]]:
        last = int(last_seq)
        if last < 0 or last > self.seq:
            return False, []
        if self._frames and last < self.oldest_seq - 1:
            return False, []
        return True, [dict(f) for f in self._frames if int(f.get("seq", 0)) > last]


class PermanentIdleBackoff:
    """Same policy shape as front.terminal.TerminalState for permanent errors."""

    def __init__(self) -> None:
        self.idle_backoff = 0.0
        self.idle_blocked_until = 0.0

    def reset(self) -> None:
        self.idle_backoff = 0.0
        self.idle_blocked_until = 0.0

    def note_permanent_idle_error(self, now: float) -> float:
        self.idle_backoff = 60.0 if self.idle_backoff <= 0 else min(self.idle_backoff * 2.0, 1800.0)
        self.idle_blocked_until = now + self.idle_backoff
        return self.idle_backoff

    def remaining(self, now: float) -> float:
        return max(0.0, self.idle_blocked_until - now)


def permanent_model_error(message: str) -> bool:
    msg = str(message or "")
    return any(s in msg for s in ("HTTP 401", "HTTP 403", "HTTP 404"))


class RestartBackoff:
    """Supervised-restart bookkeeping with injectable clock.

    Shared by CharaChild and GatewayChild so both get the same discipline:
    exponential backoff (floor → ×2 → cap), a consecutive-crash strike
    counter capped at a stuck-loop threshold (hermes ``_STUCK_LOOP_THRESHOLD
    = 3``), and a health reset — a run that stays up past a threshold clears
    BOTH the backoff and the strikes so an isolated crash doesn't compound.

    No async sleeps live here: callers ask for ``next_delay()`` and sleep
    themselves, and call ``note_started()`` / ``note_healthy_if_due()`` /
    ``note_crash()`` around the child lifecycle. That keeps the policy
    unit-testable with a fake monotonic clock.
    """

    def __init__(
        self,
        *,
        floor: float = 60.0,
        cap: float = 1800.0,
        health_after: float = 120.0,
        max_strikes: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.floor = float(floor)
        self.cap = float(cap)
        self.health_after = float(health_after)
        self.max_strikes = int(max_strikes)
        self.monotonic = monotonic
        self.delay = self.floor
        self.strikes = 0
        self._started_at = 0.0
        self._counted_healthy = False

    def reset(self) -> None:
        self.delay = self.floor
        self.strikes = 0
        self._started_at = 0.0
        self._counted_healthy = False

    def note_started(self) -> None:
        """Mark the moment a child process came up (resets the health timer)."""
        self._started_at = self.monotonic()
        self._counted_healthy = False

    def note_healthy_if_due(self) -> bool:
        """Reset backoff + strikes if the current run has been up long enough.

        Returns True the first time a run crosses the health threshold so the
        caller can emit a transition. Idempotent within one run.
        """
        if self._counted_healthy or self._started_at <= 0.0:
            return False
        if self.monotonic() - self._started_at < self.health_after:
            return False
        self._counted_healthy = True
        self.delay = self.floor
        self.strikes = 0
        return True

    def note_crash(self) -> float:
        """Record an unexpected exit; return the delay to wait before retry.

        If the run had already been declared healthy, the strike counter and
        backoff were reset in note_healthy_if_due, so this crash starts a fresh
        ladder. Increments the strike counter and grows the delay.
        """
        # A run that proved healthy resets the ladder even if note_healthy_if_due
        # was never polled (e.g. exit detected before the next idle tick).
        if not self._counted_healthy and self._started_at > 0.0 and self.monotonic() - self._started_at >= self.health_after:
            self.delay = self.floor
            self.strikes = 0
        delay = self.delay
        self.strikes += 1
        self.delay = min(self.delay * 2.0, self.cap)
        return delay

    @property
    def suspended(self) -> bool:
        return self.strikes >= self.max_strikes


@dataclass(frozen=True)
class LifeState:
    state: str
    next_cycle_at: float = 0.0
    rest_until: float = 0.0
    engaged_until: float = 0.0
    detail: str = ""

    def frame(self) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "method": "life.state", "params": dataclasses.asdict(self)}


class IdleGate:
    """Quiet/rest/backoff/patience gate with injectable clocks."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        epoch: Callable[[], float] = time.time,
    ) -> None:
        self.monotonic = monotonic
        self.epoch = epoch
        self.backoff = PermanentIdleBackoff()
        self.last_user_mono = 0.0
        self.next_cycle_mono = 0.0
        self.working = False
        self.detail = ""

    @staticmethod
    def delay(snapshot: dict[str, Any]) -> float:
        return max(0.0, float(snapshot.get("patience") or 600.0))

    def note_user(self) -> None:
        self.last_user_mono = self.monotonic()

    def schedule_after(self, snapshot: dict[str, Any]) -> None:
        self.next_cycle_mono = self.monotonic() + self.delay(snapshot)

    def mark_working(self, detail: str = "") -> LifeState:
        self.working = True
        self.detail = detail
        return self.life_state({})

    def mark_idle_complete(self, snapshot: dict[str, Any]) -> LifeState:
        self.working = False
        self.detail = ""
        self.backoff.reset()
        self.schedule_after(snapshot)
        return self.life_state(snapshot)

    def mark_idle_error(self, message: str) -> LifeState:
        self.working = False
        self.detail = message[:240]
        if permanent_model_error(message):
            self.backoff.note_permanent_idle_error(self.monotonic())
        return self.life_state({})

    def life_state(self, snapshot: dict[str, Any]) -> LifeState:
        now_m = self.monotonic()
        now_e = self.epoch()
        if self.working:
            return LifeState("working", detail=self.detail)
        quiet = max(0, int(snapshot.get("quiet") or 300))
        engaged_until_m = self.last_user_mono + quiet if self.last_user_mono else 0.0
        if engaged_until_m and now_m < engaged_until_m:
            return LifeState("waiting", engaged_until=now_e + (engaged_until_m - now_m))
        rest_until = float(snapshot.get("rest_until") or 0.0)
        if rest_until > now_e:
            return LifeState("resting", rest_until=rest_until)
        remaining = self.backoff.remaining(now_m)
        if remaining > 0:
            return LifeState("backoff", next_cycle_at=now_e + remaining, detail=self.detail)
        if self.next_cycle_mono <= 0:
            self.schedule_after(snapshot)
        if now_m < self.next_cycle_mono:
            return LifeState("idle_countdown", next_cycle_at=now_e + (self.next_cycle_mono - now_m))
        return LifeState("idle_countdown", next_cycle_at=now_e)

    def ready(self, snapshot: dict[str, Any]) -> bool:
        st = self.life_state(snapshot)
        if st.state != "idle_countdown":
            return False
        return bool(st.next_cycle_at <= self.epoch() + 1e-6)


class DriverSlot:
    """Single-driver takeover helper, independent of websockets."""

    def __init__(self) -> None:
        self.current: Any = None

    async def take(self, driver: Any, close_old: Callable[[Any], Awaitable[None]]) -> None:
        old = self.current
        self.current = driver
        if old is not None and old is not driver:
            await close_old(old)

    def clear(self, driver: Any) -> None:
        if self.current is driver:
            self.current = None


# A stalled browser must not wedge the child's stdout pump: the supervisor
# reads the child's stdout on the loop and forwards each frame to the driver
# inline, so a `ws.send` that blocks on a slow client backs pressure all the
# way down into the agent (its stdout pipe fills, then it blocks on write).
# The FrameRing already lets a client recover missed frames on rejoin, so we
# bound the per-frame send: on timeout we drop the driver rather than freeze
# the pump — the slow client simply rejoins and replays from its last seq.
_DRIVER_SEND_TIMEOUT_SECONDS = 10.0


class _Driver:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.joined = False
        self.lock = asyncio.Lock()

    async def send(self, frame: dict[str, Any]) -> bool:
        try:
            raw = json.dumps(frame, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        async with self.lock:
            try:
                await asyncio.wait_for(self.ws.send(raw), timeout=_DRIVER_SEND_TIMEOUT_SECONDS)
                return True
            except Exception:
                # Timeout or transport error ⇒ client is gone/wedged. Caller
                # drops the driver; the client recovers via FrameRing rejoin.
                return False

    async def close_superseded(self) -> None:
        await _close_ws(self.ws, 4408, "superseded")


# ---- chara children ---------------------------------------------------------

class CharaChild:
    SNAPSHOT_TTL = 5.0

    def __init__(self, meta: S.SessionMeta, supervisor: "Supervisor") -> None:
        self.meta = meta
        self.name = meta.name
        self.supervisor = supervisor
        self.ring = FrameRing(4096)
        self.driver_slot = DriverSlot()
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.detail = ""
        self._stdout_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._pending: dict[Any, asyncio.Future] = {}
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        self._stopping = False
        self._attached = False
        self._snap_cache: tuple[float, dict[str, Any]] | None = None
        self._client_stream_ids: set[Any] = set()
        self.idle = IdleGate()
        self.life: LifeState | None = None
        # Supervised auto-restart: an unexpected exit restarts with backoff
        # (60s→1800s) up to 3 consecutive crashes, then suspends (terminal
        # crashed). A healthy run resets both. Operator start clears suspension.
        self.restart = RestartBackoff()

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "pid": self.proc.pid if self.proc else 0, "life": dataclasses.asdict(self.life) if self.life else None}

    async def start(self, *, operator: bool = True) -> dict[str, Any]:
        # operator=True is an explicit start (user message / WS attach / RPC):
        # it always clears any suspension and pending backoff and tries again.
        # operator=False is the internal supervised restart path.
        if operator:
            self._cancel_pending_restart()
            self.restart.reset()
        async with self._lock:
            if self.proc is not None and self.proc.returncode is None:
                return self.status()
            meta = S.load_session(self.name) or self.meta
            self.meta = meta
            if not meta.is_configured():
                self.state, self.detail = "error", "chara is not set up yet"
                self._emit_life(LifeState("backoff", detail=self.detail))
                return self.status()
            legacy = meta.running_pid()
            if legacy:
                self.state, self.detail = "error", f"another frontend is attached (pid {legacy})"
                self._emit_life(LifeState("backoff", detail=self.detail))
                return self.status()
            daemon = meta.daemon_pid()
            if daemon:
                self.state, self.detail = "error", f"legacy daemon is running (pid {daemon})"
                self._emit_life(LifeState("backoff", detail=self.detail))
                return self.status()
            env = {**os.environ, **meta.env()}
            env.setdefault("LUNAMOTH_PY_BACKEND", ISOLATION_TO_BACKEND.get(meta.isolation, "sandbox"))
            log_path = meta.root / "supervisor.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab")
            self.ring.reset()
            self._stopping = False
            self._attached = False
            self._snap_cache = None
            self._client_stream_ids.clear()
            self.state, self.detail = "starting", ""
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "lunamoth.front.cli",
                "serve",
                meta.name,
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=env,
                cwd=str(APP_DIR),
                # One JSON frame per line; an attach response carries the whole
                # restored history, which easily exceeds asyncio's default 64KB
                # readline limit. Without a bigger limit, readline() raises on a
                # large chara's attach, the response never arrives, and the chat
                # hangs with an empty history. Match the WS frame ceiling (16MB).
                limit=16 * 1024 * 1024,
            )
            log.close()
            self.state, self.detail = "running", ""
            self.restart.note_started()
            self._stdout_task = asyncio.create_task(self._read_stdout(), name=f"chara-{self.name}-stdout")
            self._idle_task = asyncio.create_task(self._idle_loop(), name=f"chara-{self.name}-idle")
            self._emit_life(self.idle.life_state({"patience": 600.0, "quiet": 300}))
            return self.status()

    async def stop(self) -> dict[str, Any]:
        self._cancel_pending_restart()
        self.restart.reset()
        async with self._lock:
            self._stopping = True
            proc = self.proc
            self.state, self.detail = "stopped", ""
            self._emit_life(LifeState("waiting"))
            if self._idle_task:
                self._idle_task.cancel()
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        self._clear_running_marker()
        return self.status()

    async def ensure_started(self, *, operator: bool = False) -> None:
        # A suspended (terminal-crashed) child stays dead for INTERNAL callers
        # (the idle loop's own private_call) — they must not resurrect a broken
        # chara. Operator entrypoints pass operator=True to clear suspension.
        if self.state == "crashed" and not operator:
            raise RuntimeError(self.detail or "chara child crashed")
        st = await self.start(operator=operator)
        if st["state"] not in {"running", "starting"}:
            raise RuntimeError(st.get("detail") or st.get("state") or "chara is not running")

    async def _read_stdout(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # A frame somehow still exceeded the (large) line limit:
                    # skip the unreadable chunk rather than letting the reader
                    # die — a silently-dead reader is what hung attach before.
                    _log.warning("oversized stdout frame from %s skipped", self.name)
                    with contextlib.suppress(Exception):
                        await proc.stdout.readuntil(b"\n")
                    continue
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                rid = frame.get("id") if isinstance(frame, dict) else None
                if rid in self._pending:
                    fut = self._pending.pop(rid)
                    if not fut.done():
                        if isinstance(frame, dict) and frame.get("error"):
                            fut.set_exception(RuntimeError(str(frame["error"].get("message") or "rpc error")))
                        else:
                            fut.set_result(frame.get("result") if isinstance(frame, dict) else None)
                    continue
                if not isinstance(frame, dict):
                    continue
                completed_client_stream = False
                if rid in self._client_stream_ids:
                    self._client_stream_ids.discard(rid)
                    completed_client_stream = not self._client_stream_ids
                out = self.ring.push(frame)
                driver = self.driver_slot.current
                if driver is not None and driver.joined:
                    ok = await driver.send(out)
                    if not ok:
                        self.driver_slot.clear(driver)
                if completed_client_stream:
                    snap = await self.snapshot(silent=True)
                    self.idle.mark_idle_complete(snap or {})
                    self._emit_life(self.idle.life_state(snap or {}))
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("chara child exited"))
            self._pending.clear()
            await self._note_exit()

    async def _note_exit(self) -> None:
        proc = self.proc
        code = proc.returncode if proc is not None else None
        if code is None and proc is not None:
            with contextlib.suppress(Exception):
                code = await proc.wait()
        if self._idle_task:
            self._idle_task.cancel()
        self._clear_running_marker()
        if self._stopping or code == 0:
            self.state, self.detail = "stopped", ""
            self._emit_life(LifeState("waiting"))
            return
        # Unexpected exit: supervised restart with backoff, suspend after 3
        # consecutive crashes. crash = a visible state always — never a silent
        # restart loop: every transition emits life.state.
        delay = self.restart.note_crash()
        if self.restart.suspended:
            self.state = "crashed"
            self.detail = f"crashed (exit {code}); suspended after {self.restart.strikes} consecutive crashes"
            self._emit_life(LifeState("crashed", detail=self.detail))
            return
        self.state = "backoff"
        self.detail = f"crashed (exit {code}); restart in {int(delay)}s"
        self._emit_life(LifeState("backoff", detail=self.detail))
        self._restart_task = asyncio.create_task(
            self._restart_after(delay), name=f"chara-{self.name}-restart"
        )

    async def _restart_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._stopping or (self.proc is not None and self.proc.returncode is None):
            return
        self.state, self.detail = "starting", ""
        self._emit_life(LifeState("backoff", detail="restarting"))
        with contextlib.suppress(Exception):
            await self.start(operator=False)

    def _cancel_pending_restart(self) -> None:
        task = self._restart_task
        self._restart_task = None
        if task is not None and not task.done():
            task.cancel()

    def _clear_running_marker(self) -> None:
        with contextlib.suppress(OSError):
            self.meta.pid_path.unlink()

    async def private_call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        await self.ensure_started()
        proc = self.proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError("chara child is not running")
        self._rpc_id += 1
        rid = f"__sup:{self._rpc_id}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        raw = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, ensure_ascii=False)
        proc.stdin.write(raw.encode("utf-8") + b"\n")
        await proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=timeout)

    async def attach_background(self) -> None:
        if self._attached:
            return
        try:
            await self.private_call("attach", {"present": False}, timeout=120.0)
            self._attached = True
        except Exception as exc:  # noqa: BLE001
            self.detail = str(exc)[:240]
            self._emit_life(LifeState("backoff", detail=self.detail))
            raise

    async def snapshot(self, *, silent: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if self._snap_cache and now - self._snap_cache[0] < self.SNAPSHOT_TTL:
            return dict(self._snap_cache[1])
        if not self._attached:
            await self.attach_background()
        try:
            snap = await self.private_call("snapshot", {}, timeout=20.0)
            if isinstance(snap, dict):
                self._snap_cache = (now, snap)
                return dict(snap)
        except Exception as exc:  # noqa: BLE001
            if not silent:
                self.detail = str(exc)[:240]
        return {}

    async def forward_client_frame(self, raw: str) -> None:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            req = None
        if isinstance(req, dict) and req.get("method") == "detach":
            # A client leaving the room must not kill the resident. On the
            # child's stdio transport `detach` means "transport over, exit"
            # (the direct `serve --stdio` contract) — under the supervisor the
            # child's lifecycle is OURS, so translate the leave into a
            # presence fact and answer the client ourselves.
            with contextlib.suppress(Exception):
                if self._attached:
                    await self.private_call("presence.set", {"present": False}, timeout=10.0)
            rid = req.get("id")
            driver = self.driver_slot.current
            if rid is not None and driver is not None:
                await driver.send({"jsonrpc": "2.0", "id": rid, "result": {"ok": True, "resident": True}})
            return
        # A client frame (send/command/event) is operator activity: clear any
        # suspension and try again (operator override of a 3-strike suspend).
        await self.ensure_started(operator=True)
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("chara child is not running")
        if isinstance(req, dict):
            method = req.get("method")
            rid = req.get("id")
            if method == "send":
                self.idle.note_user()
                self._snap_cache = None
            if method == "command":
                self._snap_cache = None
            if method in {"send", "event", "idle"} and rid is not None:
                self._client_stream_ids.add(rid)
                self.idle.mark_working(method)
                self._emit_life(self.idle.life_state({}))
        proc.stdin.write(raw.encode("utf-8") + b"\n")
        await proc.stdin.drain()

    async def connect_driver(self, driver: _Driver) -> None:
        await self.driver_slot.take(driver, lambda old: old.close_superseded())
        self._send_life_to(driver)

    async def disconnect_driver(self, driver: _Driver) -> None:
        self.driver_slot.clear(driver)
        try:
            if self._attached:
                await self.private_call("presence.set", {"present": False}, timeout=10.0)
        except Exception:
            _log.debug("presence false failed for %s", self.name, exc_info=True)

    async def handle_rejoin(self, last_seq: int, driver: _Driver) -> bool:
        ok, frames = self.ring.replay_after(last_seq)
        if not ok:
            await driver.send({"jsonrpc": "2.0", "method": "rejoin.gap"})
            return False
        for frame in frames:
            if not await driver.send(frame):
                return False
        return True

    def _send_life_to(self, driver: _Driver) -> None:
        if self.life is None:
            return
        asyncio.create_task(driver.send(self.life.frame()))

    def _emit_life(self, state: LifeState) -> None:
        if self.life == state:
            return
        self.life = state
        driver = self.driver_slot.current
        if driver is not None and driver.joined:
            asyncio.create_task(driver.send(state.frame()))

    async def _idle_loop(self) -> None:
        try:
            await self.attach_background()
            snap = await self.snapshot(silent=True)
            self.idle.schedule_after(snap or {})
            self._emit_life(self.idle.life_state(snap or {}))
            while self.proc is not None and self.proc.returncode is None:
                # A run that has stayed up past the health threshold clears the
                # crash-restart strikes and backoff so an isolated crash later
                # doesn't compound toward suspension or the 1800s cap.
                self.restart.note_healthy_if_due()
                snap = await self.snapshot(silent=True)
                # Autonomous running is OFF (operator toggled it off on the
                # AUTONOMY is the chara's `mode`: live = autonomous (the full
                # lifecycle below), chat = a plain chat agent that NEVER works on
                # its own. Off → no cycles ever; entering to chat can't turn it
                # on, only the board/in-chat autonomy switch (which flips mode).
                if str(snap.get("mode") or "live") != "live":
                    self._emit_life(LifeState("waiting"))
                    await asyncio.sleep(1.0)
                    continue
                # Autonomous (mode=live). Conversation vs self-work is the
                # engagement window: present + recently spoke → conversation
                # ("waiting · back to its own work in N min", via IdleGate);
                # left or the window lapsed → self-work cycles fire below.
                st = self.idle.life_state(snap or {})
                self._emit_life(st)
                if not self.idle.ready(snap or {}) or self._client_stream_ids:
                    await asyncio.sleep(min(1.0, max(0.1, (st.next_cycle_at or time.time() + 1) - time.time())))
                    continue
                self.idle.mark_working("idle")
                self._emit_life(self.idle.life_state({}))
                try:
                    await self.private_call("idle", {}, timeout=3600.0)
                    snap = await self.snapshot(silent=True)
                    self.idle.mark_idle_complete(snap or {})
                except Exception as exc:  # noqa: BLE001
                    # If the child is gone (stop / pause / crash / app restart),
                    # its OWN lifecycle owns the state — _note_exit emits the
                    # real running/backoff/crashed state and the auto-restart
                    # handles recovery. Don't ALSO report "chara child exited"
                    # as an alarming idle backoff (that double-reporting is the
                    # "自发循环退避 · chara child exited" the operator kept seeing
                    # on every app restart). Only a model error while the child
                    # is still ALIVE is a genuine idle error.
                    # "child exited" can arrive (the stdout pipe closes) a beat
                    # BEFORE proc.wait() sets returncode — so match the message
                    # too, not just the returncode, or the race re-emits the
                    # spurious "Idle backoff · chara child exited".
                    msg0 = str(exc)
                    child_gone = (self.proc is None or self.proc.returncode is not None
                                  or "child exited" in msg0)
                    if self._stopping or child_gone:
                        return
                    msg = str(exc)
                    self.idle.mark_idle_error(msg)
                    if not permanent_model_error(msg):
                        self.idle.schedule_after(snap or {})
                self._emit_life(self.idle.life_state(snap or {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("idle loop failed for %s", self.name)


# ---- gateway children -------------------------------------------------------

# A configuration error in an adapter must NOT be retried — it is fatal until
# the operator fixes the config, unlike a transient crash. The gateway child
# exits with this code so the supervisor can distinguish the two.
GATEWAY_FATAL_EXIT = 78  # EX_CONFIG (sysexits.h): configuration error


@dataclass
class GatewayInfo:
    platform: str = ""
    # state ∈ {stopped, starting, running, backoff, fatal}. `starting` is the
    # spawn window; `fatal` is a non-retryable config error; `backoff` is a
    # transient crash waiting to restart.
    state: str = "stopped"
    detail: str = ""
    pid: int = 0
    # Human-readable reason on backoff/fatal (the last crash detail), kept
    # separate from `detail` so the web can drive a diagnostic chip. `detail`
    # is retained for back-compat with existing gateway.status callers.
    error_message: str = ""


class GatewayChild:
    """Per-chara messaging controller.

    The gateway is NOT a separate process/agent anymore: the messaging adapters
    run INSIDE the chara's ``serve --stdio`` child and share its one agent (see
    server/messaging_host.py). This controller just toggles that host on the
    running child over RPC, so a WeChat message and the desktop app talk to the
    SAME chara, in the SAME conversation. The host requires the chara child to
    be running (it hosts the shared agent), so enabling the gateway ensures the
    child is up; the supervisor's existing child-restart discipline carries the
    host's resilience (it re-reads messaging.json and re-starts on every boot)."""

    def __init__(self, meta: S.SessionMeta, supervisor: "Supervisor") -> None:
        self.meta = meta
        self.name = meta.name
        self.supervisor = supervisor
        self.info = GatewayInfo(platform=self._platform())

    def _config_path(self) -> Path:
        return self.meta.root / "messaging.json"

    def _config(self) -> dict[str, Any]:
        try:
            data = json.loads(self._config_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _platform(self) -> str:
        adapters = self._config().get("adapters")
        if isinstance(adapters, dict) and adapters:
            return ",".join(sorted(str(k) for k in adapters))
        return ""

    def enabled(self) -> bool:
        return bool(self._config().get("enabled"))

    def set_enabled(self, enabled: bool) -> None:
        path = self._config_path()
        data = self._config()
        data["enabled"] = bool(enabled)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def _apply_host_status(self, st: Any) -> None:
        if not isinstance(st, dict):
            return
        state = str(st.get("state") or "stopped")
        # needs_login = enabled & configured but waiting for an interactive QR
        # scan; it must read as its own state, not a false "running".
        self.info.state = state if state in ("running", "needs_login") else "stopped"
        detail = str(st.get("detail") or "")
        self.info.detail = detail
        self.info.error_message = detail if state not in ("running", "needs_login") else ""
        if st.get("platform"):
            self.info.platform = str(st.get("platform"))

    def _running_child(self) -> "CharaChild | None":
        child = self.supervisor.charas.get(self.name)
        if child is not None and child.proc is not None and child.proc.returncode is None:
            return child
        return None

    def status(self) -> dict[str, Any]:
        # Sync, best-effort (no RPC): used by the board summary. The gateway PANE
        # uses status_live(), which asks the in-child host for the truth.
        self.info.platform = self._platform()
        child = self._running_child()
        if not self.enabled() or child is None:
            if self.info.state not in ("backoff", "fatal"):
                self.info.state = "stopped"
        self.info.pid = int(child.proc.pid) if child and child.proc else 0
        return dataclasses.asdict(self.info)

    async def status_live(self) -> dict[str, Any]:
        """Ask the in-child messaging host for its real state (running vs
        needs_login vs stopped) — never the "child is up so it must be running"
        heuristic, which lied while an adapter sat waiting for a QR scan."""
        self.info.platform = self._platform()
        child = self._running_child()
        if not self.enabled() or child is None:
            self.info.state = "stopped"
            self.info.detail = self.info.error_message = ""
            self.info.pid = 0
            return dataclasses.asdict(self.info)
        with contextlib.suppress(Exception):
            self._apply_host_status(await child.private_call("messaging.status", {}, timeout=8.0))
        self.info.pid = int(child.proc.pid) if child.proc else 0
        return dataclasses.asdict(self.info)

    async def start(self, *, persist: bool = False) -> dict[str, Any]:
        if persist:
            self.set_enabled(True)
        self.info.platform = self._platform()
        if not self.enabled():
            self.info.state = "stopped"
            self.info.detail = self.info.error_message = ""
            return self.status()
        # The host lives in the chara child (it hosts the shared agent), so the
        # child must be running. Starting the gateway makes the chara resident;
        # it does NOT change autonomy mode (a chat-mode chara still relays).
        child = self.supervisor.child(self.name)
        try:
            await child.ensure_started(operator=True)
            st = await child.private_call("messaging.start", {}, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            self.info.state = "backoff"
            self.info.error_message = str(exc)[:240]
            self.info.detail = self.info.error_message
            return dataclasses.asdict(self.info)
        self._apply_host_status(st)
        return dataclasses.asdict(self.info)

    async def stop(self, *, persist: bool = False) -> dict[str, Any]:
        if persist:
            self.set_enabled(False)
        child = self._running_child()
        if child is not None:
            with contextlib.suppress(Exception):
                self._apply_host_status(await child.private_call("messaging.stop", {}, timeout=15.0))
        self.info.state = "stopped"
        self.info.detail = self.info.error_message = ""
        self.info.pid = 0
        return dataclasses.asdict(self.info)


# ---- static HTTP ------------------------------------------------------------

class WebHandler(http.server.SimpleHTTPRequestHandler):
    token = ""
    supervisor: Supervisor | None = None
    # Set per-server by start_http (see the `type(...)` subclass there).
    allow_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
    wildcard_bind: bool = False
    secure_cookie: bool = False  # add Secure to the auth cookie (https / proxy)
    # OPTIONAL password login for a public bind (alternative to the token URL).
    # `pw_record` is the stored PBKDF2 record (hash+salt, never plaintext) — set
    # ONLY for a non-loopback bind with a configured/generated password; None
    # keeps login inert (the local app never sees a login screen). `pw_limiter`
    # throttles failed POST /login per client IP.
    pw_record: dict | None = None
    pw_limiter: Any | None = None
    login_fail_delay: float = 1.0  # fixed delay on a wrong password (anti-brute-force)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        _log.debug("http: " + fmt, *args)

    # ---- request gating (Host allowlist + token cookie/query) ---------------

    def _host_ok(self) -> bool:
        """Reject Host headers outside the allowlist (anti DNS-rebinding)."""
        return N.host_allowed(
            self.headers.get("Host", ""), self.allow_hosts, wildcard_bind=self.wildcard_bind
        )

    def _is_secure_request(self) -> bool:
        """True when the cookie should carry Secure: a TLS reverse proxy in
        front (X-Forwarded-Proto: https) or a direct https connection."""
        if self.secure_cookie:
            return True
        return self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"

    def _auth_ok(self, url) -> bool:
        """Dual-read auth: a valid ``?token=`` query OR the auth cookie.

        On a valid ``?token=`` handshake we mint the SameSite cookie so later
        ``<img src>``/``/asset`` requests (which can't send the query) pass on
        the cookie alone. The actual Set-Cookie is emitted by send_auth_cookie,
        called from the response path once a 200 is going out."""
        cookie = self.headers.get("Cookie", "")
        if N.request_authed(url.query, cookie, self.token):
            # Mint/refresh the cookie when the token arrived via the query.
            if N.token_from_query(url.query):
                self._pending_set_cookie = N.auth_cookie_header(
                    self.token, secure=self._is_secure_request()
                )
            return True
        return False

    # Raster only: no svg/html — an attacker-controlled SVG served same-origin
    # with image/svg+xml would be a stored-XSS vector. Card avatars use inline
    # SVG via a separate sanitized data-URI path, never this route.
    _ASSET_MIME = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".gif": "image/gif"}

    def end_headers(self) -> None:
        if not getattr(self, "_skip_no_store", False):
            self.send_header("Cache-Control", "no-store")
        pending = getattr(self, "_pending_set_cookie", "")
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_set_cookie = ""
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _client_ip(self) -> str:
        """Per-client key for the login rate limit. Trust ``X-Forwarded-For`` ONLY
        when the socket peer is loopback (the reverse proxy runs on the same host).
        A direct connection to the published port could spoof XFF to mint a fresh
        rate-limit bucket per request and defeat the per-IP brute-force throttle —
        so for a non-loopback peer use the real peer IP and ignore XFF."""
        try:
            peer = self.client_address[0]
        except (AttributeError, IndexError):
            return "?"
        if N.is_loopback_host(peer):
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return peer

    def _handle_login(self) -> None:
        """POST /login {password} → mint the SAME auth cookie on success.

        Pre-auth (the login form must reach it without a token). Defends with a
        per-IP token-bucket throttle + a fixed delay on every failure. Mints the
        token cookie via netsec.auth_cookie_header — the SAME cookie the
        ?token= handshake sets — so the rest of the app is unchanged after login.
        """
        if self.pw_record is None:
            # Login not enabled (loopback / no password). Behave as if absent.
            self.send_error(404)
            return
        ip = self._client_ip()
        limiter = self.pw_limiter
        if limiter is not None and not limiter.allow(ip):
            self._send_json(429, {"error": "too many attempts"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(max(0, length)) if length > 0 else b""
        try:
            req = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            req = {}
        password = str(req.get("password") or "")
        if AUTHPW.verify_password(self.pw_record, password):
            cookie = N.auth_cookie_header(self.token, secure=self._is_secure_request())
            if not cookie:  # an unsafe --token can't ride a cookie; refuse cleanly
                self._send_json(500, {"error": "token not cookie-safe"})
                return
            self._pending_set_cookie = cookie
            self.send_response(204)
            self.end_headers()
            return
        time.sleep(self.login_fail_delay)  # fixed delay slows brute-force
        self._send_json(401, {"error": "invalid password"})

    def _card_roots(self) -> list[Path]:
        return [H.bundled_cards_dir().resolve(), H.user_cards_dir().resolve()]

    def _session_roots(self) -> list[Path]:
        # The session ROOT tree — used ONLY to locate raster card-art sidecars
        # (a living chara's frozen card copies sprite.png etc. to its session root)
        # and to flag a file as session-volatile for caching. NON-image files are
        # NOT served from here (see _readable_session_roots) — that is what keeps
        # config.json / session.json / transcript.db off this route.
        try:
            return [m.root.resolve() for m in S.list_sessions()]
        except Exception:  # noqa: BLE001 - serving must not depend on session health
            return []

    def _readable_session_roots(self) -> list[Path]:
        """The ONLY session subtrees /asset may hand out NON-image files from: the
        chara's workspace and the read-only assets shelf (send_file docs / works /
        reference material). The session ROOT — which holds config.json (the provider
        api_key!), session.json, env_status.json and transcript.db — is deliberately
        absent, so those secrets are unreachable via this route."""
        out: list[Path] = []
        try:
            for m in S.list_sessions():
                sb = m.sandbox_dir.resolve()
                out += [(sb / "workspace").resolve(), (sb / "assets").resolve()]
        except Exception:  # noqa: BLE001
            return []
        return out

    # Never serve these by name, wherever they sit — session secrets / state / logs.
    _ASSET_DENY_NAMES = frozenset({"config.json", "session.json", "env_status.json", "presence.json"})

    def _serve_asset(self, url) -> None:
        """Serve a file by absolute path. Two narrow lanes:

        - Raster card art (png/webp/jpg/gif): from the card decks (cacheable) or a
          session's art sidecars / workspace images (no-store). Images carry no
          secrets, so the broad session tree is acceptable here.
        - Non-image (a doc the chara sent with send_file): FORCED DOWNLOAD, and ONLY
          from a session's sandbox/workspace or sandbox/assets. The session ROOT is
          NOT a read root for non-images — that is what keeps config.json (the
          provider api_key) and transcript.db off this route. A hard name denylist
          backs it up.
        """
        raw = (parse_qs(url.query).get("p") or [""])[0]
        try:
            target = Path(unquote(raw)).resolve()
        except Exception:  # noqa: BLE001
            self.send_error(404); return
        # Hard denylist: session secrets / state / transcript / pidfiles, by name,
        # regardless of where they resolve — defense in depth behind the lane split.
        if (target.name in self._ASSET_DENY_NAMES
                or target.name.startswith("transcript.db")
                or target.suffix in (".pid", ".log")):
            self.send_error(404); return
        if not target.is_file():
            self.send_error(404); return

        def under(roots: list[Path]) -> bool:
            return any(target == r or r in target.parents for r in roots)

        mime = self._ASSET_MIME.get(target.suffix.lower())
        if mime is not None:
            # Raster image lane: card decks (cacheable) or session art/images (no-store).
            session_roots = self._session_roots()
            if not under(self._card_roots() + session_roots):
                self.send_error(404); return
            in_session = under(session_roots)
            disposition = None
            cache = "no-store" if in_session else "public, max-age=86400"
        else:
            # Non-image lane: ONLY a session's workspace/assets, forced download.
            if not under(self._readable_session_roots()):
                self.send_error(404); return
            mime = "application/octet-stream"
            safe = target.name.replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")
            disposition = f'attachment; filename="{safe}"'
            cache = "no-store"
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404); return
        self._skip_no_store = True
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._skip_no_store = False
        self._pending_set_cookie = ""
        if not self._host_ok():
            self.send_error(403, "host not allowed")
            return
        url = urlsplit(self.path)
        # The SPA shell + its hashed bundle load pre-auth (they carry no secrets
        # and must run to perform the ?token= handshake). Everything else —
        # /asset and any other path — requires the token (query) or auth cookie.
        if not _is_preauth_path(url.path) and not self._auth_ok(url):
            self.send_error(401, "authentication required")
            return
        if url.path == "/authinfo":
            # Pre-auth probe: should the client show a login form? No secrets —
            # just a boolean. True only when a password is configured (a public
            # bind) AND the request is NOT already authenticated. The "already
            # authed" clause is essential: after a successful /login the user
            # carries the lm_auth cookie but still has no #token=, so without it
            # /authinfo would keep saying login:true and the Gate would loop
            # forever (the user could never enter). Loopback ⇒ pw_record=None ⇒ false.
            cookie = self.headers.get("Cookie", "")
            already = N.request_authed(url.query, cookie, self.token)
            self._send_json(200, {"login": self.pw_record is not None and not already})
            return
        if url.path == "/auth":
            # Boot handshake: the SPA loads its token from the URL hash (never sent
            # to the server), so the shell GET mints no cookie. The client calls
            # GET /auth?token=… once at boot — _auth_ok above validated it and
            # queued the Set-Cookie, so subsequent tokenless <img>/asset requests
            # authenticate via the SameSite cookie. 204, no body.
            self.send_response(204)
            self.end_headers()
            return
        if url.path == "/asset":
            self._serve_asset(url)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._skip_no_store = False  # never inherit an asset GET's cache flag (keep-alive safety)
        self._pending_set_cookie = ""
        if not self._host_ok():
            self.send_error(403, "host not allowed")
            return
        url = urlsplit(self.path)
        if url.path == "/login":
            # Pre-auth: the login form is exactly how an un-tokened client gets
            # authed. Inert (404) unless a password is configured (public bind).
            self._handle_login()
            return
        if not self._auth_ok(url):
            self.send_error(403)
            return
        if url.path == "/rpc":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(max(0, length))
            try:
                req = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                payload = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                dispatcher = H.HubDispatcher(lambda frame: True, supervisor=self.supervisor)
                payload = dispatcher.dispatch(req) or {"jsonrpc": "2.0", "id": None, "result": None}
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if url.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        name = Path(self.headers.get("X-Filename") or "card.json").name
        if length <= 0 or length > UPLOAD_MAX or Path(name).suffix.lower() not in (".json", ".png"):
            self.send_error(400, "expected a .json or .png card under 8 MB")
            return
        body = self.rfile.read(length)
        # A .json that parses as a standalone world book is stored aside and
        # reported as kind="world" so the deck can offer "merge into card X".
        payload = json.dumps(H.store_upload(name, body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_http(
    host: str,
    port: int,
    token: str,
    supervisor: Supervisor | None = None,
    *,
    allow_hosts: frozenset[str] | None = None,
    secure_cookie: bool = False,
    pw_record: dict | None = None,
    pw_limiter: Any | None = None,
) -> http.server.ThreadingHTTPServer:
    attrs = {
        "token": token,
        "supervisor": supervisor,
        "allow_hosts": allow_hosts if allow_hosts is not None else N.allowed_hosts(host),
        "wildcard_bind": N.is_wildcard_host(host),
        "secure_cookie": bool(secure_cookie),
        "pw_record": pw_record,
        # One limiter per server (shared across the handler threads); only made
        # when login is enabled (a public bind with a password).
        "pw_limiter": pw_limiter if pw_limiter is not None
        else (AUTHPW.LoginRateLimiter() if pw_record is not None else None),
    }
    handler = type("Handler", (WebHandler,), attrs)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="desktop-http", daemon=True)
    thread.start()
    return server


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _reachable_ips(host: str) -> list[str]:
    """Best-effort list of addresses a remote browser could use to reach a
    non-loopback bind. For a wildcard bind, enumerate the host's own IPs; for a
    specific host, just that host."""
    if not N.is_wildcard_host(host):
        return [host]
    ips: list[str] = []
    with contextlib.suppress(Exception):
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127.") and ip != "::1":
                ips.append(ip)
    return ips or [host]


# ---- supervisor -------------------------------------------------------------

class Supervisor:
    def __init__(
        self,
        host: str,
        http_port: int,
        ws_port: int,
        token: str,
        *,
        allow_hosts: list[str] | None = None,
        secure_cookie: bool = False,
        pw_record: dict | None = None,
    ) -> None:
        self.host = host
        self.http_port = int(http_port)
        self.ws_port = int(ws_port)  # 0 ⇒ OS-assigned; resolved in serve()
        self.token = token
        # OPTIONAL password-login record (public bind only); None ⇒ login inert.
        self.pw_record = pw_record
        # Host/Origin allow set (anti DNS-rebinding / CSWSH). Loopback + bound
        # host always; `allow_hosts` names extra reachable hosts for a proxy.
        self.allow_hosts = N.allowed_hosts(host, allow_hosts)
        self.wildcard_bind = N.is_wildcard_host(host)
        self.secure_cookie = bool(secure_cookie)
        self.charas: dict[str, CharaChild] = {}
        self.gateways: dict[str, GatewayChild] = {}
        self._pty_bridges: set[PtyBridge] = set()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._shutdown = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._canary = ResourceCanary()
        self._shutdown_ctx: dict[str, Any] | None = None
        self._installed_signals: list[int] = []

    def child(self, name: str) -> CharaChild:
        meta = S.load_session(name)
        if meta is None:
            raise RuntimeError(f"no chara named {name!r}")
        child = self.charas.get(name)
        if child is None:
            child = CharaChild(meta, self)
            self.charas[name] = child
        return child

    @staticmethod
    def is_autonomous(meta: S.SessionMeta) -> bool:
        """Autonomy is the chara's persisted `mode`: live = autonomous, chat =
        plain chat agent. This is THE single autonomy switch (board, in-chat,
        and TUI all flip it); there is no separate pause flag."""
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
            return str(cfg.get("mode") or "live") == "live"
        except (OSError, json.JSONDecodeError):
            return True  # default live

    @staticmethod
    def set_mode_on_disk(meta: S.SessionMeta, mode: str) -> None:
        """Persist mode (live|chat) into the session config — used for a chara
        whose child isn't running. A running child is told via its /mode
        command so the live agent + snapshot update too."""
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        cfg["mode"] = "live" if mode == "live" else "chat"
        cfg.pop("api_key", None)  # SEC-2: never persist the secret into a session config
        meta.config_path.parent.mkdir(parents=True, exist_ok=True)
        meta.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def gateway(self, name: str) -> GatewayChild:
        meta = S.load_session(name)
        if meta is None:
            raise RuntimeError(f"no chara named {name!r}")
        gw = self.gateways.get(name)
        if gw is None:
            gw = GatewayChild(meta, self)
            self.gateways[name] = gw
        return gw

    async def start_chara(self, name: str) -> dict[str, Any]:
        # Board "on" = autonomous + resident: mode live, start the child.
        child = self.child(name)
        Supervisor.set_mode_on_disk(child.meta, "live")
        return await child.start()

    async def stop_chara(self, name: str) -> dict[str, Any]:
        # Board "off" = not autonomous + stopped (saves tokens): mode chat,
        # stop the child. Entering to chat later starts a plain chat agent.
        child = self.child(name)
        Supervisor.set_mode_on_disk(child.meta, "chat")
        return await child.stop()

    async def set_autonomy(self, name: str, on: bool) -> dict[str, Any]:
        """Flip autonomy (mode live|chat) WITHOUT killing the chat you're in —
        the in-chat switch. A running child is told via its /mode command so the
        live agent + snapshot update immediately; a stopped child gets the
        config write (and is started if turning autonomy on)."""
        child = self.child(name)
        mode = "live" if on else "chat"
        Supervisor.set_mode_on_disk(child.meta, mode)
        if child.proc is not None and child.proc.returncode is None:
            with contextlib.suppress(Exception):
                await child.private_call("command", {"line": f"/mode {mode}"}, timeout=10.0)
            child._snap_cache = None
            if on:
                snap = await child.snapshot(silent=True)
                child.idle.schedule_after(snap or {})  # resume from now, not instantly
            else:
                child._emit_life(LifeState("waiting"))
        elif on:
            await child.start()
        return child.status()

    def chara_status(self, name: str) -> dict[str, Any] | None:
        child = self.charas.get(name)
        return child.status() if child else None

    def life_state(self, name: str) -> dict[str, Any] | None:
        child = self.charas.get(name)
        return dataclasses.asdict(child.life) if child and child.life else None

    async def start_gateway(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        return await self.gateway(name).start(persist=persist)

    async def stop_gateway(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        return await self.gateway(name).stop(persist=persist)

    def gateway_status(self, name: str) -> dict[str, Any] | None:
        gw = self.gateways.get(name)
        if gw is None:
            meta = S.load_session(name)
            if meta is None:
                return None
            gw = self.gateway(name)
        return gw.status()

    async def gateway_status_live(self, name: str) -> dict[str, Any] | None:
        meta = S.load_session(name)
        if meta is None:
            return None
        return await self.gateway(name).status_live()

    async def gateways_all_live(self) -> dict[str, Any]:
        """Live gateway status for EVERY chara — the global gateway view's one
        source of truth (the same status_live() the per-chara pane uses, so the
        overview and the in-chara panel never disagree)."""
        out: list[dict[str, Any]] = []
        for meta in S.list_sessions():
            gw = self.gateway(meta.name)
            with contextlib.suppress(Exception):
                status = await gw.status_live()
                out.append({"name": meta.name, "enabled": gw.enabled(), "gateway": status})
        return {"gateways": out}

    async def bootstrap_gateways(self) -> None:
        for meta in S.list_sessions():
            gw = self.gateway(meta.name)
            if gw.enabled():
                await gw.start(persist=False)

    async def serve(self, *, open_browser: bool = True) -> int:
        if not WEB_DIR.is_dir() or not any(WEB_DIR.iterdir()):
            print(
                f"error: the web UI is not built at {WEB_DIR}\n"
                "       run: cd apps/web && npm install && npm run build",
                file=sys.stderr,
            )
            return 1
        self.loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self._canary.start()
        # Bind the WS first so a `--ws-port 0` (OS-assigned) port is known before
        # we bake it into the URL/daemon.json. The HTTP port is started here too
        # so a conflict surfaces with attribution rather than a raw traceback.
        try:
            ws_server = await self._start_ws()
        except OSError as exc:
            print(f"error: could not bind the WebSocket port: {exc}", file=sys.stderr)
            return 1
        try:
            self.ws_port = ws_server.sockets[0].getsockname()[1]
        except (IndexError, AttributeError):
            pass
        try:
            self._httpd = start_http(
                self.host, self.http_port, self.token, self,
                allow_hosts=self.allow_hosts, secure_cookie=self.secure_cookie,
                pw_record=self.pw_record,
            )
            self.http_port = self._httpd.server_address[1]
        except OSError:
            holder = N.describe_port_holder(self.http_port)
            print(
                f"error: HTTP port {self.http_port} held by {holder}\n"
                "       stop it, or pass --port <other> (or --port 0 for any free port).",
                file=sys.stderr,
            )
            ws_server.close()
            with contextlib.suppress(Exception):
                await ws_server.wait_closed()
            return 1
        self._warn_on_public_bind()
        url = f"http://{self.host}:{self.http_port}/#token={self.token}&ws={self.ws_port}"
        # The resident daemon child owns daemon.json: rewrite it with the
        # resolved ports (the WS port was OS-assigned) so `lunamoth start NAME`
        # and `--connect` read the live values. A foreground (ephemeral) run
        # must NOT clobber a running daemon's metadata.
        if os.getenv("LUNAMOTH_DAEMON_CHILD"):
            write_daemon_json(os.getpid(), self.http_port, self.ws_port, self.token)
        print(f"LunaMoth desktop: {url}", file=sys.stderr, flush=True)
        print("life.state: supervisor emits life.state frames on client connect and transitions", file=sys.stderr, flush=True)
        if open_browser:
            self._open_later(url)
        await self.bootstrap_gateways()
        try:
            await self._shutdown.wait()
        finally:
            ws_server.close()
            with contextlib.suppress(Exception):
                await ws_server.wait_closed()
            await self.shutdown()
        return 0

    def _warn_on_public_bind(self) -> None:
        """A non-loopback bind exposes the chara's shell + tools to the network.
        Warn prominently and print the reachable URLs (AstrBot server.py:642-677).
        The token is the gate — serve() refuses a wildcard bind without one
        (enforced in cmd_desktop), so a reachable instance is always authed."""
        if N.is_loopback_host(self.host):
            return
        bar = "=" * 60
        lines = [
            bar,
            "  SECURITY: LunaMoth is bound to a NON-LOOPBACK address.",
            f"  Anyone who can reach {self.host}:{self.http_port} and holds the",
            "  token can drive this chara's shell, files, and tools.",
            "  Put a TLS reverse proxy in front; never expose it raw on the",
            "  public internet. The URL below carries the access token.",
            bar,
        ]
        for line in lines:
            print(line, file=sys.stderr, flush=True)
        for ip in _reachable_ips(self.host):
            print(f"  reachable: http://{ip}:{self.http_port}/", file=sys.stderr, flush=True)
        if self.pw_record is not None:
            print(
                "  password login is ALSO enabled: bookmark the host and log in "
                "with the password (no token URL needed).",
                file=sys.stderr, flush=True,
            )

    def _open_later(self, url: str) -> None:
        def work() -> None:
            time.sleep(0.4)
            if sys.platform == "darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import webbrowser

                webbrowser.open(url)

        threading.Thread(target=work, name="desktop-open", daemon=True).start()

    async def _start_ws(self) -> Any:
        """Bind the WebSocket server (port 0 ⇒ OS-assigned) and return it so the
        caller can read the chosen port and own the lifecycle."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError("the desktop needs websockets. Install with: uv sync --extra server") from exc
        handler = functools.partial(self._ws_entry)
        return await websockets.serve(
            handler, self.host, self.ws_port, max_size=16 * 1024 * 1024
        )

    def _origin_ok(self, ws: Any) -> bool:
        """Reject a cross-origin WS even with a valid token (anti-CSWSH). A
        missing Origin (native clients / Electron / CLI tunnels) is allowed —
        the token gates those; a PRESENT foreign Origin is the browser attack."""
        origin = ""
        request = getattr(ws, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if headers is not None:
                with contextlib.suppress(Exception):
                    origin = headers.get("Origin", "") or headers.get("origin", "") or ""
        return N.origin_allowed(origin, self.allow_hosts, wildcard_bind=self.wildcard_bind)

    def _ws_cookie(self, ws: Any) -> str:
        """The Cookie header from the WS handshake (browsers send same-origin
        cookies on the upgrade request) — so a password-login user, who reached the
        bare bookmark with no ?token=, authenticates the WS via the lm_auth cookie."""
        request = getattr(ws, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if headers is not None:
                with contextlib.suppress(Exception):
                    return headers.get("Cookie", "") or headers.get("cookie", "") or ""
        return ""

    async def _ws_entry(self, ws: Any, path: str = "") -> None:
        path = _path_from_ws(ws, path)
        # Origin is checked FIRST (anti-CSWSH) — a cross-origin browser WS is
        # rejected 4403 before auth, so accepting the cookie below is safe.
        if not self._origin_ok(ws):
            await _close_ws(ws, 4403, "origin not allowed")
            return
        # Dual-read like the HTTP gate: ?token= (Electron/SSH/token-URL) OR the
        # lm_auth cookie (password-login users, whose bookmark has no token).
        if not N.request_authed(urlsplit(path).query, self._ws_cookie(ws), self.token):
            await _close_ws(ws, 4401, "authentication required")
            return
        route = urlsplit(path).path
        if route in ("", "/", "/hub"):
            await self._handle_hub(ws)
        elif route.startswith("/chara/"):
            rest = route[len("/chara/"):].strip("/")
            if rest.endswith("/pty"):
                await self._handle_pty(ws, rest[: -len("/pty")].strip("/"), path)
            else:
                await self._handle_chara(ws, rest)
        else:
            await _close_ws(ws, 4404, "unknown endpoint")

    async def _handle_hub(self, ws: Any) -> None:
        sink = _WSSink(ws, asyncio.get_running_loop())
        dispatcher = H.HubDispatcher(sink.write, supervisor=self)
        loop = asyncio.get_running_loop()
        try:
            await sink.write_async({"jsonrpc": "2.0", "method": "hello", "params": {"role": "hub"}})
            while True:
                try:
                    raw = await _recv_text(ws)
                except Exception:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    await sink.write_async({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                    continue
                loop.create_task(self._dispatch_hub_async(dispatcher, req, sink))
        finally:
            sink.close()

    async def _dispatch_hub_async(self, dispatcher: H.HubDispatcher, req: Any, sink: _WSSink) -> None:
        resp = await asyncio.to_thread(dispatcher.dispatch, req)
        if resp is not None:
            await sink.write_async(resp)

    async def _handle_chara(self, ws: Any, name: str) -> None:
        if S.load_session(name) is None:
            await _close_ws(ws, 4404, "no such chara")
            return
        child = self.child(name)
        try:
            # Opening the chara's room is operator activity: clear suspension.
            await child.ensure_started(operator=True)
        except Exception as exc:  # noqa: BLE001
            await _close_ws(ws, 4423, str(exc)[:120])
            return
        driver = _Driver(ws)
        await child.connect_driver(driver)
        first = True
        try:
            while True:
                try:
                    raw = (await _recv_text(ws)).strip()
                except Exception:
                    break
                if not raw:
                    continue
                if first:
                    first = False
                    try:
                        req = json.loads(raw)
                    except json.JSONDecodeError:
                        req = None
                    if isinstance(req, dict) and req.get("method") == "rejoin":
                        params = req.get("params") if isinstance(req.get("params"), dict) else {}
                        ok = await child.handle_rejoin(int(params.get("last_seq") or 0), driver)
                        driver.joined = ok
                        if ok:
                            with contextlib.suppress(Exception):
                                await child.private_call("presence.set", {"present": True}, timeout=10.0)
                        else:
                            driver.joined = True
                        continue
                    driver.joined = True
                await child.forward_client_frame(raw)
        finally:
            await child.disconnect_driver(driver)
            await _close_ws(ws)

    async def _handle_pty(self, ws: Any, name: str, path: str) -> None:
        """An operator shell inside the chara's isolation jail, over one WS.

        The shell targets the chara's HOME (its sandbox workspace), not the
        agent: no ensure_started() — the PTY works while the chara child is
        stopped or resting, and a PTY is NOT a driver (no rejoin/seq).
        """
        # OPEN QUESTION (curriculum): should a chara be able to sense that an
        # operator shell entered its home? For now the chara is NOT notified
        # and the transcript is untouched — the audit trail is the only record.
        meta = S.load_session(name)
        if meta is None:
            await _close_ws(ws, 4404, "no such chara")
            return
        qs = parse_qs(urlsplit(path).query)

        def _dim(key: str, default: int) -> int:
            try:
                return int((qs.get(key) or [default])[0])
            except (TypeError, ValueError):
                return default

        workspace = meta.sandbox_dir / "workspace"
        allow_network, writable = I.runtime_permissions(meta.sandbox_dir)
        audit = AuditLog(meta.sandbox_dir / "logs" / "audit.jsonl")
        try:
            argv, cwd, env = I.interactive_shell_argv(
                meta.isolation,
                workspace,
                allow_network=allow_network,
                writable_paths=writable,
            )
            bridge = PtyBridge.spawn(argv, cwd=cwd, env=env, cols=_dim("cols", 80), rows=_dim("rows", 24))
        except (I.JailUnavailableError, OSError) as exc:
            # No degrade, no silent fallback: the operator sees WHY in the
            # terminal, then the socket closes with an error code.
            _log.warning("pty for %s failed to start: %s", name, exc)
            with contextlib.suppress(Exception):
                await ws.send(f"\r\nshell unavailable: {exc}\r\n")
            await _close_ws(ws, 1011, str(exc)[:120])
            return
        self._pty_bridges.add(bridge)
        audit.write("pty_open", chara=name, isolation=meta.isolation, pid=bridge.pid)
        _log.info("pty open for %s (isolation=%s, pid=%d)", name, meta.isolation, bridge.pid)
        loop = asyncio.get_running_loop()

        async def pump_pty_to_ws() -> None:
            while True:
                chunk = await loop.run_in_executor(None, bridge.read, _PTY_READ_TIMEOUT)
                if chunk is None:  # EOF: child exited
                    await _close_ws(ws, 1000, "shell exited")
                    return
                if not chunk:
                    continue
                try:
                    await ws.send(chunk)  # binary frame: raw bytes, never JSON
                except Exception:
                    return

        reader = asyncio.create_task(pump_pty_to_ws(), name=f"pty-{name}-reader")
        try:
            while True:
                try:
                    msg = await ws.recv()
                except Exception:
                    break
                raw = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
                if not raw:
                    continue
                match = _PTY_RESIZE_RE.fullmatch(raw)
                if match:  # consumed server-side, never written to the shell
                    bridge.resize(int(match.group(1)), int(match.group(2)))
                    continue
                bridge.write(raw)
        finally:
            reader.cancel()
            # CancelledError is a BaseException: plain suppress(Exception)
            # would let it abort the rest of this cleanup.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            bridge.close()
            self._pty_bridges.discard(bridge)
            audit.write("pty_close", chara=name, pid=bridge.pid, exit=bridge.returncode)
            _log.info("pty closed for %s (pid=%d, exit=%s)", name, bridge.pid, bridge.returncode)
            await _close_ws(ws)

    def _install_signal_handlers(self) -> None:
        """Record shutdown forensics on the loop's own signal path (async-safe).

        ``loop.add_signal_handler`` runs the callback in the event loop, so we
        can snapshot *why* we're dying and set ``_shutdown`` without touching
        re-entrancy-unsafe signal internals. Best-effort: on platforms/threads
        where it isn't available (e.g. a non-main-thread test loop) we leave the
        plain handler installed by desktop.py in place.
        """
        loop = self.loop
        if loop is None:
            return
        for signame in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, functools.partial(self.request_shutdown, signal_num=int(sig)))
                self._installed_signals.append(int(sig))
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                _log.debug("could not install async signal handler for %s", signame, exc_info=True)

    def _remove_signal_handlers(self) -> None:
        loop = self.loop
        if loop is None:
            return
        for sig in self._installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, OSError):
                loop.remove_signal_handler(sig)
        self._installed_signals.clear()

    async def shutdown(self) -> None:
        # Forensics first: a durable "this is why lunamothd is exiting" line so a
        # week-long-daemon death isn't a silent gap in the log. Never blocks.
        ctx = self._shutdown_ctx or snapshot_shutdown_context()
        with contextlib.suppress(Exception):
            _log.info("[SHUTDOWN] %s", format_shutdown_context(ctx))
        self._remove_signal_handlers()
        if self._httpd is not None:
            self._httpd.shutdown()
        for bridge in list(self._pty_bridges):
            with contextlib.suppress(Exception):
                bridge.close()
        self._pty_bridges.clear()
        for gw in list(self.gateways.values()):
            with contextlib.suppress(Exception):
                await gw.stop(persist=False)
        for child in list(self.charas.values()):
            with contextlib.suppress(Exception):
                await child.stop()
        # Final RSS line after teardown, so "last RSS before exit" is in the log.
        self._canary.stop()

    def request_shutdown(self, signal_num: int | None = None) -> None:
        # Snapshot the trigger the first time we're asked to stop — cheap,
        # non-blocking, and the most useful single forensic fact.
        if self._shutdown_ctx is None:
            with contextlib.suppress(Exception):
                self._shutdown_ctx = snapshot_shutdown_context(signal_num)
        self._shutdown.set()


# ---- daemon metadata --------------------------------------------------------

def daemon_json_path() -> Path:
    return S.lunamoth_home() / "daemon.json"


def daemon_log_path() -> Path:
    return S.lunamoth_home() / "logs" / "daemon.log"


def write_daemon_json(pid: int, http_port: int, ws_port: int, token: str) -> Path:
    path = daemon_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"pid": int(pid), "http_port": int(http_port), "ws_port": int(ws_port), "token": token}, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def read_daemon_json() -> dict[str, Any]:
    try:
        data = json.loads(daemon_json_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def daemon_alive(data: dict[str, Any] | None = None) -> bool:
    data = data or read_daemon_json()
    try:
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        # On POSIX, kill(0) succeeds for zombies; check /proc when available so
        # status/stop do not report a dead daemon as alive on Linux.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                parts = stat.read_text(encoding="utf-8", errors="replace").split()
                if len(parts) > 2 and parts[2] == "Z":
                    return False
            except OSError:
                pass
        return True
    except (OSError, ValueError, TypeError):
        return False


def daemon_status() -> dict[str, Any]:
    data = read_daemon_json()
    data["alive"] = daemon_alive(data)
    data["path"] = str(daemon_json_path())
    data["log"] = str(daemon_log_path())
    data["home"] = str(S.lunamoth_home())
    return data


def stop_daemon_process(grace: float = 8.0) -> bool:
    data = read_daemon_json()
    if not daemon_alive(data):
        with contextlib.suppress(OSError):
            daemon_json_path().unlink()
        return False
    pid = int(data["pid"])
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not daemon_alive(data):
            with contextlib.suppress(OSError):
                daemon_json_path().unlink()
            return True
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        daemon_json_path().unlink()
    return True
