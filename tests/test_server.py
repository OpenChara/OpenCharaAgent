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
    attached: bool = False
    detached: bool = False

    def set_permission_hook(self, hook):
        self.permission_hook = hook

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
            user_present=True,
            rest_until=0.0,
            quiet=300,
            tempo=1.0,
            patience=600.0,
            embodiment="literal",
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
        {"type": "text", "text": "echo: ", "channel": "say"},
        {"type": "text", "text": "hi", "channel": "say"},
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
