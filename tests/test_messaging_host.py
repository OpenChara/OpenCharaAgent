"""The in-process messaging host shares the serve child's ONE agent.

A WeChat message must (a) run a turn on the dispatcher's handle, (b) stream that
turn's events onto the transport (so the desktop app sees it live), and (c)
deliver only the say-channel text back to the adapter.
"""
from __future__ import annotations

from chara.messaging.base import Adapter, InboundMessage
from chara.protocol import MUSE, SAY, TextDelta, ThinkDelta, ToolEnd, ToolStart
from chara.server.dispatch import JsonRpcDispatcher
from chara.server.messaging_host import MessagingHost


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

    def resolve_media(self, rel):
        return None  # no files by default; media tests override

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


def test_wechat_empty_allowlist_is_owner_only_not_open():
    # Empty allowed_senders = OWNER-ONLY, not open: a stranger is refused (no turn),
    # so a public channel can't drive a tool-capable agent. (The bound owner is still
    # always let through — see test_empty_allowlist_lets_the_owner_through.)
    handle = _Handle()
    adapter = _Adapter()                 # owner_id() = "" → no owner resolvable
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    host = MessagingHost(dispatch, "/tmp/x.json")
    dispatch.set_messaging_host(host)
    host._allowed = set()                # nothing configured, no owner

    host._process(adapter, InboundMessage("anyone", "Stranger", "hi"))
    assert handle.user_calls == []       # stranger NOT run
    assert "hi there" not in adapter.sent


class _OwnedAdapter(_Adapter):
    """An adapter that knows its bound owner (e.g. WeChat's logged-in account)."""

    def owner_id(self):
        return "me"


def test_empty_allowlist_lets_the_owner_through():
    # The owner is always allowed, so an empty list means "only me" — the WeChat
    # case where the opaque owner id can't be typed into the allow-list.
    handle = _Handle()
    adapter = _OwnedAdapter()
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    host = MessagingHost(dispatch, "/tmp/x.json")
    dispatch.set_messaging_host(host)
    host._allowed = set()                # empty, but the adapter resolves an owner

    host._process(adapter, InboundMessage("me", "Me", "hi"))
    assert handle.user_calls == ["hi"]   # owner allowed despite the empty list
    host._process(adapter, InboundMessage("stranger", "X", "yo"))
    assert handle.user_calls == ["hi"]   # a non-owner is still refused


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
    import chara.server.messaging_host as mh

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
            from chara.protocol import SAY, TextDelta
            yield TextDelta("reply", SAY)

        @staticmethod
        def snapshot():
            raise RuntimeError("no snapshot in this stub")

        @staticmethod
        def resolve_media(rel):
            return None

    def emit_peer_message(self, *a, **k):
        pass

    def run_stream_sync(self, kind, make, on_event):
        from chara.server.dispatch import RpcError
        self.calls += 1
        if self.calls <= self._fail:
            raise RpcError(-32011, "a stream is already in flight")
        for ev in make():
            on_event(ev)


def test_inbound_waits_and_retries_when_agent_busy(monkeypatch):
    import chara.server.messaging_host as mh
    monkeypatch.setattr(mh.time, "sleep", lambda *_: None)  # no real waiting
    dispatch = _BusyDispatch(fail=2)
    adapter = _Adapter()
    host = MessagingHost(dispatch, "/tmp/x.json")
    host._allowed = {"u1"}
    host._process(adapter, InboundMessage("u1", "Alice", "hi"))
    assert dispatch.calls == 3                       # retried twice, then ran
    assert any("reply" in s for s in adapter.sent)   # message NOT dropped


def test_inbound_busy_note_when_agent_never_frees(monkeypatch):
    import chara.server.messaging_host as mh
    monkeypatch.setattr(mh.time, "sleep", lambda *_: None)
    dispatch = _BusyDispatch(fail=999)               # never frees
    adapter = _Adapter()
    host = MessagingHost(dispatch, "/tmp/x.json")
    host._allowed = {"u1"}
    host._process(adapter, InboundMessage("u1", "Alice", "hi"))
    # ack + an honest "still busy" note — never silent ack-then-nothing
    assert any("busy" in s.lower() or "还在忙" in s for s in adapter.sent)


# ---- ① a MEDIA: marker over the gateway: real media OR an honest note (never silent) ----


class _AttachHandle(_Handle):
    """Streams a say reply with a MEDIA: marker (hermes shape) and resolves it to a
    real local path inside the sandbox."""
    def stream_user(self, text):
        self.user_calls.append(text)
        yield TextDelta("here it is\n", SAY)
        yield TextDelta("MEDIA:works/foo.png", SAY)

    def resolve_media(self, rel):
        return "/tmp/foo.png" if rel == "works/foo.png" else None


class _MediaAdapter(_Adapter):
    def __init__(self):
        super().__init__()
        self.media: list = []
        self.images: list = []

    def send_media(self, source, mime="", caption=""):
        self.media.append((source, mime, caption))

    def send_image(self, url, caption=""):
        self.images.append((url, caption))


class _ImageUrlHandle(_Handle):
    """Streams a say reply with a remote markdown image (hermes ![alt](url) path)."""
    def stream_user(self, text):
        self.user_calls.append(text)
        yield TextDelta("look: ![a](https://fal.media/x.png)", SAY)


def test_remote_image_url_sent_natively_when_adapter_supports_it():
    handle = _ImageUrlHandle()
    adapter = _MediaAdapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    assert adapter.images == [("https://fal.media/x.png", "")]   # sent as a native photo
    assert not any("![a]" in s for s in adapter.sent)            # markdown stripped from text


def test_remote_image_url_falls_back_to_link_text_when_unsupported():
    handle = _ImageUrlHandle()
    adapter = _Adapter()  # default send_image raises DeliveryDeferred
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    assert any("https://fal.media/x.png" in s for s in adapter.sent)  # link survives as text


def test_media_marker_falls_back_to_honest_note_when_channel_has_no_media():
    handle = _AttachHandle()
    adapter = _Adapter()  # default send_media raises DeliveryDeferred
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    assert any("here it is" in s for s in adapter.sent)        # the say text (marker stripped)
    assert not any("MEDIA:" in s for s in adapter.sent)         # the marker never shows as text
    assert any("foo.png" in s for s in adapter.sent)            # honest 'file generated' note, NOT a silent drop


def test_media_marker_uses_send_media_when_the_adapter_supports_it():
    handle = _AttachHandle()
    adapter = _MediaAdapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    # extracted the MEDIA: marker, resolved it via the sandbox, and uploaded the file
    assert adapter.media == [("/tmp/foo.png", "image/png", "")]
    assert any("here it is" in s for s in adapter.sent)
    assert not any("MEDIA:" in s for s in adapter.sent)


def test_missing_media_marker_surfaces_honest_note():
    # A MEDIA: marker that doesn't resolve to a real file → honest note, never silent.
    class _Missing(_AttachHandle):
        def resolve_media(self, rel):
            return None

    handle = _Missing()
    adapter = _MediaAdapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._process(adapter, InboundMessage("u1", "Alice", "show me"))
    assert adapter.media == []                                   # nothing uploaded
    assert any("foo.png" in s for s in adapter.sent)             # but the user is told


# ---- superchat → gateway: PROACTIVE idle-turn say is pushed to the platform ----

def test_proactive_idle_superchat_pushed_to_gateway():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    # a speak emits a superchat-marked say; ordinary say (non-superchat) and muse don't leave
    host._on_stream_event("idle", TextDelta("hey — I made progress", SAY, superchat=True), False, False)
    host._on_stream_event("idle", TextDelta(" plain narration", SAY), False, False)  # not a speak → skipped
    host._on_stream_event("idle", TextDelta(" still going", MUSE), False, False)  # muse skipped
    host._on_stream_event("idle", None, True, False)  # turn end → flush
    assert any("made progress" in s for s in adapter.sent)
    assert not any("plain narration" in s for s in adapter.sent)
    assert not any("still going" in s for s in adapter.sent)  # panoramic muse never leaves


def test_idle_superchat_not_pushed_when_interrupted():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    host._on_stream_event("idle", TextDelta("half a thought", SAY, superchat=True), False, False)
    host._on_stream_event("idle", None, True, True)  # interrupted → discard
    assert adapter.sent == []


def test_desktop_send_turn_pushes_the_speak_but_not_the_reply():
    adapter = _Adapter()
    host = MessagingHost(None, "/tmp/x.json")
    host._adapters = [adapter]
    # a desktop 'send' turn: the ordinary reply is operator-local (must NOT leave),
    # but a deliberate speak during it reaches every gateway (the fix).
    host._on_stream_event("send", TextDelta("desktop-only reply", SAY), False, False)
    host._on_stream_event("send", TextDelta("everyone, news!", SAY, superchat=True), False, False)
    host._on_stream_event("send", None, True, False)
    assert any("everyone, news!" in s for s in adapter.sent)
    assert not any("desktop-only reply" in s for s in adapter.sent)


def test_dispatcher_observer_wires_speak_to_the_host():
    # integration: the real dispatcher's stream tap drives the host push.
    handle = _Handle()
    adapter = _Adapter()
    dispatch, host = _host_with_adapter(handle, adapter, [])
    host._adapters = [adapter]
    dispatch.set_stream_observer(host._on_stream_event)
    dispatch.run_stream_sync(
        "idle",
        lambda: iter([TextDelta("a real superchat", SAY, superchat=True), TextDelta(" inner", MUSE)]),
        None,
    )
    assert any("a real superchat" in s for s in adapter.sent)
    assert not any("inner" in s for s in adapter.sent)


def test_inbound_speak_fans_out_to_the_other_gateways_once():
    # A speak made WHILE replying to one platform also reaches the OTHER gateways;
    # the source gets it only inside its own reply (no double-send), the other
    # gateway gets ONLY the speak — never the platform-private reply prose.
    class _SpeakHandle(_Handle):
        def stream_user(self, text):
            yield TextDelta("a private reply ", SAY)
            yield TextDelta("everyone, big news", SAY, superchat=True)

    handle = _SpeakHandle()
    source = _Adapter("weixin")
    other = _Adapter("telegram")
    dispatch, host = _host_with_adapter(handle, source, [])
    host._adapters = [source, other]
    host._process(source, InboundMessage("u1", "Alice", "hi"))

    # source: the full reply (private prose + the speak), and exactly once
    assert any("a private reply" in s for s in source.sent)
    assert any("everyone, big news" in s for s in source.sent)
    assert sum(s.count("everyone, big news") for s in source.sent) == 1
    # other gateway: ONLY the speak, never the private reply prose
    assert any("everyone, big news" in s for s in other.sent)
    assert not any("a private reply" in s for s in other.sent)


class _BlockingAdapter(_Adapter):
    """run() blocks until close(), like a real adapter holding a connection — so
    we can prove an unchanged platform is NOT torn down on a sibling toggle."""

    def __init__(self, name="weixin"):
        super().__init__(name)
        import threading

        self._gate = threading.Event()
        self.run_count = 0
        self.closed = False

    def needs_login(self):
        return False

    def run(self, inbox):
        self.run_count += 1
        self._gate.wait(5.0)  # hold the "connection" open until close()

    def close(self):
        self.closed = True
        self._gate.set()


def test_reconcile_toggles_one_platform_without_restarting_others(tmp_path, monkeypatch):
    """Disabling qq while weixin stays on must stop ONLY qq — weixin keeps its same
    running adapter/thread (run_count unchanged), so there is no reconnect blip."""
    import chara.server.messaging_host as mh

    handle = _Handle()
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    cfg = tmp_path / "messaging.json"
    cfg.write_text(
        '{"enabled": true, "adapters": {"weixin": {"enabled": true}, "qq": {"enabled": true}}}',
        encoding="utf-8",
    )
    host = MessagingHost(dispatch, cfg)

    wx = _BlockingAdapter("weixin")
    qq = _BlockingAdapter("qq")

    def fake_make(c):
        ad = c.get("adapters", {})
        out = []
        if ad.get("weixin", {}).get("enabled"):
            out.append(wx)
        if ad.get("qq", {}).get("enabled"):
            out.append(qq)
        return out

    monkeypatch.setattr(mh, "make_adapters", fake_make)

    try:
        st = host.start()
        assert st["state"] == "running"
        assert sorted(p["platform"] for p in st["platforms"]) == ["qq", "weixin"]
        assert wx.run_count == 1 and qq.run_count == 1

        # Disable qq, keep weixin → reconcile (not a full rebuild).
        cfg.write_text(
            '{"enabled": true, "adapters": {"weixin": {"enabled": true}, "qq": {"enabled": false}}}',
            encoding="utf-8",
        )
        st2 = host.start()
        assert [p["platform"] for p in st2["platforms"]] == ["weixin"]
        assert qq.closed is True        # qq was stopped
        assert wx.closed is False       # weixin untouched...
        assert wx.run_count == 1        # ...and NOT restarted (no blip)
    finally:
        host.stop()


class _CrashingAdapter(_Adapter):
    def run(self, inbox):
        raise RuntimeError("gateway link exploded")


def test_adapter_crash_is_reflected_in_status_not_stale_running(tmp_path):
    """A dead adapter thread must surface as an 'error' platform row — the host
    used to keep reporting 'running' until the whole child restarted."""
    handle = _Handle()
    frames: list[dict] = []
    dispatch = JsonRpcDispatcher(frames.append, handle=handle)
    host = MessagingHost(dispatch, tmp_path / "messaging.json")

    crashing = _CrashingAdapter("qq")
    with host._lock:
        host._start_one(crashing)
    th = host._threads_by_name.get("qq")
    if th is not None:
        th.join(timeout=2.0)

    st = host.status()
    assert st["state"] == "error"                       # sole platform died
    assert "gateway link exploded" in st["detail"]
    rows = {p["platform"]: p for p in st["platforms"]}
    assert rows["qq"]["state"] == "error"
    assert "gateway link exploded" in rows["qq"]["detail"]

    # A restart of the platform clears the crash record.
    healthy = _BlockingAdapter("qq")
    with host._lock:
        host._start_one(healthy)
        host._recompute_state()
    try:
        st2 = host.status()
        assert st2["state"] == "running"
        assert {p["platform"]: p["state"] for p in st2["platforms"]} == {"qq": "running"}
    finally:
        host.stop()


def test_inbound_turn_marks_turn_active_for_the_supervisor():
    """status().turn_active is True exactly while an inbound platform turn runs —
    the supervisor reads it so autonomy-off never interrupts a live reply."""
    seen: list[bool] = []

    class _ProbeHandle(_Handle):
        def stream_user(self, text):
            seen.append(bool(host.status().get("turn_active")))
            yield TextDelta("hi there", SAY)

    handle = _ProbeHandle()
    adapter = _Adapter()
    frames: list[dict] = []
    dispatch, host = _host_with_adapter(handle, adapter, frames)

    assert host.status()["turn_active"] is False
    host._process(adapter, InboundMessage("u1", "Alice", "hello"))
    assert seen == [True]                        # active while the turn streamed
    assert host.status()["turn_active"] is False  # cleared after the reply


def test_stranger_never_becomes_the_speak_destination():
    """2026-07-02 P1: a stranger's DM must not hijack the proactive-speak
    destination. The durable peer is committed via remember_peer only AFTER the
    sender passes the allow-list; the refusal still reaches the stranger via
    the ephemeral reply target."""
    handle = _Handle()

    class _PeerAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.peer = ""

        def remember_peer(self, message):
            self.peer = str(message.sender_id)

    adapter = _PeerAdapter()
    frames: list[dict] = []
    dispatch, host = _host_with_adapter(handle, adapter, frames)

    host._process(adapter, InboundMessage("stranger", "Mallory", "yo"))
    assert adapter.peer == ""            # the refused sender was never committed
    assert len(adapter.sent) == 1        # but the refusal still went out

    host._process(adapter, InboundMessage("u1", "Owner", "hi"))
    assert adapter.peer == "u1"          # the authorized sender is the destination
