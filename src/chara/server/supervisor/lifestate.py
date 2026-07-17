"""Small, unit-testable policy primitives shared by the chara/gateway children.

These carry no I/O and no websocket/process dependencies — they are pure state
machines with injectable clocks, so the child lifecycle (children.py) and the
idle/life driving (the Supervisor coordinator) can be reasoned about and tested
in isolation.
"""
from __future__ import annotations

import dataclasses
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...content.knobs import DEFAULT_PATIENCE, DEFAULT_QUIET


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
        return max(0.0, float(snapshot.get("patience") or DEFAULT_PATIENCE))

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
        quiet = max(0, int(snapshot.get("quiet") or DEFAULT_QUIET))
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
