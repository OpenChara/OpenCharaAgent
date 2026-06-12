from __future__ import annotations

import json

from lunamoth.server.supervisor import DriverSlot, FrameRing, IdleGate, LifeState


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
