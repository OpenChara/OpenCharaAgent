import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from lunamoth.protocol.api import AttachInfo, Reply, StateSnapshot
from lunamoth.protocol.events import TextDelta


@dataclass
class DummyHandle:
    permission_hook: Any = None
    clarify_hook: Any = None
    attached: bool = False
    detached: bool = False

    def set_permission_hook(self, hook):
        self.permission_hook = hook

    def set_clarify_hook(self, hook):
        self.clarify_hook = hook

    def set_present(self, present: bool):
        self.attached = bool(present)

    def attach(self, present: bool = True):
        self.attached = True
        return AttachInfo(
            char_name="test",
            lang="en",
            mode="chat",
            show_thinking=False,
            restored=(),
            opening="none",
            opening_text="",
        )

    def detach(self):
        self.detached = True
        self.attached = False

    def stream_user(self, text: str):
        yield TextDelta("echo: ")
        yield TextDelta(text)

    def stream_idle(self):
        yield TextDelta("idle", "muse")

    def command(self, line: str):
        return Reply(True, f"ran {line}", {"line": line}, verbose=False)

    def snapshot(self, fresh: bool = False):
        return StateSnapshot(
            char_name="test",
            lang="en",
            mode="chat",
            provider="mock",
            model="mock",
            reasoning="medium",
            reasoning_supported=False,
            show_thinking=False,
            user_name="operator",
            isolation="sandbox",
            net_on=False,
            rest_until=0.0,
            quiet=300,
            patience=600.0,
            embodiment="literal",
            website=False,
            context_tokens=0,
            context_max=100,
            memory_chars=0,
            memory_max=1,
            memory_text="",
            memory_path="/tmp/memory",
            sandbox_root="/tmp/sandbox",
            workspace_root="/tmp/sandbox/workspace",
        )


def make_dispatcher(handle=None):
    frames = []
    lock = threading.Lock()

    def write(frame):
        # Ensure all frames are JSON-shaped, mirroring a real transport.
        json.loads(json.dumps(frame, ensure_ascii=False))
        with lock:
            frames.append(frame)
        return True

    from lunamoth.server.dispatch import JsonRpcDispatcher

    dispatch = JsonRpcDispatcher(write, handle=handle or DummyHandle())
    return dispatch, frames


def wait_response(frames, rid, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for frame in frames:
            if frame.get("id") == rid:
                return frame
        time.sleep(0.01)
    raise AssertionError(f"no response for id {rid}: {frames}")


def test_hello_includes_protocol_version():
    from lunamoth.server.dispatch import hello_frame

    assert hello_frame()["params"]["protocol_version"] == 1


def test_real_mock_handle_streams_protocol_events(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))

    from lunamoth.protocol.api import CharaHandle
    from lunamoth.session.settings import Settings

    handle = CharaHandle(Settings(provider="mock", character_path="", toolpack=""))
    dispatch, frames = make_dispatcher(handle)
    attached = dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {"present": True}})
    assert attached["result"]["char_name"]

    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "status"}}) is None
    done = wait_response(frames, 2)
    assert done["result"] == {"ok": True, "interrupted": False}
    text_events = [f["params"] for f in frames if f.get("method") == "event" and f["params"].get("type") == "text"]
    assert text_events
    assert "".join(e["text"] for e in text_events)


def test_attach_send_roundtrip_streams_text_notifications():
    dispatch, frames = make_dispatcher()
    resp = dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {"present": True}})
    assert resp["result"]["mode"] == "chat"

    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "hi"}}) is None
    done = wait_response(frames, 2)
    assert done["result"] == {"ok": True, "interrupted": False}
    events = [f for f in frames if f.get("method") == "event"]
    assert [e["params"] for e in events] == [
        {"type": "text", "text": "echo: ", "channel": "say", "superchat": False},
        {"type": "text", "text": "hi", "channel": "say", "superchat": False},
    ]


def test_command_and_snapshot_return_dataclass_dicts():
    dispatch, _frames = make_dispatcher()
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    cmd = dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "command", "params": {"line": "/status"}})
    assert cmd["result"] == {"ok": True, "text": "ran /status", "data": {"line": "/status"}, "verbose": False}
    snap = dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "snapshot", "params": {}})
    assert snap["result"]["provider"] == "mock"
    assert snap["result"]["workspace_root"].endswith("workspace")


class SlowHandle(DummyHandle):
    def __init__(self):
        super().__init__()
        self.closed = threading.Event()

    def stream_user(self, text: str):
        try:
            yield TextDelta("first")
            time.sleep(1.0)
            yield TextDelta("second")
        finally:
            self.closed.set()


def test_interrupt_mid_stream_closes_generator_and_marks_response():
    handle = SlowHandle()
    dispatch, frames = make_dispatcher(handle)
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "slow"}}) is None
    deadline = time.time() + 1.0
    while time.time() < deadline and not [f for f in frames if f.get("method") == "event"]:
        time.sleep(0.01)
    interrupt = dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "interrupt", "params": {}})
    assert interrupt["result"]["interrupted"] is True
    done = wait_response(frames, 2, timeout=2.0)
    assert done["result"] == {"ok": True, "interrupted": True}
    assert handle.closed.wait(0.2)
    assert [f["params"]["text"] for f in frames if f.get("method") == "event"] == ["first"]


class PermissionHandle(DummyHandle):
    def stream_user(self, text: str):
        granted = self.permission_hook("network", "need network", "", 5)
        yield TextDelta("granted" if granted else "denied")


def test_permission_ask_then_reply_granted():
    dispatch, frames = make_dispatcher(PermissionHandle())
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "ask"}}) is None
    deadline = time.time() + 1.0
    ask = None
    while time.time() < deadline:
        asks = [f for f in frames if f.get("method") == "permission_ask"]
        if asks:
            ask = asks[0]
            break
        time.sleep(0.01)
    assert ask is not None
    assert ask["params"]["kind"] == "network"
    pid = ask["params"]["id"]
    reply = dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "permission_reply", "params": {"id": pid, "granted": True}})
    assert reply["result"] == {"ok": True}
    done = wait_response(frames, 2)
    assert done["result"]["interrupted"] is False
    assert any(f.get("method") == "event" and f["params"]["text"] == "granted" for f in frames)


class ClarifyHandle(DummyHandle):
    def stream_user(self, text: str):
        answer = self.clarify_hook("which way?", ["left", "right"])
        yield TextDelta(f"chose:{answer}")


def test_clarify_ask_then_reply():
    dispatch, frames = make_dispatcher(ClarifyHandle())
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "ask"}}) is None
    deadline = time.time() + 1.0
    ask = None
    while time.time() < deadline:
        asks = [f for f in frames if f.get("method") == "clarify_ask"]
        if asks:
            ask = asks[0]
            break
        time.sleep(0.01)
    assert ask is not None
    assert ask["params"]["question"] == "which way?"
    assert ask["params"]["choices"] == ["left", "right"]
    pid = ask["params"]["id"]
    reply = dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "clarify_reply", "params": {"id": pid, "answer": "right"}})
    assert reply["result"] == {"ok": True}
    done = wait_response(frames, 2)
    assert done["result"]["interrupted"] is False
    assert any(f.get("method") == "event" and f["params"]["text"] == "chose:right" for f in frames)


def test_second_attach_is_adoption_safe_presence_update():
    handle = DummyHandle()
    dispatch, _frames = make_dispatcher(handle)
    ok = dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {"present": True}})
    assert "result" in ok
    again = dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "attach", "params": {"present": True}})
    assert again["result"] == ok["result"]
    dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "detach", "params": {}})
    ok2 = dispatch.dispatch({"jsonrpc": "2.0", "id": 4, "method": "attach", "params": {}})
    assert "result" in ok2


def test_presence_set_is_idempotent():
    handle = DummyHandle()
    dispatch, _frames = make_dispatcher(handle)
    resp = dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "presence.set", "params": {"present": True}})
    assert resp["result"] == {"ok": True, "present": True}
    resp = dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "presence.set", "params": {"present": False}})
    assert resp["result"] == {"ok": True, "present": False}


def test_ws_auth_query_and_first_message():
    from lunamoth.server.ws import auth_message_ok, query_auth_ok

    assert query_auth_ok("/api/ws?token=s3cr3t", "s3cr3t")
    assert not query_auth_ok("/api/ws?token=wrong", "s3cr3t")

    ok, response = auth_message_ok(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "auth", "params": {"token": "s3cr3t"}}),
        "s3cr3t",
    )
    assert ok and response["result"] == {"ok": True, "protocol_version": 1}

    ok, response = auth_message_ok(
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "auth", "params": {"token": "wrong"}}),
        "s3cr3t",
    )
    assert not ok
    assert response["error"]["code"] == -32021


class SlowIdleHandle(DummyHandle):
    """A long idle turn the human should be able to supersede by speaking."""
    def __init__(self):
        super().__init__()
        self.idle_closed = threading.Event()

    def stream_idle(self):
        try:
            yield TextDelta("musing", "muse")
            time.sleep(1.0)
            yield TextDelta("more musing", "muse")
        finally:
            self.idle_closed.set()

    def stream_user(self, text: str):
        yield TextDelta("reply: ")
        yield TextDelta(text)


def test_human_send_supersedes_an_in_flight_idle_turn():
    """The chara is doing its own thing (idle stream); the human sends a
    message. That must stop the idle turn and take over, not fail with
    'a stream is already in flight' (-32011) — the interrupt-the-chara bug."""
    handle = SlowIdleHandle()
    dispatch, frames = make_dispatcher(handle)
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    # server-side idle turn begins (the chara's own work)
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "idle", "params": {}}) is None
    deadline = time.time() + 1.0
    while time.time() < deadline and not [f for f in frames if f.get("method") == "event"]:
        time.sleep(0.01)
    # the human speaks mid-idle — must be accepted, not refused
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "send", "params": {"text": "hi"}}) is None
    assert handle.idle_closed.wait(1.0)  # the idle turn was wound down
    done = wait_response(frames, 3, timeout=2.0)
    assert done["result"]["ok"] is True
    texts = [f["params"]["text"] for f in frames if f.get("method") == "event"]
    assert "reply: " in texts and "hi" in texts  # the human's turn ran


def test_two_human_turns_still_collide():
    """Superseding is only idle→human; two human turns at once is a real
    client bug and still errors."""
    handle = SlowHandle()
    dispatch, frames = make_dispatcher(handle)
    dispatch.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert dispatch.dispatch({"jsonrpc": "2.0", "id": 2, "method": "send", "params": {"text": "a"}}) is None
    deadline = time.time() + 1.0
    while time.time() < deadline and not [f for f in frames if f.get("method") == "event"]:
        time.sleep(0.01)
    resp = dispatch.dispatch({"jsonrpc": "2.0", "id": 3, "method": "send", "params": {"text": "b"}})
    assert resp["error"]["code"] == -32011


# ---- #29 slow-client backpressure on the direct serve --stdio + ws path -----

def test_wssink_write_is_non_blocking_and_drains_via_task():
    """`write` from the agent thread must enqueue and return at once; a
    background drain task performs the actual ws.send."""
    import asyncio

    from lunamoth.server.ws import _WSSink

    sent: list[str] = []

    class FastWS:
        async def send(self, raw):  # noqa: ANN001
            sent.append(raw)

    async def go():
        loop = asyncio.get_running_loop()
        sink = _WSSink(FastWS(), loop)
        sink.start_drain()
        # Simulate the agent thread handing frames over.
        def producer():
            for i in range(5):
                assert sink.write({"method": "event", "params": {"i": i}}) is True
        await asyncio.to_thread(producer)
        # Let the drain task flush.
        for _ in range(50):
            if len(sent) == 5:
                break
            await asyncio.sleep(0.01)
        assert len(sent) == 5
        assert sink.dropped == 0
        sink.close()

    asyncio.run(asyncio.wait_for(go(), timeout=5.0))


def test_wssink_stalled_client_does_not_block_writer_and_evicts():
    """A browser that never reads must not block the producing thread: writes
    return immediately, the bounded buffer overflows by eviction, and after
    sustained overflow the sink declares the client wedged and closes."""
    import asyncio
    import time as _time

    from lunamoth.server import ws as WS
    from lunamoth.server.ws import _WSSink

    class StalledWS:
        async def send(self, raw):  # noqa: ANN001
            await asyncio.sleep(3600)  # never returns — wedged client

    async def go():
        loop = asyncio.get_running_loop()
        # Shrink the limits so the test stays fast.
        old_max, old_strikes = WS._SINK_BUFFER_MAX, WS._SINK_OVERFLOW_STRIKES
        WS._SINK_BUFFER_MAX, WS._SINK_OVERFLOW_STRIKES = 4, 16
        try:
            sink = _WSSink(StalledWS(), loop)
            sink.start_drain()

            results: list[bool] = []

            def producer():
                start = _time.monotonic()
                for i in range(200):
                    results.append(sink.write({"method": "event", "params": {"i": i}}))
                # 200 non-blocking writes against a wedged client must be fast.
                return _time.monotonic() - start

            elapsed = await asyncio.to_thread(producer)
            # Give the loop a moment to process the queued enqueues.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if sink._closed:
                    break
            assert elapsed < 2.0  # never blocked on the 10s-per-frame send
            assert sink.dropped > 0  # eviction happened
            # Sustained overflow ⇒ wedged ⇒ sink closed ⇒ later writes report False.
            assert sink.write({"method": "event"}) is False
        finally:
            WS._SINK_BUFFER_MAX, WS._SINK_OVERFLOW_STRIKES = old_max, old_strikes

    asyncio.run(asyncio.wait_for(go(), timeout=10.0))
