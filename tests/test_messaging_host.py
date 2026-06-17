"""The in-process messaging host shares the serve child's ONE agent.

A WeChat message must (a) run a turn on the dispatcher's handle, (b) stream that
turn's events onto the transport (so the desktop app sees it live), and (c)
deliver only the say-channel text back to the adapter.
"""
from __future__ import annotations

from lunamoth.messaging.base import Adapter, InboundMessage
from lunamoth.protocol import MUSE, SAY, TextDelta, ThinkDelta, ToolEnd, ToolStart
from lunamoth.server.dispatch import JsonRpcDispatcher
from lunamoth.server.messaging_host import MessagingHost


class _Adapter(Adapter):
    max_message_length = 0

    def __init__(self, name="weixin"):
        self._name = name
        self.sent: list[str] = []

    @property
    def name(self):
        return self._name

    def run(self, inbox):
        return None

    def send(self, text: str):
        self.sent.append(text)


class _Handle:
    def __init__(self):
        self.attached = False
        self.user_calls: list[str] = []

    def set_permission_hook(self, hook):
        pass

    def set_clarify_hook(self, hook):
        pass

    def attach(self, present=True):
        self.attached = True
        return {"opening": "adopt"}

    def detach(self):
        self.attached = False

    def stream_user(self, text):
        self.user_calls.append(text)
        yield ThinkDelta("private")
        yield TextDelta("musing ", MUSE)
        yield ToolStart("terminal")
        yield TextDelta("hi there", SAY)
        yield ToolEnd("terminal", summary="x")


def _host_with_adapter(handle, adapter, frames):
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    host = MessagingHost(dispatch, "/tmp/does-not-matter.json")
    dispatch.set_messaging_host(host)
    # Drive the relay path directly (no real adapter I/O / config file).
    host._allowed = {"u1"}
    return dispatch, host


def test_wechat_turn_shares_agent_say_to_adapter_events_to_transport():
    handle = _Handle()
    adapter = _Adapter()
    frames: list[dict] = []
    dispatch, host = _host_with_adapter(handle, adapter, frames)

    host._process(adapter, InboundMessage("u1", "Alice", "hi"))

    # (a) the turn ran on the shared handle
    assert handle.user_calls == ["hi"]
    # (c) only say-channel text went back to WeChat
    assert adapter.sent[-1] == "hi there"   # the chara's say-channel reply
    assert len(adapter.sent) == 2            # preceded by the immediate "got it" receipt
    # the incoming message is surfaced to the app as a peer_message BEFORE the
    # reply events, so the window shows the message that prompted the reply.
    methods = [f.get("method") for f in frames]
    assert "peer_message" in methods
    peer = next(f for f in frames if f.get("method") == "peer_message")
    assert peer["params"]["text"] == "hi" and peer["params"]["source"] == "weixin"
    assert methods.index("peer_message") < methods.index("event")
    # (b) the turn's events streamed onto the transport (the app sees it live),
    # including muse/tool events the adapter intentionally never receives.
    channels = [
        f["params"].get("channel")
        for f in frames
        if f.get("method") == "event" and f["params"].get("type") == "text"
    ]
    assert SAY in channels and MUSE in channels
    kinds = [f["params"].get("type") for f in frames if f.get("method") == "event"]
    assert "tool_start" in kinds and "think" in kinds


def test_wechat_empty_allowlist_is_open():
    # Empty allowed_senders = open (matches the field help). Out of the box,
    # the chara must answer — not refuse everyone.
    handle = _Handle()
    adapter = _Adapter()
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    host = MessagingHost(dispatch, "/tmp/x.json")
    dispatch.set_messaging_host(host)
    host._allowed = set()   # nothing configured

    host._process(adapter, InboundMessage("anyone", "Stranger", "hi"))
    assert handle.user_calls == ["hi"]
    assert adapter.sent[-1] == "hi there"   # the chara's say-channel reply
    assert len(adapter.sent) == 2            # preceded by the immediate "got it" receipt


def test_wechat_unauthorized_sender_refused_not_run():
    handle = _Handle()
    adapter = _Adapter()
    frames: list[dict] = []
    dispatch, host = _host_with_adapter(handle, adapter, frames)

    host._process(adapter, InboundMessage("stranger", "Mallory", "hi"))
    assert handle.user_calls == []          # no turn on the shared agent
    assert len(adapter.sent) == 1           # one refusal
    assert "hi there" not in adapter.sent


class _LoginPendingAdapter(_Adapter):
    """An adapter that isn't logged in yet (a WeChat QR not yet scanned)."""

    def needs_login(self):
        return True

    def run(self, inbox):
        # If the host ever started this, it would open a competing QR session.
        raise AssertionError("a login-pending adapter must not be run by the host")


def test_host_skips_login_pending_adapter_and_reports_needs_login(tmp_path, monkeypatch):
    import lunamoth.server.messaging_host as mh

    handle = _Handle()
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    cfg = tmp_path / "messaging.json"
    cfg.write_text('{"enabled": true, "adapters": {"weixin": {}}}', encoding="utf-8")
    host = MessagingHost(dispatch, cfg)
    # The adapter for this config needs login → host must leave it pending.
    monkeypatch.setattr(mh, "make_adapters", lambda c: [_LoginPendingAdapter()])

    st = host.start()
    assert st["state"] == "needs_login"   # honest, not a false "running"
    assert not handle.attached            # never even attached / opened a session
    handle = _Handle()
    adapter = _Adapter()
    frames: list[dict] = []
    dispatch, host = _host_with_adapter(handle, adapter, frames)

    msg = InboundMessage("u1", "Alice", "hi", message_id="MSG-7")
    host._process(adapter, msg)
    host._process(adapter, msg)  # platform redelivery
    assert handle.user_calls == ["hi"]      # only one turn


# ---- ③ no-drop on collision + honest receipt ---------------------------------

class _BusyDispatch:
    """A dispatcher stub whose run_stream_sync raises -32011 (a desktop turn is
    mid-flight) the first `fail` times, then runs the turn."""

    def __init__(self, fail: int):
        self._fail = fail
        self.calls = 0

    class handle:
        @staticmethod
        def stream_user(text, **kw):
            from lunamoth.protocol import SAY, TextDelta
            yield TextDelta("reply", SAY)

        @staticmethod
        def snapshot():
            raise RuntimeError("no snapshot in this stub")

    def emit_peer_message(self, *a, **k):
        pass

    def run_stream_sync(self, kind, make, on_event):
        from lunamoth.server.dispatch import RpcError
        self.calls += 1
        if self.calls <= self._fail:
            raise RpcError(-32011, "a stream is already in flight")
        for ev in make():
            on_event(ev)


def test_inbound_waits_and_retries_when_agent_busy(monkeypatch):
    import lunamoth.server.messaging_host as mh
    monkeypatch.setattr(mh.time, "sleep", lambda *_: None)  # no real waiting
    dispatch = _BusyDispatch(fail=2)
    adapter = _Adapter()
    host = MessagingHost(dispatch, "/tmp/x.json")
    host._allowed = {"u1"}
    host._process(adapter, InboundMessage("u1", "Alice", "hi"))
    assert dispatch.calls == 3                       # retried twice, then ran
    assert any("reply" in s for s in adapter.sent)   # message NOT dropped


def test_inbound_busy_note_when_agent_never_frees(monkeypatch):
    import lunamoth.server.messaging_host as mh
    monkeypatch.setattr(mh.time, "sleep", lambda *_: None)
    dispatch = _BusyDispatch(fail=999)               # never frees
    adapter = _Adapter()
    host = MessagingHost(dispatch, "/tmp/x.json")
    host._allowed = {"u1"}
    host._process(adapter, InboundMessage("u1", "Alice", "hi"))
    # ack + an honest "still busy" note — never silent ack-then-nothing
    assert any("busy" in s.lower() or "还在忙" in s for s in adapter.sent)


# ---- ① send_file over the gateway: real media OR an honest note (never silent) ----

from lunamoth.protocol import Attachment  # noqa: E402


class _AttachHandle(_Handle):
    def stream_user(self, text):
        self.user_calls.append(text)
        yield TextDelta("here it is", SAY)
        yield Attachment(url="/asset?p=/tmp/foo.png", mime="image/png", name="foo.png", caption="a pic")


class _MediaAdapter(_Adapter):
    def __init__(self):
        super().__init__()
        self.media: list = []

    def send_media(self, source, mime="", caption=""):
        self.media.append((source, mime, caption))


def test_send_file_falls_back_to_honest_note_when_channel_has_no_media():
    handle = _AttachHandle()
    adapter = _Adapter()  # default send_media raises DeliveryDeferred
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    assert any("here it is" in s for s in adapter.sent)        # the say text
    assert any("foo.png" in s for s in adapter.sent)            # honest 'file generated' note, NOT a silent drop


def test_send_file_uses_send_media_when_the_adapter_supports_it():
    handle = _AttachHandle()
    adapter = _MediaAdapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    # resolved the /asset?p= URL to the real local path and uploaded it
    assert adapter.media == [("/tmp/foo.png", "image/png", "a pic")]


# ---- superchat → gateway: PROACTIVE idle-turn say is pushed to the platform ----

def test_proactive_idle_superchat_pushed_to_gateway():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    host._on_stream_event("idle", TextDelta("hey — I made progress", SAY), False, False)
    host._on_stream_event("idle", TextDelta(" still going", MUSE), False, False)  # muse skipped
    host._on_stream_event("idle", None, True, False)  # turn end → flush
    assert any("made progress" in s for s in adapter.sent)
    assert not any("still going" in s for s in adapter.sent)  # panoramic muse never leaves


def test_idle_superchat_not_pushed_when_interrupted():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    host._on_stream_event("idle", TextDelta("half a thought", SAY), False, False)
    host._on_stream_event("idle", None, True, True)  # interrupted → discard
    assert adapter.sent == []


def test_non_idle_turns_are_not_pushed_proactively():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    # a desktop 'send' turn is operator-local — its reply must NOT go to WeChat
    host._on_stream_event("send", TextDelta("desktop-only reply", SAY), False, False)
    host._on_stream_event("send", None, True, False)
    assert adapter.sent == []


def test_dispatcher_observer_wires_idle_say_to_the_host():
    # integration: the real dispatcher's stream tap drives the host push.
    handle = _Handle()
    adapter = _Adapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._adapters = [adapter]
    dispatch.set_stream_observer(host._on_stream_event)
    dispatch.run_stream_sync(
        "idle",
        lambda: iter([TextDelta("a real superchat", SAY), TextDelta(" inner", MUSE)]),
        None,
    )
    assert any("a real superchat" in s for s in adapter.sent)
    assert not any("inner" in s for s in adapter.sent)
