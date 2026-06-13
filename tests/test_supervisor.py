from __future__ import annotations

import asyncio
import json

from lunamoth.server.supervisor import (
    GATEWAY_FATAL_EXIT,
    DriverSlot,
    FrameRing,
    GatewayChild,
    GatewayInfo,
    IdleGate,
    LifeState,
    RestartBackoff,
)


def test_frame_ring_injects_seq_replays_and_reports_gap():
    ring = FrameRing(capacity=3)
    assert ring.push({"method": "hello"})["seq"] == 1
    ring.push({"method": "a"})
    ring.push({"method": "b"})
    ring.push({"method": "c"})
    ok, frames = ring.replay_after(2)
    assert ok
    assert [f["seq"] for f in frames] == [3, 4]
    ok, frames = ring.replay_after(0)
    assert not ok and frames == []


def test_idle_gate_quiet_rest_backoff_and_delay():
    mono = {"t": 0.0}
    epoch = {"t": 1000.0}
    gate = IdleGate(monotonic=lambda: mono["t"], epoch=lambda: epoch["t"])
    snap = {"quiet": 5, "rest_until": 0.0, "patience": 10.0}
    gate.note_user()
    assert gate.life_state(snap).state == "idle_countdown"  # at t=0 no truthy engagement stamp
    mono["t"] = 1.0
    gate.note_user()
    mono["t"] = 2.0
    waiting = gate.life_state(snap)
    assert waiting.state == "waiting"
    assert round(waiting.engaged_until) == 1004
    mono["t"] = 8.0
    gate.schedule_after(snap)
    countdown = gate.life_state(snap)
    assert countdown.state == "idle_countdown"
    assert round(countdown.next_cycle_at) == 1010
    mono["t"] = 18.0
    assert gate.ready(snap)
    gate.mark_idle_error("HTTP 401 invalid key")
    assert gate.life_state(snap).state == "backoff"


def test_life_state_frame_shape():
    frame = LifeState("resting", rest_until=123.0).frame()
    assert frame == {
        "jsonrpc": "2.0",
        "method": "life.state",
        "params": {
            "state": "resting",
            "next_cycle_at": 0.0,
            "rest_until": 123.0,
            "engaged_until": 0.0,
            "detail": "",
        },
    }
    json.dumps(frame)


class FakeDriver:
    def __init__(self):
        self.closed = False


async def _takeover():
    slot = DriverSlot()
    a = FakeDriver()
    b = FakeDriver()

    async def close(old):
        old.closed = True

    await slot.take(a, close)
    await slot.take(b, close)
    return a.closed, slot.current is b


def test_driver_takeover_closes_old_driver():
    import asyncio

    closed, current = asyncio.run(_takeover())
    assert closed and current


def test_client_detach_does_not_kill_the_resident_child():
    """A client leaving the room is a presence fact, not a child shutdown:
    the supervisor must translate `detach` instead of forwarding it (the
    child's stdio transport treats a forwarded detach as 'exit')."""
    import asyncio

    from lunamoth.server.supervisor import CharaChild
    from lunamoth.session.sessions import SessionMeta

    child = CharaChild(SessionMeta(name="t"), supervisor=None)
    child._attached = True
    calls = []

    async def fake_private_call(method, params, timeout=10.0):
        calls.append((method, params))
        return {"ok": True}

    sent = []

    class Driver:
        joined = True

        async def send(self, frame):
            sent.append(frame)
            return True

    child.private_call = fake_private_call
    child.driver_slot.current = Driver()

    async def run():
        # proc is None: if the frame were forwarded (or ensure_started were
        # reached) this would raise / try to spawn — translation must short-circuit.
        await child.forward_client_frame('{"jsonrpc":"2.0","id":7,"method":"detach"}')

    asyncio.run(run())
    assert calls == [("presence.set", {"present": False})]
    assert sent and sent[0]["id"] == 7 and sent[0]["result"]["ok"] is True
    assert child.proc is None and child.state == "stopped"


# ---- RestartBackoff primitive (injectable clock) ---------------------------

def test_restart_backoff_exponential_floor_cap_and_strikes():
    mono = {"t": 0.0}
    b = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=3, monotonic=lambda: mono["t"])
    # Crash before the run was ever marked started: ladder grows 60→120→240…
    b.note_started()
    assert b.note_crash() == 60.0 and b.strikes == 1 and not b.suspended
    b.note_started()
    assert b.note_crash() == 120.0 and b.strikes == 2 and not b.suspended
    b.note_started()
    assert b.note_crash() == 240.0 and b.strikes == 3 and b.suspended  # 3rd crash → suspended


def test_restart_backoff_caps_at_ceiling():
    b = RestartBackoff(floor=60.0, cap=200.0, health_after=120.0, max_strikes=10**9)
    delays = []
    for _ in range(6):
        delays.append(b.note_crash())
    assert delays == [60.0, 120.0, 200.0, 200.0, 200.0, 200.0]  # capped at 200


def test_restart_backoff_healthy_run_resets_strikes_and_delay():
    mono = {"t": 0.0}
    b = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=3, monotonic=lambda: mono["t"])
    b.note_started()
    b.note_crash()  # strike 1, delay now 120
    b.note_started()
    b.note_crash()  # strike 2, delay now 240
    assert b.strikes == 2
    # New run that stays up past the health threshold resets both.
    mono["t"] = 100.0
    b.note_started()
    mono["t"] = 100.0 + 121.0  # > health_after
    assert b.note_healthy_if_due() is True
    assert b.strikes == 0 and b.delay == 60.0
    assert b.note_healthy_if_due() is False  # idempotent within the run


def test_restart_backoff_note_crash_resets_after_healthy_even_without_poll():
    """A run that proved healthy but crashes before the next idle poll still
    resets the ladder — the strike fallback inside note_crash covers it."""
    mono = {"t": 0.0}
    b = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=3, monotonic=lambda: mono["t"])
    b.note_started()
    b.note_crash()
    b.note_started()
    b.note_crash()  # delay now 240, strikes 2
    mono["t"] = 1000.0
    b.note_started()
    mono["t"] = 1000.0 + 130.0  # healthy window elapsed, but note_healthy_if_due not polled
    assert b.note_crash() == 60.0  # fresh ladder
    assert b.strikes == 1


# ---- CharaChild supervised auto-restart ------------------------------------

def _fake_proc(returncode):
    class P:
        def __init__(self, rc):
            self.returncode = rc
            self.pid = 4321

        async def wait(self):
            return self.returncode

    return P(returncode)


def _make_child(monotonic):
    from lunamoth.server.supervisor import CharaChild
    from lunamoth.session.sessions import SessionMeta

    child = CharaChild(SessionMeta(name="t"), supervisor=None)
    child.restart = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=3, monotonic=monotonic)
    return child


def test_chara_unexpected_exit_schedules_backoff_restart(monkeypatch):
    mono = {"t": 0.0}
    child = _make_child(lambda: mono["t"])
    life = []
    child._emit_life = lambda st: life.append(st)
    starts = []

    async def fake_start(*, operator=True):
        starts.append(operator)
        return {"state": "running"}

    child.start = fake_start
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    child.proc = _fake_proc(1)
    child.restart.note_started()

    async def run():
        await child._note_exit()
        # let the restart task it scheduled run
        if child._restart_task:
            await child._restart_task

    asyncio.run(run())
    assert child.state in {"starting", "running"}
    assert slept == [60.0]  # one backoff delay at the floor
    assert starts == [False]  # internal supervised restart, not operator
    # life emitted backoff then (during restart) backoff "restarting"
    assert any(st.state == "backoff" for st in life)


def test_chara_three_strikes_suspends_terminal_crashed(monkeypatch):
    mono = {"t": 0.0}
    child = _make_child(lambda: mono["t"])
    life = []
    child._emit_life = lambda st: life.append(st)

    async def fake_sleep(d):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run():
        for _ in range(3):
            child.proc = _fake_proc(1)
            child.restart.note_started()  # fresh failed run each time
            await child._note_exit()
            if child._restart_task and not child.restart.suspended:
                # cancel the scheduled restart so we drive crashes manually
                child._cancel_pending_restart()

    asyncio.run(run())
    assert child.state == "crashed"
    assert "suspended" in child.detail
    assert life[-1].state == "crashed"


def test_chara_requested_stop_is_not_a_crash():
    child = _make_child(lambda: 0.0)
    life = []
    child._emit_life = lambda st: life.append(st)
    child._stopping = True
    child.proc = _fake_proc(1)

    async def run():
        await child._note_exit()

    asyncio.run(run())
    assert child.state == "stopped"
    assert child._restart_task is None
    assert child.restart.strikes == 0


def test_chara_clean_exit_zero_is_not_a_crash():
    child = _make_child(lambda: 0.0)
    child._emit_life = lambda st: None
    child.proc = _fake_proc(0)

    async def run():
        await child._note_exit()

    asyncio.run(run())
    assert child.state == "stopped"
    assert child._restart_task is None


def test_chara_operator_start_clears_suspension():
    child = _make_child(lambda: 0.0)
    child._emit_life = lambda st: None
    child.state = "crashed"
    child.detail = "crashed (exit 1); suspended"
    child.restart.strikes = 3
    assert child.restart.suspended

    # An operator ensure_started clears suspension and tries again.
    spawned = []

    async def fake_start(*, operator=True):
        spawned.append(operator)
        child.restart.reset() if operator else None
        return {"state": "running"}

    # start() itself does the reset; here we verify the gating in ensure_started.
    child.start = fake_start

    async def run():
        await child.ensure_started(operator=True)

    asyncio.run(run())
    assert spawned == [True]


def test_chara_internal_caller_does_not_resurrect_suspended_child():
    child = _make_child(lambda: 0.0)
    child.state = "crashed"
    child.detail = "suspended"
    child.restart.strikes = 3

    async def run():
        await child.ensure_started(operator=False)

    import pytest

    with pytest.raises(RuntimeError):
        asyncio.run(run())


# ---- GatewayChild state enum + error_message + backoff health reset --------

def test_gateway_info_shape_has_state_enum_and_error_message():
    import dataclasses

    info = GatewayInfo(platform="qq", state="backoff", detail="x", error_message="boom", pid=7)
    d = dataclasses.asdict(info)
    assert set(d) == {"platform", "state", "detail", "error_message", "pid"}
    json.dumps(d)


class _FakeMeta:
    """A SessionMeta stand-in whose root is a real tmp dir (gateway.log lands there)."""

    def __init__(self, root):
        self.name = "t"
        self.root = root
        self.isolation = "sandbox"

    def env(self):
        return {}


def _make_gateway(monotonic, root):
    gw = GatewayChild.__new__(GatewayChild)
    gw.meta = _FakeMeta(root)
    gw.name = "t"
    gw.supervisor = None
    gw.info = GatewayInfo()
    gw.proc = None
    gw._task = None
    gw._stop_requested = False
    gw.backoff = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=10**9, monotonic=monotonic)
    return gw


def test_gateway_state_mapping_transient_crash_goes_backoff(monkeypatch, tmp_path):
    mono = {"t": 0.0}
    gw = _make_gateway(lambda: mono["t"], tmp_path)
    monkeypatch.setattr(gw, "enabled", lambda: True)
    monkeypatch.setattr(gw, "_platform", lambda: "qq")

    spawn_calls = {"n": 0}

    async def fake_spawn():
        spawn_calls["n"] += 1
        gw.proc = _fake_proc(1)  # transient crash exit
        return gw.proc

    # Drive a single loop iteration: spawn → crash(1) → backoff, then stop.
    states = []

    async def fake_sleep(d):
        states.append(("backoff", gw.info.state, gw.info.error_message, d))
        gw._stop_requested = True  # break the loop after one backoff

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: fake_spawn())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run():
        await gw._run_supervised()

    asyncio.run(run())
    assert states and states[0][1] == "backoff"
    assert states[0][3] == 60.0  # floor delay
    assert "crashed (exit 1)" in states[0][2]


def test_gateway_fatal_exit_does_not_retry(monkeypatch, tmp_path):
    mono = {"t": 0.0}
    gw = _make_gateway(lambda: mono["t"], tmp_path)
    monkeypatch.setattr(gw, "enabled", lambda: True)
    monkeypatch.setattr(gw, "_platform", lambda: "qq")

    async def fake_spawn():
        gw.proc = _fake_proc(GATEWAY_FATAL_EXIT)
        return gw.proc

    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: fake_spawn())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run():
        await gw._run_supervised()

    asyncio.run(run())
    assert gw.info.state == "fatal"
    assert gw.info.error_message and "configuration" in gw.info.error_message
    assert slept == []  # never retried


def test_gateway_backoff_resets_after_healthy_run():
    """An isolated crash a long time after a healthy run restarts at the floor,
    not compounded toward the 1800s cap."""
    mono = {"t": 0.0}
    b = RestartBackoff(floor=60.0, cap=1800.0, health_after=120.0, max_strikes=10**9, monotonic=lambda: mono["t"])
    # A burst of early crashes climbs the ladder.
    b.note_started(); b.note_crash()
    b.note_started(); b.note_crash()
    b.note_started(); b.note_crash()
    assert b.delay > 60.0
    # Then a healthy run for > health_after.
    mono["t"] = 1000.0
    b.note_started()
    mono["t"] = 1000.0 + 200.0
    b.note_healthy_if_due()
    assert b.delay == 60.0
    # A later isolated crash restarts at the floor.
    b.note_started()
    mono["t"] = 1300.0
    assert b.note_crash() == 60.0


def test_gateway_clean_exit_zero_stops(monkeypatch, tmp_path):
    mono = {"t": 0.0}
    gw = _make_gateway(lambda: mono["t"], tmp_path)
    monkeypatch.setattr(gw, "enabled", lambda: True)
    monkeypatch.setattr(gw, "_platform", lambda: "qq")

    async def fake_spawn():
        gw.proc = _fake_proc(0)
        return gw.proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: fake_spawn())

    async def run():
        await gw._run_supervised()

    asyncio.run(run())
    assert gw.info.state == "stopped" and gw.info.error_message == ""


def test_autonomy_pause_marker_round_trip(tmp_path):
    """The board on/off persists as a marker; entering never changes it."""
    from lunamoth.server.supervisor import Supervisor
    from lunamoth.session.sessions import SessionMeta

    meta = SessionMeta(name="p")
    # point the session root at a temp dir
    object.__setattr__(meta, "name", "p")
    import lunamoth.session.sessions as S
    root = tmp_path / "sessions" / "p"
    root.mkdir(parents=True)
    # SessionMeta.root derives from sessions_dir()/name — patch sessions_dir
    orig = S.sessions_dir
    S.sessions_dir = lambda: tmp_path / "sessions"
    try:
        assert Supervisor.is_paused(meta) is False
        Supervisor.set_paused(meta, True)
        assert Supervisor.is_paused(meta) is True
        Supervisor.set_paused(meta, False)
        assert Supervisor.is_paused(meta) is False
    finally:
        S.sessions_dir = orig


def test_set_autonomy_toggles_the_same_marker_the_board_reads(tmp_path, monkeypatch):
    """The in-chat autonomy switch and the board's status must agree: both go
    through the one persisted pause marker (the inner/outer conflict fix)."""
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.server import hub as H
    from lunamoth.server.supervisor import Supervisor
    from lunamoth.session import sessions as S

    H.save_defaults({"provider": "openrouter", "base_url": "https://x.invalid/v1",
                     "api_key": "k", "model": "m"})
    entry = H.wake(card_path=str(H.bundled_cards_dir() / "Quinn.zh.json"))
    meta = S.load_session(entry["name"])

    # no supervisor → the hub flips the marker directly; the board reads it back
    out = []
    d = H.HubDispatcher(lambda f: out.append(f) or True)
    def call(method, params):
        r = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert "error" not in r, r.get("error")
        return r["result"]

    call("chara.set_autonomy", {"name": meta.name, "on": False})
    assert Supervisor.is_paused(meta) is True
    assert call("sessions.list", {})[0]["paused"] is True   # board agrees: off
    call("chara.set_autonomy", {"name": meta.name, "on": True})
    assert Supervisor.is_paused(meta) is False
    assert call("sessions.list", {})[0]["paused"] is False  # board agrees: on
