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
from urllib.parse import parse_qs, urlsplit

from ..obs.audit import AuditLog
from ..session import isolation as I
from ..session import sessions as S
from . import hub as H
from .pty import PtyBridge
from .ws import _WSSink, _close_ws, _path_from_ws, _recv_text, query_auth_ok

_log = logging.getLogger("lunamoth.server.supervisor")

APP_DIR = Path(__file__).resolve().parents[3]
WEB_DIR = Path(__file__).resolve().parents[1] / "front" / "web"
UPLOAD_MAX = 8 * 1024 * 1024
ISOLATION_TO_BACKEND = {"dir": "local", "sandbox": "sandbox", "docker": "docker"}

# Whole-frame resize escape consumed server-side by the PTY endpoint
# (hermes shape): \x1b[RESIZE:<cols>;<rows>] — full-match only.
_PTY_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_TIMEOUT = 0.2


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
    if msg.startswith(("HTTP 401", "HTTP 403", "HTTP 404")):
        return True
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
                await self.ws.send(raw)
                return True
            except Exception:
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
                line = await proc.stdout.readline()
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
                    child_gone = self.proc is None or self.proc.returncode is not None
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
    def __init__(self, meta: S.SessionMeta, supervisor: "Supervisor") -> None:
        self.meta = meta
        self.name = meta.name
        self.supervisor = supervisor
        self.info = GatewayInfo(platform=self._platform())
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        # Same supervised-restart discipline as CharaChild: a run that stays up
        # past the health threshold resets the backoff so an isolated crash a
        # week apart doesn't compound toward the 1800s cap. (No 3-strike
        # suspension here — a gateway has always restarted unboundedly; the new
        # `fatal` state covers the non-retryable config-error case instead.)
        self.backoff = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=10**9)

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

    def status(self) -> dict[str, Any]:
        self.info.platform = self._platform()
        if self.proc is not None and self.proc.returncode is None:
            self.info.pid = int(self.proc.pid)
        return dataclasses.asdict(self.info)

    def set_enabled(self, enabled: bool) -> None:
        path = self._config_path()
        data = self._config()
        data["enabled"] = bool(enabled)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    async def start(self, *, persist: bool = False) -> dict[str, Any]:
        if persist:
            self.set_enabled(True)
        self._stop_requested = False
        if self.proc is not None and self.proc.returncode is None:
            self.info.state = "running"
            self.info.detail = ""
            self.info.error_message = ""
            self.info.pid = self.proc.pid
            return self.status()
        # An explicit start is an operator override: clear a prior fatal/backoff.
        self.backoff.reset()
        self._task = asyncio.create_task(self._run_supervised(), name=f"gateway-{self.name}")
        return self.status()

    async def stop(self, *, persist: bool = False) -> dict[str, Any]:
        if persist:
            self.set_enabled(False)
        self._stop_requested = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        self.info.state = "stopped"
        self.info.detail = ""
        self.info.error_message = ""
        self.info.pid = 0
        return self.status()

    async def _run_supervised(self) -> None:
        while not self._stop_requested and self.enabled():
            self.info.platform = self._platform()
            self.info.state = "starting"
            self.info.detail = ""
            self.info.error_message = ""
            env = {**os.environ, **self.meta.env()}
            env.setdefault("LUNAMOTH_PY_BACKEND", ISOLATION_TO_BACKEND.get(self.meta.isolation, "sandbox"))
            log_path = self.meta.root / "gateway.log"
            log = log_path.open("ab")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "lunamoth.front.cli",
                    "gateway",
                    self.name,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    cwd=str(APP_DIR),
                )
                self.info.pid = self.proc.pid
                self.info.state = "running"
                self.backoff.note_started()
                code = await self.proc.wait()
            finally:
                log.close()
            self.info.pid = 0
            if self._stop_requested or not self.enabled() or code == 0:
                self.info.state = "stopped"
                self.info.detail = ""
                self.info.error_message = ""
                return
            if code == GATEWAY_FATAL_EXIT:
                # A configuration error in an adapter: do NOT retry. Stays fatal
                # until the operator fixes the config and explicitly restarts.
                self.info.state = "fatal"
                self.info.error_message = "gateway configuration error; not retrying"
                self.info.detail = self.info.error_message
                return
            # A healthy run resets the ladder (handled inside note_crash too if
            # the idle poll never ran); an isolated crash restarts at the floor.
            delay = self.backoff.note_crash()
            self.info.state = "backoff"
            self.info.error_message = f"crashed (exit {code}); restart in {int(delay)}s"
            self.info.detail = self.info.error_message
            await asyncio.sleep(delay)
        if not self.enabled():
            self.info.state = "stopped"
            self.info.detail = ""
            self.info.error_message = ""


# ---- static HTTP ------------------------------------------------------------

class WebHandler(http.server.SimpleHTTPRequestHandler):
    token = ""
    supervisor: Supervisor | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        _log.debug("http: " + fmt, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlsplit(self.path)
        qs = parse_qs(url.query)
        token = (qs.get("token") or [""])[0]
        if not self.token or token != self.token:
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


def start_http(host: str, port: int, token: str, supervisor: Supervisor | None = None) -> http.server.ThreadingHTTPServer:
    handler = type("Handler", (WebHandler,), {"token": token, "supervisor": supervisor})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="desktop-http", daemon=True)
    thread.start()
    return server


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


# ---- supervisor -------------------------------------------------------------

class Supervisor:
    def __init__(self, host: str, http_port: int, ws_port: int, token: str) -> None:
        self.host = host
        self.http_port = int(http_port)
        self.ws_port = int(ws_port)
        self.token = token
        self.charas: dict[str, CharaChild] = {}
        self.gateways: dict[str, GatewayChild] = {}
        self._pty_bridges: set[PtyBridge] = set()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._shutdown = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

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

    async def bootstrap_gateways(self) -> None:
        for meta in S.list_sessions():
            gw = self.gateway(meta.name)
            if gw.enabled():
                await gw.start(persist=False)

    async def serve(self, *, open_browser: bool = True) -> int:
        if not WEB_DIR.is_dir():
            print(f"error: renderer assets missing at {WEB_DIR}", file=sys.stderr)
            return 1
        self.loop = asyncio.get_running_loop()
        self._httpd = start_http(self.host, self.http_port, self.token, self)
        url = f"http://{self.host}:{self.http_port}/#token={self.token}&ws={self.ws_port}"
        print(f"LunaMoth desktop: {url}", file=sys.stderr, flush=True)
        print("life.state: supervisor emits life.state frames on client connect and transitions", file=sys.stderr, flush=True)
        if open_browser:
            self._open_later(url)
        await self.bootstrap_gateways()
        try:
            await self._serve_ws()
        finally:
            await self.shutdown()
        return 0

    def _open_later(self, url: str) -> None:
        def work() -> None:
            time.sleep(0.4)
            if sys.platform == "darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import webbrowser

                webbrowser.open(url)

        threading.Thread(target=work, name="desktop-open", daemon=True).start()

    async def _serve_ws(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError("the desktop needs websockets. Install with: uv sync --extra server") from exc
        handler = functools.partial(self._ws_entry)
        async with websockets.serve(handler, self.host, self.ws_port, max_size=16 * 1024 * 1024):
            await self._shutdown.wait()

    async def _ws_entry(self, ws: Any, path: str = "") -> None:
        path = _path_from_ws(ws, path)
        if not query_auth_ok(path, self.token):
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

    async def shutdown(self) -> None:
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

    def request_shutdown(self) -> None:
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
