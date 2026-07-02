"""Per-chara stdio children + the messaging gateway controller.

``CharaChild`` owns a long-lived ``lunamoth serve <name> --stdio`` subprocess:
its stdout pump, the RPC plumbing, the idle/life loop, and the supervised
auto-restart with backoff. ``GatewayChild`` is a thin controller that toggles
the in-child messaging host (the adapters run INSIDE the chara child, sharing
its one agent) over RPC.

It deliberately never imports core/tools: chara work happens only inside the
spawned child process.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...content.knobs import DEFAULT_PATIENCE, DEFAULT_QUIET
from ...session import sessions as S
from ..ws import _close_ws
from .lifestate import DriverSlot, FrameRing, IdleGate, LifeState, RestartBackoff, permanent_model_error
from .paths import APP_DIR

if TYPE_CHECKING:
    from .core import Supervisor

_log = logging.getLogger("lunamoth.server.supervisor")


# A stalled browser must not wedge the child's stdout pump: the supervisor
# reads the child's stdout on the loop and forwards each frame to the driver
# inline, so a `ws.send` that blocks on a slow client backs pressure all the
# way down into the agent (its stdout pipe fills, then it blocks on write).
# The FrameRing already lets a client recover missed frames on rejoin, so we
# bound the per-frame send: on timeout we drop the driver rather than freeze
# the pump — the slow client simply rejoins and replays from its last seq.
_DRIVER_SEND_TIMEOUT_SECONDS = 10.0


def _driver_send_timeout() -> float:
    """The per-frame send ceiling, read off the package surface at call time.

    Tests shrink ``supervisor._DRIVER_SEND_TIMEOUT_SECONDS`` on the package to
    keep the stalled-client test fast; honoring it requires a live lookup rather
    than capturing the module-level value at import time.
    """
    pkg = sys.modules.get("lunamoth.server.supervisor")
    if pkg is not None:
        return float(getattr(pkg, "_DRIVER_SEND_TIMEOUT_SECONDS", _DRIVER_SEND_TIMEOUT_SECONDS))
    return _DRIVER_SEND_TIMEOUT_SECONDS


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
                await asyncio.wait_for(self.ws.send(raw), timeout=_driver_send_timeout())
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
    # Auto-suspend a chat-mode (autonomy-off) chara after this long idle + nobody in its
    # room + no live gateway — the next message / autonomy-on lazily restarts it (history
    # restored). This is the ONLY lifecycle effect of autonomy-off; there is no separate
    # restart concept for the user. Env-overridable for tests.
    SUSPEND_AFTER = float(os.environ.get("LUNAMOTH_SUSPEND_AFTER", "1800"))

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
        # Strong refs for fire-and-forget tasks (life-frame sends, idle-suspend stop):
        # the event loop only holds a WEAK ref, so without this a task can be GC'd
        # mid-flight and the frame/stop silently vanishes.
        self._bg_tasks: set[asyncio.Task] = set()
        self._pending: dict[Any, asyncio.Future] = {}
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        self._stopping = False
        self._attached = False
        self._snap_cache: tuple[float, dict[str, Any]] | None = None
        self._client_stream_ids: set[Any] = set()
        self.idle = IdleGate()
        self.life: LifeState | None = None
        self._chat_mode_since = 0.0  # when this run entered chat mode (for idle-suspend)
        # Supervised auto-restart: an unexpected exit restarts with backoff
        # (60s→1800s) up to 3 consecutive crashes, then suspends (terminal
        # crashed). A healthy run resets both. Operator start clears suspension.
        self.restart = RestartBackoff()

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "pid": self.proc.pid if self.proc else 0, "life": dataclasses.asdict(self.life) if self.life else None}

    def _gateway_enabled(self) -> bool:
        """True if a messaging gateway is configured on for this chara — its in-child host
        would die with the process, so an enabled gateway blocks idle-suspend."""
        try:
            data = json.loads((self.meta.root / "messaging.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(isinstance(data, dict) and data.get("enabled"))

    def _idle_suspend_due(self, now: float) -> bool:
        """Whether an idle chat-mode chara should be auto-suspended now: it entered chat
        mode (``_chat_mode_since`` set), has been idle past ``SUSPEND_AFTER`` (since the
        later of chat-mode entry / last operator message), nobody is in its room, and no
        gateway is live. Pure decision — the loop owns the stop + the clock reset."""
        if self._chat_mode_since == 0.0:
            return False
        attached = self.driver_slot.current is not None or bool(self._client_stream_ids)
        idle_for = now - max(self.idle.last_user_mono, self._chat_mode_since)
        return not attached and idle_for >= self.SUSPEND_AFTER and not self._gateway_enabled()

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
            env = {**os.environ, **meta.env()}  # meta.env() carries LUNAMOTH_PY_BACKEND
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
            # A fresh process reads the (possibly edited) card into its stable prefix,
            # so any "card changed since start" flag is now satisfied — clear it. Locked +
            # atomic (shared with the hub's config writers) so it can't tear config.json /
            # drop the api_key, nor lose-update against a concurrent card.patch.
            with contextlib.suppress(OSError, json.JSONDecodeError):
                from ..hub._common import _atomic_write_json, card_write_lock
                with card_write_lock(meta.config_path):
                    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
                    if cfg.pop("card_dirty", None) is not None:
                        _atomic_write_json(meta.config_path, cfg, private=True)
            self.restart.note_started()
            self._stdout_task = asyncio.create_task(self._read_stdout(), name=f"chara-{self.name}-stdout")
            self._idle_task = asyncio.create_task(self._idle_loop(), name=f"chara-{self.name}-idle")
            self._emit_life(self.idle.life_state({"patience": DEFAULT_PATIENCE, "quiet": DEFAULT_QUIET}))
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
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            # Always reclaim the slot. _read_stdout only pops on a matching
            # response, which never arrives for a timed-out/cancelled call — so
            # without this the _pending dict (and the orphaned future) leak one
            # entry per slow call for the child's whole lifetime.
            self._pending.pop(rid, None)

    async def messaging_turn_active(self) -> bool:
        """True while the in-child messaging host is mid-turn on an INBOUND
        platform message (WeChat/QQ/...): that turn is a conversation with a
        human, so an autonomy-off interrupt must leave it alone — only
        self-work (idle/react) is halted. Best-effort: no host / a slow child
        reads as "no turn"."""
        try:
            st = await self.private_call("messaging.status", {}, timeout=5.0)
        except Exception:  # noqa: BLE001
            return False
        return bool(isinstance(st, dict) and st.get("turn_active"))

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
            # child's lifecycle is OURS, so we swallow the detach and answer the
            # client ourselves (the chara is independent of attach/detach, so
            # there is nothing to register on the leave).
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
        # A driver disconnecting just frees the slot; the resident keeps living
        # exactly as it was (the chara is independent of attach/detach).
        self.driver_slot.clear(driver)

    async def handle_rejoin(self, last_seq: int, driver: _Driver) -> bool:
        ok, frames = self.ring.replay_after(last_seq)
        if not ok:
            await driver.send({"jsonrpc": "2.0", "method": "rejoin.gap"})
            return False
        for frame in frames:
            if not await driver.send(frame):
                return False
        return True

    def _spawn(self, coro, *, name: "str | None" = None) -> None:
        """Fire-and-forget a coroutine while keeping a STRONG ref (the loop holds only
        a weak one) so it can't be garbage-collected mid-flight."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _send_life_to(self, driver: _Driver) -> None:
        if self.life is None:
            return
        self._spawn(driver.send(self.life.frame()))

    def _emit_life(self, state: LifeState) -> None:
        if self.life == state:
            return
        self.life = state
        driver = self.driver_slot.current
        if driver is not None and driver.joined:
            self._spawn(driver.send(state.frame()))

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
                # Completion WAKE — mode-INDEPENDENT (works the same in live and
                # chat): a finished background job (image gen / delegate / background
                # terminal) wakes the chara to react to it, exactly like a user
                # message speaking up. Driven only when no client is streaming; the
                # `react` RPC raises -32011 if a turn is already in flight, in which
                # case that turn drains the notice itself, so we just back off. After
                # reacting we bust the snapshot cache and re-loop so the mode logic
                # below sees the drained (no-longer-pending) state.
                if snap.get("pending_notices") and not self._client_stream_ids:
                    # `snap` is TTL-cached (SNAPSHOT_TTL), so pending_notices can be
                    # stale — a normal turn may have already drained the notice.
                    # Re-read FRESH before waking so we never drive a no-op react on
                    # stale data (and so a real completion is seen promptly).
                    self._snap_cache = None
                    snap = await self.snapshot(silent=True)
                    if snap.get("pending_notices") and not self._client_stream_ids:
                        try:
                            await self.private_call("react", {}, timeout=3600.0)
                        except Exception:  # noqa: BLE001
                            if self.proc is None or self.proc.returncode is not None:
                                return
                            await asyncio.sleep(1.0)  # a turn is already running; it drains it
                        self._snap_cache = None
                        continue
                    # fresh read shows it was already drained → fall through with the
                    # fresh snapshot (the mode logic below uses it).
                # Autonomous running is OFF (operator toggled it off on the
                # AUTONOMY is the chara's `mode`: live = autonomous (the full
                # lifecycle below), chat = a plain chat agent that NEVER works on
                # its own. Off → no cycles ever; entering to chat can't turn it
                # on, only the board/in-chat autonomy switch (which flips mode).
                if str(snap.get("mode") or "live") != "live":
                    self._emit_life(LifeState("waiting"))
                    # Idle-suspend: a chat-mode (autonomy-off) chara that's been idle past
                    # SUSPEND_AFTER, with nobody in its room and no live gateway, is shut
                    # down to free resources. The next message / autonomy-on lazily restarts
                    # it (ensure_started, history restored) — which is also how a restart
                    # happens at all, so autonomy stays the ONE switch.
                    if self._chat_mode_since == 0.0:
                        self._chat_mode_since = time.monotonic()
                    if self._idle_suspend_due(time.monotonic()):
                        self._emit_life(LifeState("waiting", detail="suspended (idle)"))
                        self._spawn(self.stop(), name=f"chara-{self.name}-suspend")
                        return
                    await asyncio.sleep(1.0)
                    continue
                self._chat_mode_since = 0.0  # autonomous again → reset the idle-suspend clock
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
    # Per-platform breakdown for the gateway overview: one entry per CONFIGURED
    # platform — {"platform", "enabled", "state"} — merging the config's
    # per-platform enabled flag with the host's live state (running/needs_login
    # for enabled platforms, stopped otherwise). Lets the overview show one
    # independent row per (chara, platform).
    platforms: list = dataclasses.field(default_factory=list)


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

    def _platform_rows(self, live: Any) -> list[dict[str, Any]]:
        """One row per CONFIGURED platform: its own `enabled` flag (an absent flag
        inherits the legacy top-level `enabled`, so old configs keep working) plus
        the host's live state for it (running/needs_login when up, else stopped)."""
        cfg = self._config()
        adapters = cfg.get("adapters")
        if not isinstance(adapters, dict):
            return []
        from ...messaging.gateway import adapter_enabled  # one canonical "own ?? legacy"
        legacy = bool(cfg.get("enabled"))
        live_by: dict[str, dict[str, Any]] = {}
        if isinstance(live, list):
            for p in live:
                if isinstance(p, dict) and p.get("platform"):
                    live_by[str(p["platform"])] = p
        rows: list[dict[str, Any]] = []
        for name in sorted(str(k) for k in adapters):
            ac = adapters.get(name)
            ac = ac if isinstance(ac, dict) else {}
            on = adapter_enabled(ac, legacy=legacy)
            lv = live_by.get(name)
            rows.append({"platform": name, "enabled": on,
                         "state": str(lv.get("state")) if lv else "stopped"})
        return rows

    def enabled(self) -> bool:
        return bool(self._config().get("enabled"))

    def set_enabled(self, enabled: bool) -> None:
        """Persist the gateway kill-switch. The top-level `enabled` is DERIVED
        from the per-platform flags on every messaging.save (session_messaging
        recomputes it, and the web client re-derives it too), so a bare
        top-level write is silently undone by the next save — the per-platform
        flags themselves must move. OFF materializes `enabled: false` into
        every adapter block so the stop sticks. ON writes the top level and
        flips the platform flags back on only when NO platform would come on
        otherwise (every block carries an explicit false — i.e. after a stop);
        a deliberate per-platform choice is otherwise left alone."""
        path = self._config_path()
        data = self._config()
        data["enabled"] = bool(enabled)
        adapters = data.get("adapters")
        if isinstance(adapters, dict):
            from ...messaging.gateway import adapter_enabled
            blocks = [a for a in adapters.values() if isinstance(a, dict)]
            if not enabled:
                for a in blocks:
                    a["enabled"] = False
            elif blocks and not any(adapter_enabled(a, legacy=True) for a in blocks):
                for a in blocks:
                    a["enabled"] = True
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
        self.info.platforms = self._platform_rows(st.get("platforms"))

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
        self.info.platforms = self._platform_rows(None)
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
            self.info.platforms = self._platform_rows(None)
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
