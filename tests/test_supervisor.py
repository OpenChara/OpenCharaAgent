from __future__ import annotations

import asyncio
import json
import logging

from lunamoth.server.supervisor import (
    DriverSlot,
    FrameRing,
    GatewayChild,
    GatewayInfo,
    IdleGate,
    LifeState,
    ResourceCanary,
    RestartBackoff,
    _Driver,
    format_shutdown_context,
    log_memory_usage,
    snapshot_shutdown_context,
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
    """A client leaving the room is NOT a child shutdown: the supervisor must
    swallow `detach` and answer the client itself instead of forwarding it (the
    child's stdio transport treats a forwarded detach as 'exit'). The chara is
    independent of attach/detach, so there is nothing to register on the leave."""
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
        # reached) this would raise / try to spawn — the swallow must short-circuit.
        await child.forward_client_frame('{"jsonrpc":"2.0","id":7,"method":"detach"}')

    asyncio.run(run())
    assert calls == []  # detach is swallowed, no presence translation
    assert sent and sent[0]["id"] == 7 and sent[0]["result"]["ok"] is True
    assert child.proc is None and child.state == "stopped"


def test_idle_suspend_due_only_when_idle_detached_no_gateway():
    """A chat-mode (autonomy-off) chara auto-suspends only when it's been idle past
    SUSPEND_AFTER, nobody is in its room, and no gateway is live — and a recent operator
    message resets the idle clock."""
    from lunamoth.server.supervisor import CharaChild
    from lunamoth.session.sessions import SessionMeta

    child = CharaChild(SessionMeta(name="t"), supervisor=None)
    child.SUSPEND_AFTER = 100.0
    child._gateway_enabled = lambda: False
    # A FIXED clock — _idle_suspend_due takes `now` as a param, so we don't touch the real
    # monotonic clock (which is near-zero on a freshly-booted CI runner, where `now - 200`
    # would go negative and skew the max() — the bug that made the first version CI-only).
    now = 100_000.0

    assert child._idle_suspend_due(now) is False          # never entered chat mode
    child._chat_mode_since = now - 200.0
    assert child._idle_suspend_due(now) is True            # idle 200s, detached, no gateway
    child.driver_slot.current = object()
    assert child._idle_suspend_due(now) is False           # someone in the room
    child.driver_slot.current = None
    child._client_stream_ids.add(1)
    assert child._idle_suspend_due(now) is False           # an in-flight turn
    child._client_stream_ids.clear()
    child.idle.last_user_mono = now - 10.0
    assert child._idle_suspend_due(now) is False           # a recent message reset the clock
    child.idle.last_user_mono = now - 300.0
    assert child._idle_suspend_due(now) is True            # last message long ago
    child._gateway_enabled = lambda: True
    assert child._idle_suspend_due(now) is False           # a live gateway blocks suspend
    child._gateway_enabled = lambda: False
    child._chat_mode_since = now - 50.0
    child.idle.last_user_mono = 0.0
    assert child._idle_suspend_due(now) is False           # only 50s < SUSPEND_AFTER


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
    # `platforms` (per-platform overview rows) joined the shape when gateways
    # became independently toggleable; it defaults to an empty list.
    assert set(d) == {"platform", "state", "detail", "error_message", "pid", "platforms"}
    assert d["platforms"] == []
    json.dumps(d)


class _FakeMeta:
    """A SessionMeta stand-in whose root is a real tmp dir (messaging.json lands there)."""

    def __init__(self, root):
        self.name = "t"
        self.root = root
        self.isolation = "sandbox"

    def env(self):
        return {}


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.pid = 4242


class _FakeCharaChild:
    """Stand-in for a running serve child that hosts the shared agent."""

    def __init__(self, host_status=None):
        self.proc = _FakeProc()
        self.ensure_started_calls = 0
        self.calls: list[str] = []
        self.host_status = host_status or {"state": "running", "platform": "weixin", "detail": ""}

    async def ensure_started(self, *, operator=False):
        self.ensure_started_calls += 1

    async def private_call(self, method, params=None, timeout=60.0):
        self.calls.append(method)
        if method == "messaging.start":
            return self.host_status
        if method == "messaging.stop":
            return {"state": "stopped", "platform": "weixin", "detail": ""}
        if method == "messaging.status":
            return self.host_status
        return {"state": "stopped", "platform": "", "detail": ""}


class _FakeSupervisor:
    def __init__(self, child):
        self._child = child
        self.charas = {"t": child} if child is not None else {}

    def child(self, name):
        return self._child


def _make_gateway(root, supervisor):
    gw = GatewayChild.__new__(GatewayChild)
    gw.meta = _FakeMeta(root)
    gw.name = "t"
    gw.supervisor = supervisor
    gw.info = GatewayInfo()
    return gw


def _write_messaging(root, enabled):
    cfg = {"enabled": bool(enabled), "adapters": {"weixin": {}}}
    (root / "messaging.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_gateway_start_drives_shared_agent_child(tmp_path):
    # The gateway no longer spawns its own process: it ensures the chara child
    # (the shared agent) is up and turns the in-child messaging host on.
    child = _FakeCharaChild()
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))
    _write_messaging(tmp_path, enabled=True)

    asyncio.run(gw.start(persist=False))
    assert child.ensure_started_calls == 1
    assert "messaging.start" in child.calls
    assert gw.info.state == "running" and gw.info.platform == "weixin"


def test_gateway_start_noop_when_disabled(tmp_path):
    child = _FakeCharaChild()
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))
    _write_messaging(tmp_path, enabled=False)

    asyncio.run(gw.start(persist=False))
    assert child.calls == []  # never touched the chara child
    assert gw.info.state == "stopped"


def test_gateway_stop_tells_running_child(tmp_path):
    child = _FakeCharaChild()
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))

    asyncio.run(gw.stop(persist=False))
    assert "messaging.stop" in child.calls
    assert gw.info.state == "stopped" and gw.info.error_message == ""


def test_gateway_status_live_reflects_real_host_state(tmp_path):
    # status_live() asks the in-child host for the truth (running vs
    # needs_login vs stopped) — never a "child is up so it must be running" lie.
    child = _FakeCharaChild(host_status={"state": "needs_login", "platform": "weixin", "detail": "weixin"})
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))
    _write_messaging(tmp_path, enabled=True)

    st = asyncio.run(gw.status_live())
    assert st["state"] == "needs_login"
    assert "messaging.status" in child.calls


def test_gateway_status_live_running(tmp_path):
    child = _FakeCharaChild(host_status={"state": "running", "platform": "weixin", "detail": ""})
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))
    _write_messaging(tmp_path, enabled=True)

    st = asyncio.run(gw.status_live())
    assert st["state"] == "running" and st["platform"] == "weixin"


def test_gateway_status_live_stopped_when_child_down(tmp_path):
    gw = _make_gateway(tmp_path, _FakeSupervisor(None))
    _write_messaging(tmp_path, enabled=True)

    st = asyncio.run(gw.status_live())
    assert st["state"] == "stopped"  # host boots with the chara child


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


def test_gateway_start_surfaces_host_error_as_backoff(tmp_path):
    # A bad adapter config makes messaging.start raise; the controller reflects
    # it as a visible backoff (never a silent success).
    child = _FakeCharaChild()

    async def boom(method, params=None, timeout=60.0):
        child.calls.append(method)
        raise RuntimeError("unknown messaging adapter 'nope'")

    child.private_call = boom
    gw = _make_gateway(tmp_path, _FakeSupervisor(child))
    _write_messaging(tmp_path, enabled=True)

    asyncio.run(gw.start(persist=False))
    assert gw.info.state == "backoff"
    assert "nope" in gw.info.error_message


def test_gateway_stop_survives_a_messaging_save_recompute(tmp_path):
    """P2 (2026-07-02 audit): gateway.stop used to write only the top-level
    `enabled`, which messaging.save re-DERIVES from the per-platform flags —
    the next unrelated save silently un-stopped the gateway. set_enabled(False)
    now materializes `enabled: false` into every adapter block, so the derived
    top level stays false through any recompute."""
    from lunamoth.server.hub.session_messaging import messaging_save

    gw = _make_gateway(tmp_path, _FakeSupervisor(_FakeCharaChild()))
    cfg = {"enabled": True,
           "adapters": {"weixin": {}, "qq": {"url": "ws://x", "enabled": True}}}
    (tmp_path / "messaging.json").write_text(json.dumps(cfg), encoding="utf-8")

    gw.set_enabled(False)  # the gateway.stop(persist=True) write
    on_disk = json.loads((tmp_path / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is False
    assert all(a.get("enabled") is False for a in on_disk["adapters"].values())

    # An UNRELATED messaging.save (edit the allow-list) runs the recompute...
    messaging_save(_FakeMeta(tmp_path), {"allowed_senders": ["alice"]})
    on_disk = json.loads((tmp_path / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is False          # ...and the stop STICKS
    assert gw.enabled() is False
    assert on_disk["allowed_senders"] == ["alice"]

    # gateway.start after a stop: every block carries an explicit false, so the
    # per-platform flags flip back on together with the top level.
    gw.set_enabled(True)
    on_disk = json.loads((tmp_path / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True
    assert all(a.get("enabled") is True for a in on_disk["adapters"].values())


def test_gateway_set_enabled_on_respects_a_deliberate_platform_choice(tmp_path):
    """set_enabled(True) flips the per-platform flags only when NONE would come
    on otherwise (the post-stop state). A mixed, deliberate per-platform choice
    (weixin on, qq off) is left exactly as the user set it."""
    gw = _make_gateway(tmp_path, _FakeSupervisor(_FakeCharaChild()))
    cfg = {"enabled": False,
           "adapters": {"weixin": {"enabled": True}, "qq": {"enabled": False}}}
    (tmp_path / "messaging.json").write_text(json.dumps(cfg), encoding="utf-8")

    gw.set_enabled(True)
    on_disk = json.loads((tmp_path / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True
    assert on_disk["adapters"]["weixin"]["enabled"] is True
    assert on_disk["adapters"]["qq"]["enabled"] is False  # the choice survives


def test_autonomy_is_the_persisted_mode(tmp_path):
    """Autonomy = the chara's mode (live|chat) on disk; there is no separate
    pause flag. is_autonomous reads it, set_mode_on_disk flips it."""
    from lunamoth.server.supervisor import Supervisor
    from lunamoth.session.sessions import SessionMeta
    import lunamoth.session.sessions as S

    (tmp_path / "sessions" / "p").mkdir(parents=True)
    orig = S.sessions_dir
    S.sessions_dir = lambda: tmp_path / "sessions"
    try:
        meta = SessionMeta(name="p")
        assert Supervisor.is_autonomous(meta) is True   # default live
        Supervisor.set_mode_on_disk(meta, "chat")
        assert Supervisor.is_autonomous(meta) is False
        Supervisor.set_mode_on_disk(meta, "live")
        assert Supervisor.is_autonomous(meta) is True
    finally:
        S.sessions_dir = orig


def test_set_autonomy_and_the_board_agree_via_mode(tmp_path, monkeypatch):
    """The in-chat autonomy switch and the board status both go through the one
    persisted mode — inner and outer can never disagree."""
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.server import hub as H
    from lunamoth.server.supervisor import Supervisor
    from lunamoth.session import sessions as S

    # The keyring is the ONE key store (no top-level api_key); seed a provider key
    # and activate it so wake resolves a key by route.
    H.save_key("default", provider="openrouter", base_url="https://x.invalid/v1",
               api_key="k", model="m")
    H.use_key("default")
    entry = H.wake(card_path=str(H.bundled_cards_dir() / "Quinn" / "card.json"))
    meta = S.load_session(entry["name"])

    out = []
    d = H.HubDispatcher(lambda f: out.append(f) or True)
    def call(method, params):
        r = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert "error" not in r, r.get("error")
        return r["result"]

    call("chara.set_autonomy", {"name": meta.name, "on": False})
    assert Supervisor.is_autonomous(meta) is False
    assert call("sessions.list", {})[0]["paused"] is True    # board: autonomy off
    call("chara.set_autonomy", {"name": meta.name, "on": True})
    assert Supervisor.is_autonomous(meta) is True
    assert call("sessions.list", {})[0]["paused"] is False   # board: autonomy on


def test_set_autonomy_off_interrupts_self_work_but_not_a_chat(monkeypatch):
    """Turning autonomy OFF halts an in-flight self-work turn (sends `interrupt`
    so the tool chain stops at the next safe boundary), but leaves an operator
    chat reply (a live client stream) alone. Turning ON never interrupts."""
    import asyncio

    import lunamoth.server.supervisor.core as core
    from lunamoth.server.supervisor import CharaChild, Supervisor
    from lunamoth.session.sessions import SessionMeta

    monkeypatch.setattr(Supervisor, "set_mode_on_disk", staticmethod(lambda meta, mode: None))
    sup = Supervisor(host="127.0.0.1", http_port=0, ws_port=0, token="t")
    child = CharaChild(SessionMeta(name="t"), supervisor=sup)
    monkeypatch.setattr(core.S, "load_session", lambda name: child.meta)

    class _Proc:
        returncode = None
        pid = 4321

    child.proc = _Proc()
    child._emit_life = lambda *a, **k: None

    async def fake_snapshot(silent=False):
        return {}

    child.snapshot = fake_snapshot
    sup.charas["t"] = child

    calls: list[str] = []

    async def fake_private_call(method, params=None, timeout=10.0):
        calls.append(method)
        return {}

    child.private_call = fake_private_call

    # OFF, no client stream → the self-work turn is interrupted
    child._client_stream_ids = set()
    asyncio.run(sup.set_autonomy("t", False))
    assert "interrupt" in calls

    # OFF, a live client stream (operator chat) → reply is NOT cut
    calls.clear()
    child._client_stream_ids = {"rid"}
    asyncio.run(sup.set_autonomy("t", False))
    assert "interrupt" not in calls

    # ON → never interrupts
    calls.clear()
    child._client_stream_ids = set()
    asyncio.run(sup.set_autonomy("t", True))
    assert "interrupt" not in calls


def test_set_autonomy_off_spares_an_inbound_messaging_turn(monkeypatch):
    """P2 (2026-07-02 audit): an inbound WeChat/QQ reply runs inside the child
    and is invisible to _client_stream_ids, so autonomy-off used to cut a live
    user-facing platform reply. The guard now also asks the in-child messaging
    host (messaging.status → turn_active): an active inbound turn is a
    conversation and is spared; self-work is still halted."""
    import asyncio

    import lunamoth.server.supervisor.core as core
    from lunamoth.server.supervisor import CharaChild, Supervisor
    from lunamoth.session.sessions import SessionMeta

    monkeypatch.setattr(Supervisor, "set_mode_on_disk", staticmethod(lambda meta, mode: None))
    sup = Supervisor(host="127.0.0.1", http_port=0, ws_port=0, token="t")
    child = CharaChild(SessionMeta(name="t"), supervisor=sup)
    monkeypatch.setattr(core.S, "load_session", lambda name: child.meta)

    class _Proc:
        returncode = None
        pid = 4321

    child.proc = _Proc()
    child._emit_life = lambda *a, **k: None
    child._client_stream_ids = set()

    async def fake_snapshot(silent=False):
        return {}

    child.snapshot = fake_snapshot
    sup.charas["t"] = child

    turn_active = {"on": True}
    calls: list[str] = []

    async def fake_private_call(method, params=None, timeout=10.0):
        calls.append(method)
        if method == "messaging.status":
            return {"state": "running", "platform": "weixin", "detail": "",
                    "platforms": [], "turn_active": turn_active["on"]}
        return {}

    child.private_call = fake_private_call

    # OFF while the host is mid-turn on an inbound message → the reply is NOT cut.
    asyncio.run(sup.set_autonomy("t", False))
    assert "messaging.status" in calls   # the guard consulted the host
    assert "interrupt" not in calls

    # OFF with no messaging turn in flight → self-work is still halted.
    calls.clear()
    turn_active["on"] = False
    asyncio.run(sup.set_autonomy("t", False))
    assert "interrupt" in calls

    # A host that can't answer (no messaging host / slow child) reads as "no
    # turn" — best-effort, never a hang: self-work is halted.
    calls.clear()

    async def broken_private_call(method, params=None, timeout=10.0):
        calls.append(method)
        if method == "messaging.status":
            raise RuntimeError("rpc timeout")
        return {}

    child.private_call = broken_private_call
    asyncio.run(sup.set_autonomy("t", False))
    assert "interrupt" in calls


# ---- #28 shutdown forensics + resource canary -------------------------------

def test_snapshot_shutdown_context_names_signal_and_pid():
    import signal as _sig

    ctx = snapshot_shutdown_context(_sig.SIGTERM)
    assert ctx["signal"] == "SIGTERM"
    assert ctx["signal_num"] == int(_sig.SIGTERM)
    assert ctx["pid"] > 0 and ctx["ppid"] >= 0
    assert "under_systemd" in ctx
    # No signal ⇒ UNKNOWN, never raises.
    assert snapshot_shutdown_context(None)["signal"] == "UNKNOWN"


def test_format_shutdown_context_is_one_scannable_line():
    line = format_shutdown_context({
        "signal": "SIGINT",
        "under_systemd": False,
        "parent": {"pid": 42, "name": "launchd", "cmdline": "/sbin/launchd"},
        "rss_mb": 123,
        "loadavg_1m": 0.5,
    })
    assert "\n" not in line
    assert "signal=SIGINT" in line and "parent_pid=42" in line
    assert "rss=123MB" in line and "loadavg_1m=0.5" in line


class _CaptureHandler(logging.Handler):
    """Capture lunamoth.server.supervisor records directly.

    The "lunamoth" logger sets propagate=False once setup_logging runs, so
    pytest's caplog (which hangs off the root) can miss our lines under a full
    suite run. Attaching our own handler to the exact logger is propagation-
    independent.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):  # noqa: ANN001
        self.messages.append(record.getMessage())


def _capture_supervisor_logs():
    logger = logging.getLogger("lunamoth.server.supervisor")
    handler = _CaptureHandler()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    return logger, handler, old_level


def test_log_memory_usage_emits_memory_line():
    logger, handler, old = _capture_supervisor_logs()
    try:
        log_memory_usage("baseline", start_time=None)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    assert any("[MEMORY]" in m and "baseline" in m for m in handler.messages)


def test_resource_canary_start_baseline_and_stop():
    logger, handler, old = _capture_supervisor_logs()
    try:
        canary = ResourceCanary(interval=3600.0)  # never fires during the test
        started = canary.start()
        # On a platform without resource introspection start() returns False —
        # either way it must never raise and stop() must be safe.
        canary.stop(timeout=1.0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    if started:
        assert any("baseline" in m for m in handler.messages)
        assert any("shutdown" in m for m in handler.messages)
    # Double-stop is harmless.
    canary.stop(timeout=0.1)


# ---- #29 slow-client backpressure: _Driver bounded send ---------------------

def test_driver_send_drops_on_stalled_client_without_blocking():
    """A ws.send that never completes must time out, not wedge the pump."""
    import lunamoth.server.supervisor as SUP

    class StalledWS:
        async def send(self, raw):  # noqa: ANN001
            await asyncio.sleep(3600)  # never returns: stalled browser

    async def go():
        # Shrink the timeout so the test is fast; the production constant is 10s.
        old = SUP._DRIVER_SEND_TIMEOUT_SECONDS
        SUP._DRIVER_SEND_TIMEOUT_SECONDS = 0.05
        try:
            d = _Driver(StalledWS())
            ok = await d.send({"method": "event"})
            assert ok is False  # dropped, not hung
        finally:
            SUP._DRIVER_SEND_TIMEOUT_SECONDS = old

    asyncio.run(asyncio.wait_for(go(), timeout=5.0))


def test_asset_route_serves_image_confines_and_rejects_nonimage():
    """The /asset static route serves card art, stays inside the card/session
    dirs, and refuses non-image files (no card.json leak, no path traversal)."""
    import urllib.parse
    import urllib.request
    import urllib.error
    from lunamoth.server import supervisor as SV
    from lunamoth.server import hub as H

    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="t", supervisor=None)

    def code(abspath):
        # /asset GET requires the session token (cookie or ?token=) — the auth gate.
        url = f"http://127.0.0.1:{port}/asset?token=t&p=" + urllib.parse.quote(abspath)
        try:
            return urllib.request.urlopen(url, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        sprite = (H.bundled_cards_dir() / "Quinn" / "sprite.png").resolve()
        if sprite.is_file():
            assert code(str(sprite)) == 200                  # a real card asset serves
        assert code("/etc/passwd") == 404                    # outside the allowed roots
        assert code(str((H.bundled_cards_dir() / "Quinn" / "card.json").resolve())) == 404  # non-image
    finally:
        srv.shutdown()


def test_asset_route_requires_auth(tmp_path, monkeypatch):
    """SEC-1: /asset is authenticated — a request without the session token (no
    cookie, no ?token=) is refused; the cookie OR the query token unlocks it."""
    import types
    import urllib.parse, urllib.request, urllib.error
    from lunamoth.server import supervisor as SV

    root = (tmp_path / "sessions" / "probe").resolve()
    (root / "sandbox" / "workspace" / "works").mkdir(parents=True)
    img = root / "sprite.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(SV.S, "list_sessions", lambda: [types.SimpleNamespace(root=root, sandbox_dir=root / "sandbox")])

    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="t", supervisor=None)
    base = f"http://127.0.0.1:{port}/asset?p=" + urllib.parse.quote(str(img))

    def status(url, cookie=None):
        req = urllib.request.Request(url)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        # The comprehensive auth gate (supersedes SEC-1): 401 for unauth, the
        # lm_auth SameSite cookie OR the ?token= query unlocks.
        assert status(base) == 401                       # no token, no cookie → refused
        assert status(base + "&token=t") == 200           # query token unlocks
        assert status(base, cookie="lm_auth=t") == 200    # SameSite cookie unlocks
        assert status(base, cookie="lm_auth=wrong") == 401
        assert status(base, cookie="=bad;; junk") == 401  # malformed Cookie header → no crash, refused
    finally:
        srv.shutdown()


def test_asset_route_open_when_no_server_token(tmp_path, monkeypatch):
    """Dev mode (no token configured): /asset stays open, as it was before SEC-1."""
    import types
    import urllib.parse, urllib.request, urllib.error
    from lunamoth.server import supervisor as SV

    root = (tmp_path / "sessions" / "p").resolve()
    (root / "sandbox" / "workspace").mkdir(parents=True)
    img = root / "avatar.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(SV.S, "list_sessions", lambda: [types.SimpleNamespace(root=root, sandbox_dir=root / "sandbox")])
    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="", supervisor=None)
    try:
        url = f"http://127.0.0.1:{port}/asset?p=" + urllib.parse.quote(str(img))
        assert urllib.request.urlopen(url, timeout=5).status == 200  # open, no token required
    finally:
        srv.shutdown()


def test_asset_route_never_leaks_session_secrets(tmp_path, monkeypatch):
    """SECURITY: /asset must NOT serve a session's config.json (the provider
    api_key) or transcript.db — non-images under the session ROOT. Only the
    chara's workspace/assets non-images (send_file docs) and card-art images are
    served. Closes the unauthenticated-key-leak hole."""
    import types
    import urllib.parse, urllib.request, urllib.error
    from lunamoth.server import supervisor as SV

    # A fake session laid out like a real one.
    root = (tmp_path / "sessions" / "probe").resolve()
    sb = root / "sandbox"
    (sb / "workspace" / "works").mkdir(parents=True)
    (sb / "assets").mkdir(parents=True)
    (root / "config.json").write_text('{"api_key":"sk-SECRET-LEAK"}', encoding="utf-8")
    (root / "session.json").write_text("{}", encoding="utf-8")
    (sb / "transcript.db").write_text("PRIVATE CHAT", encoding="utf-8")
    (root / "sprite.png").write_bytes(b"\x89PNG\r\n\x1a\n")          # living-chara art sidecar
    (sb / "workspace" / "works" / "report.pdf").write_bytes(b"%PDF-1.4 doc")  # a send_file doc
    (sb / "workspace" / "works" / "art.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    meta = types.SimpleNamespace(root=root, sandbox_dir=sb)
    monkeypatch.setattr(SV.S, "list_sessions", lambda: [meta])

    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="t", supervisor=None)

    def get(abspath):
        url = f"http://127.0.0.1:{port}/asset?token=t&p=" + urllib.parse.quote(str(abspath))
        try:
            r = urllib.request.urlopen(url, timeout=5)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    try:
        # the secrets — all refused, body never served
        assert get(root / "config.json")[0] == 404
        assert get(root / "session.json")[0] == 404
        assert get(sb / "transcript.db")[0] == 404
        # legitimate: card-art sidecar image + a send_file doc/image under workspace
        assert get(root / "sprite.png")[0] == 200
        assert get(sb / "workspace" / "works" / "report.pdf")[0] == 200
        assert get(sb / "workspace" / "works" / "art.png")[0] == 200
        # belt-and-suspenders: even if a secret were under workspace, deny by name
        (sb / "workspace" / "config.json").write_text('{"api_key":"x"}', encoding="utf-8")
        assert get(sb / "workspace" / "config.json")[0] == 404
    finally:
        srv.shutdown()


def test_gateway_platform_rows_merge_config_and_live(tmp_path):
    """The overview gets one row per CONFIGURED platform: its own enabled flag
    (legacy-inherited when absent) merged with the host's live per-platform state.
    A configured-but-disabled platform still shows up (enabled False, stopped)."""
    gw = _make_gateway(tmp_path, _FakeSupervisor(None))
    cfg = {
        "enabled": True,
        "adapters": {
            "weixin": {"enabled": True},
            "qq": {"enabled": False, "url": "ws://x"},
            "telegram": {},  # no per-platform flag → inherits legacy top-level enabled
        },
    }
    (tmp_path / "messaging.json").write_text(json.dumps(cfg), encoding="utf-8")
    live = [
        {"platform": "weixin", "state": "running"},
        {"platform": "telegram", "state": "needs_login"},
    ]
    rows = {r["platform"]: r for r in gw._platform_rows(live)}
    assert rows["weixin"] == {"platform": "weixin", "enabled": True, "state": "running"}
    assert rows["qq"] == {"platform": "qq", "enabled": False, "state": "stopped"}
    assert rows["telegram"] == {"platform": "telegram", "enabled": True, "state": "needs_login"}
