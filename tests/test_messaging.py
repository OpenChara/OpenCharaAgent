from dataclasses import dataclass
import json
import queue
import time

import pytest

from lunamoth.messaging.base import Adapter, InboundMessage
from lunamoth.messaging.gateway import MessageDeduplicator, MessagingGateway
from lunamoth.messaging.text import split_text
from lunamoth.protocol import MUSE, SAY, TextDelta, ThinkDelta, ToolEnd, ToolStart
from lunamoth.protocol.api import Reply, StateSnapshot


class FakeAdapter(Adapter):
    max_message_length = 2048

    def __init__(self, name="fake"):
        self._name = name
        self.sent = []

    @property
    def name(self):
        return self._name

    def run(self, inbox):
        return None

    def send(self, text: str):
        self.sent.append(text)


@dataclass
class FakeSettings:
    quiet: int = 0


class FakeHandle:
    def __init__(self):
        self.settings = FakeSettings()
        self.user_calls = []
        self.command_calls = []
        self.idle_calls = 0
        self.rest_until = 0.0
        self.quiet = 0
        self.attached = False

    def attach(self, present=True):
        self.attached = True

    def resolve_media(self, rel):
        return None  # no files in these gateway tests

    def snapshot(self):
        return StateSnapshot(
            char_name="Mock",
            lang="en",
            mode="live",
            provider="mock",
            model="mock",
            reasoning="medium",
            reasoning_supported=False,
            show_thinking=False,
            user_name="user",
            isolation="sandbox",
            net_on=False,
            rest_until=self.rest_until,
            quiet=self.quiet,
            patience=600.0,
            embodiment="literal",
            context_tokens=0,
            context_max=1,
            memory_chars=0,
            memory_max=1,
            memory_text="",
            memory_path="",
            sandbox_root="",
            workspace_root="",
        )

    def stream_user(self, text):
        self.user_calls.append(text)
        yield ThinkDelta("private thought")
        yield TextDelta("hello ", MUSE)
        yield ToolStart("terminal")
        yield TextDelta("reply", SAY)
        yield ToolEnd("terminal", summary="secret")

    def stream_idle(self):
        self.idle_calls += 1
        yield TextDelta("muse-only", MUSE)
        yield TextDelta("spoken idle", SAY)

    def command(self, line):
        self.command_calls.append(line)
        return Reply(True, "status ok", {"ok": True})


def test_gateway_inbound_reply_delivers_only_say_channel():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    assert gateway.tick(timeout=0)

    assert handle.user_calls == ["hi"]
    assert adapter.sent == ["reply"]
    assert handle.attached


def test_gateway_idle_speak_delivered_muse_dropped():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=0)

    assert gateway.tick(timeout=0)

    assert handle.idle_calls == 1
    assert adapter.sent == ["spoken idle"]


def test_gateway_idle_pause_is_plain_patience():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=10)

    gateway._next_idle_at = 0.0
    assert gateway.tick(timeout=0)

    remaining = gateway._next_idle_at - time.monotonic()
    assert 9.0 <= remaining <= 10.0


def test_gateway_command_round_trip():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "/status"))
    assert gateway.tick(timeout=0)

    assert handle.command_calls == ["/status"]
    assert handle.user_calls == []
    assert adapter.sent == ["status ok"]


def test_gateway_unknown_sender_refused_once_without_model_call():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("bad", "Mallory", "hi"))
    gateway.enqueue(adapter, InboundMessage("bad", "Mallory", "again"))
    assert gateway.tick(timeout=0)
    assert gateway.tick(timeout=0)

    assert handle.user_calls == []
    assert handle.command_calls == []
    assert len(adapter.sent) == 1
    assert "configured contacts" in adapter.sent[0]


class FlakyAdapter(FakeAdapter):
    """send() raises `failures` times, then succeeds."""

    def __init__(self, failures, exc=None):
        super().__init__("flaky")
        self.failures = failures
        self.exc = exc or ConnectionResetError("socket dropped")
        self.attempts = 0

    def send(self, text: str):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.exc
        self.sent.append(text)


@pytest.fixture
def fast_send_retry(monkeypatch):
    import logging

    import lunamoth.messaging.gateway as gw_mod

    monkeypatch.setattr(gw_mod, "_SEND_RETRY_DELAY", 0.01)
    # obs.setup_logging (run by sibling tests) cuts propagation on "lunamoth";
    # restore it so caplog can see gateway records regardless of test order.
    monkeypatch.setattr(logging.getLogger("lunamoth"), "propagate", True)


def test_send_failure_retries_once_then_delivers(fast_send_retry):
    handle = FakeHandle()
    adapter = FlakyAdapter(failures=1)
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    assert gateway.tick(timeout=0)

    assert adapter.attempts == 2
    assert adapter.sent == ["reply"]  # delivered on the bounded retry


def test_send_failure_drops_that_message_and_gateway_lives(fast_send_retry, caplog):
    # The audit-#31 crash: a non-DeliveryDeferred exception from send()
    # propagated through tick()/run() and killed the gateway process. Now the
    # message is dropped with an ERROR log and the NEXT message still flows.
    import logging

    handle = FakeHandle()
    adapter = FlakyAdapter(failures=2)  # both attempts fail
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    with caplog.at_level(logging.ERROR, logger="lunamoth.messaging.gateway"):
        assert gateway.tick(timeout=0)  # does NOT raise
    assert adapter.sent == []
    assert any("dropped a message" in m for m in caplog.messages)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "again"))
    assert gateway.tick(timeout=0)
    assert adapter.sent == ["reply"]  # the inbox survived the bad send


def test_delivery_deferred_is_logged_not_retried(fast_send_retry, caplog):
    import logging

    from lunamoth.messaging.base import DeliveryDeferred

    handle = FakeHandle()
    adapter = FlakyAdapter(failures=99, exc=DeliveryDeferred("no reply window"))
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    with caplog.at_level(logging.ERROR, logger="lunamoth.messaging.gateway"):
        assert gateway.tick(timeout=0)
    assert adapter.attempts == 1  # a conscious deferral is not retried
    assert any("could not deliver" in m for m in caplog.messages)


def test_chunking_long_outbound_text():
    text = "a" * 2047 + "。" + "b" * 2048 + "c"
    chunks = split_text(text, 2048)
    assert len(chunks) == 3
    assert all(0 < len(chunk) <= 2048 for chunk in chunks)
    assert "".join(chunks) == text

class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        import json
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeWeixinTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req, timeout=0):
        self.requests.append((req, timeout))
        if not self.payloads:
            raise AssertionError("unexpected HTTP request")
        return FakeHTTPResponse(self.payloads.pop(0))


def test_weixin_login_poll_persists_cursor_and_context_token(tmp_path):
    from lunamoth.messaging.weixin import WeixinAdapter

    transport = FakeWeixinTransport([
        {"qrcode": "qr-token", "qrcode_img_content": "img"},
        {
            "status": "confirmed",
            "bot_token": "tok",
            "ilink_bot_id": "bot-1",
            "ilink_user_id": "owner-1",
            "baseurl": "https://ilink.example",
        },
        {
            "ret": 0,
            "errcode": 0,
            "get_updates_buf": "cursor-2",
            "msgs": [
                {
                    "from_user_id": "user-1",
                    "context_token": "ctx-1",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "hello"}},
                        {"type": 3, "voice_item": {"text": "voice words"}},
                    ],
                }
            ],
        },
    ])
    out = []
    adapter = WeixinAdapter(
        {},
        opener=transport,
        state_path=tmp_path / "weixin_state.json",
        output=type("Out", (), {"write": lambda self, s: out.append(s), "flush": lambda self: None})(),
        sleep=lambda _seconds: None,
    )
    inbox = queue.Queue()

    assert adapter.poll_once(inbox) == 1
    msg = inbox.get_nowait()
    assert msg.sender_id == "user-1"
    assert msg.text == "hello\nvoice words"

    state = json.loads((tmp_path / "weixin_state.json").read_text(encoding="utf-8"))
    assert state["token"] == "tok"
    assert state["account_id"] == "bot-1"
    assert state["base_url"] == "https://ilink.example"
    assert state["sync_buf"] == "cursor-2"
    assert state["context_tokens"] == {"user-1": "ctx-1"}
    assert oct((tmp_path / "weixin_state.json").stat().st_mode & 0o777) == "0o600"
    assert "api.qrserver.com" in "".join(out)

    urls = [req.full_url for req, _timeout in transport.requests]
    assert "get_bot_qrcode" in urls[0]
    assert "get_qrcode_status" in urls[1]
    assert "getupdates" in urls[2]
    assert transport.requests[2][0].headers["Authorization"] == "Bearer tok"
    assert transport.requests[2][0].headers["Authorizationtype"] == "ilink_bot_token"
    assert transport.requests[2][0].headers["X-wechat-uin"]


def test_weixin_drops_self_echo_from_own_account(tmp_path):
    """getupdates can surface the bot's OWN sent messages; with an open (empty)
    allow-list those must be dropped, not answered, or the bot loops on itself."""
    from lunamoth.messaging.weixin import WeixinAdapter

    transport = FakeWeixinTransport([
        {"qrcode": "qr-token", "qrcode_img_content": "img"},
        {
            "status": "confirmed",
            "bot_token": "tok",
            "ilink_bot_id": "bot-1",
            "ilink_user_id": "owner-1",
            "baseurl": "https://ilink.example",
        },
        {
            "ret": 0,
            "errcode": 0,
            "get_updates_buf": "cursor-2",
            "msgs": [
                # the bot's own outbound echoed back — must be ignored
                {"from_user_id": "bot-1", "context_token": "ctx-x",
                 "item_list": [{"type": 1, "text_item": {"text": "my own words"}}]},
                # a real user — must reach the chara
                {"from_user_id": "user-1", "context_token": "ctx-1",
                 "item_list": [{"type": 1, "text_item": {"text": "hello"}}]},
            ],
        },
    ])
    adapter = WeixinAdapter(
        {},
        opener=transport,
        state_path=tmp_path / "weixin_state.json",
        output=type("Out", (), {"write": lambda self, s: None, "flush": lambda self: None})(),
        sleep=lambda _seconds: None,
    )
    inbox = queue.Queue()

    adapter.poll_once(inbox)
    msg = inbox.get_nowait()
    assert msg.sender_id == "user-1" and msg.text == "hello"
    assert inbox.empty()  # only the real user surfaced; the self-echo was dropped


def test_weixin_delivers_operator_messages_but_drops_reply_echoes(tmp_path):
    """The bound WeChat id (ilink_user_id) is BOTH the operator typing to the
    chara AND the echo of the chara's own replies. The operator's messages MUST
    reach the chara (the regression: the old guard dropped the whole id, silently
    swallowing everything the operator sent); only an echo of a reply we just
    sent is dropped."""
    from lunamoth.messaging.weixin import WeixinAdapter

    transport = FakeWeixinTransport([
        {"qrcode": "qr-token", "qrcode_img_content": "img"},
        {"status": "confirmed", "bot_token": "tok", "ilink_bot_id": "bot-1",
         "ilink_user_id": "owner-1", "baseurl": "https://ilink.example"},
        {"ret": 0, "errcode": 0, "get_updates_buf": "cursor-2", "msgs": [
            # an echo of a reply the chara just sent — must be dropped
            {"from_user_id": "owner-1", "context_token": "ctx-1",
             "item_list": [{"type": 1, "text_item": {"text": "pong"}}]},
            # the operator's OWN new message (same bound id) — must reach the chara
            {"from_user_id": "owner-1", "context_token": "ctx-1",
             "item_list": [{"type": 1, "text_item": {"text": "ping"}}]},
        ]},
    ])
    adapter = WeixinAdapter(
        {}, opener=transport, state_path=tmp_path / "weixin_state.json",
        output=type("Out", (), {"write": lambda self, s: None, "flush": lambda self: None})(),
        sleep=lambda _seconds: None,
    )
    inbox = queue.Queue()
    adapter._remember_send("pong")   # the chara replied "pong" just before
    adapter.poll_once(inbox)
    got = []
    while not inbox.empty():
        got.append(inbox.get_nowait())
    assert [m.text for m in got] == ["ping"]          # operator delivered, echo dropped
    assert got[0].sender_id == "owner-1"              # ...even though it's the bound id


def test_weixin_reuses_saved_token_and_send_requires_context_token(tmp_path):
    from lunamoth.messaging.base import DeliveryDeferred, InboundMessage
    from lunamoth.messaging.weixin import WeixinAdapter

    state_path = tmp_path / "weixin_state.json"
    state_path.write_text(
        json.dumps(
            {
                "token": "tok",
                "account_id": "bot-1",
                "base_url": "https://ilink.example",
                "sync_buf": "cursor",
                "context_tokens": {"user-1": "ctx-1"},
            }
        ),
        encoding="utf-8",
    )
    transport = FakeWeixinTransport([
        {"ret": 0, "errcode": 0},
    ])
    adapter = WeixinAdapter({}, opener=transport, state_path=state_path)

    adapter.set_reply_target(InboundMessage("user-1", "user-1", "hi"))
    adapter.send("reply text")
    body = json.loads(transport.requests[-1][0].data.decode("utf-8"))
    assert body["msg"]["to_user_id"] == "user-1"
    assert body["msg"]["context_token"] == "ctx-1"
    assert body["msg"]["item_list"] == [{"type": 1, "text_item": {"text": "reply text"}}]

    adapter.set_reply_target(InboundMessage("user-2", "user-2", "hi"))
    with pytest.raises(DeliveryDeferred, match="waiting for the human to say hi first"):
        adapter.send("cannot yet")

    urls = [req.full_url for req, _timeout in transport.requests]
    assert not any("get_bot_qrcode" in url for url in urls)


def test_weixin_session_timeout_marks_relogin_and_surfaces(tmp_path):
    from lunamoth.messaging.base import InboundMessage
    from lunamoth.messaging.weixin import WeixinAdapter

    state_path = tmp_path / "weixin_state.json"
    state_path.write_text(
        json.dumps(
            {
                "token": "tok",
                "account_id": "bot-1",
                "base_url": "https://ilink.example",
                "context_tokens": {"user-1": "ctx-1"},
            }
        ),
        encoding="utf-8",
    )
    transport = FakeWeixinTransport([
        {"ret": 0, "errcode": -14, "errmsg": "session timeout"},
    ])
    adapter = WeixinAdapter({}, opener=transport, state_path=state_path)
    adapter.set_reply_target(InboundMessage("user-1", "user-1", "hi"))

    with pytest.raises(RuntimeError, match="QR re-login is required"):
        adapter.send("hello")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["needs_relogin"] is True


def test_qq_parse_private_text_segments():
    from lunamoth.messaging.qq import parse_onebot_event

    raw = json.dumps(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123456,
            "sender": {"nickname": "Alice"},
            "message": [
                {"type": "text", "data": {"text": "hello"}},
                {"type": "image", "data": {"file": "ignored.jpg"}},
                {"type": "text", "data": {"text": " world"}},
            ],
        }
    )

    msg = parse_onebot_event(raw)

    assert msg is not None
    assert msg.sender_id == "123456"
    assert msg.sender_name == "Alice"
    assert msg.text == "hello world"


def test_qq_ignores_group_messages_for_v1():
    from lunamoth.messaging.qq import parse_onebot_event

    raw = json.dumps(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 42,
            "user_id": 123456,
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
    )

    assert parse_onebot_event(raw) is None


class FakeQQSocket:
    def __init__(self, frames=None, *, error=None, on_error=None):
        self.frames = list(frames or [])
        self.error = error
        self.on_error = on_error
        self.sent = []
        self.closed = False
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        if self.on_error:
            self.on_error()
        raise self.error or TimeoutError()

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


def test_qq_send_frame_shape_and_reply_target():
    from lunamoth.messaging.qq import QQAdapter

    sock = FakeQQSocket()
    adapter = QQAdapter({"url": "ws://127.0.0.1:3001", "peer_id": "999"}, uuid_factory=lambda: "echo-1")
    adapter._socket = sock

    adapter.send("idle hi")
    adapter.set_reply_target(InboundMessage("123", "Alice", "hi"))
    adapter.send("reply hi")

    idle_frame = json.loads(sock.sent[0])
    reply_frame = json.loads(sock.sent[1])
    assert idle_frame == {
        "action": "send_private_msg",
        "params": {"user_id": 999, "message": "idle hi"},
        "echo": "echo-1",
    }
    assert reply_frame == {
        "action": "send_private_msg",
        "params": {"user_id": 123, "message": "reply hi"},
        "echo": "echo-1",
    }


def test_qq_reconnect_backoff_resets_after_success():
    from lunamoth.messaging.qq import QQAdapter

    frames = [
        json.dumps(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 123,
                "message": [{"type": "text", "data": {"text": "ping"}}],
            }
        )
    ]
    attempts = []
    sleeps = []
    sockets = []
    adapter = QQAdapter(
        {"url": "ws://127.0.0.1:3001", "access_token": "secret"},
        sleep=lambda seconds: sleeps.append(seconds),
        recv_timeout=0,
    )

    def connect(url, **kwargs):
        attempts.append((url, kwargs))
        if len(attempts) == 1:
            raise OSError("first drop")
        if len(attempts) == 2:
            sock = FakeQQSocket(error=ConnectionError("second drop"))
            sockets.append(sock)
            return sock
        sock = FakeQQSocket(frames=frames, error=ConnectionError("done"), on_error=adapter.close)
        sockets.append(sock)
        return sock

    adapter._connect_func = connect
    inbox = queue.Queue()

    adapter.run(inbox)

    assert [url for url, _kwargs in attempts] == ["ws://127.0.0.1:3001"] * 3
    assert attempts[1][1]["additional_headers"] == {"Authorization": "Bearer secret"}
    assert sleeps == [1.0, 1.0]
    msg = inbox.get_nowait()
    assert msg.sender_id == "123"
    assert msg.text == "ping"
    assert all(sock.entered and sock.exited for sock in sockets)


# ---- inbound dedup (audit #30; hermes gateway/platforms/helpers.py) -----------------


def test_dedup_ttl_window_with_fake_clock():
    now = [1000.0]
    d = MessageDeduplicator(ttl_seconds=300, clock=lambda: now[0])
    assert d.is_duplicate("qq:m1") is False   # first sight: recorded
    assert d.is_duplicate("qq:m1") is True    # redelivery inside the TTL
    now[0] += 299
    assert d.is_duplicate("qq:m1") is True    # still inside
    now[0] += 2
    assert d.is_duplicate("qq:m1") is False   # expired: treated as new
    assert d.is_duplicate("") is False
    assert d.is_duplicate("") is False           # empty ids are never deduped


def test_dedup_max_size_evicts_oldest_when_all_fresh():
    now = [0.0]
    d = MessageDeduplicator(max_size=3, ttl_seconds=300, clock=lambda: now[0])
    for i, key in enumerate(["a", "b", "c", "d"]):
        now[0] = float(i)
        assert d.is_duplicate(key) is False
    assert d.is_duplicate("a") is False  # evicted (oldest) to honor the bound
    assert d.is_duplicate("d") is True   # newest survived


def test_gateway_drops_redelivered_message_one_llm_turn():
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    msg = InboundMessage("u1", "Alice", "hi", message_id="MSG-42")
    gateway.enqueue(adapter, msg)
    gateway.enqueue(adapter, msg)  # the platform redelivery
    assert gateway.tick(timeout=0)
    gateway.tick(timeout=0)

    assert handle.user_calls == ["hi"]   # exactly one turn ran
    assert adapter.sent == ["reply"]     # and one reply went out


def test_gateway_without_message_id_never_dedups():
    # No platform id = nothing safe to key on: identical texts are two messages.
    handle = FakeHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    gateway.tick(timeout=0)
    gateway.tick(timeout=0)

    assert handle.user_calls == ["hi", "hi"]


def test_gateway_dedup_is_keyed_per_platform():
    handle = FakeHandle()
    a1, a2 = FakeAdapter("qq"), FakeAdapter("telegram")
    gateway = MessagingGateway(handle=handle, adapters=[a1, a2], allowed_senders=["u1"], patience=999)

    gateway.enqueue(a1, InboundMessage("u1", "Alice", "hi", message_id="7"))
    gateway.enqueue(a2, InboundMessage("u1", "Alice", "hi", message_id="7"))
    gateway.tick(timeout=0)
    gateway.tick(timeout=0)

    assert handle.user_calls == ["hi", "hi"]  # same id on two platforms = two messages


def test_qq_event_carries_message_id_for_dedup():
    from lunamoth.messaging.qq import parse_onebot_event

    raw = json.dumps(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123456,
            "message_id": 778899,
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
    )
    msg = parse_onebot_event(raw)
    assert msg is not None and msg.message_id == "778899"


# ---- QQ send while disconnected (audit #32) ------------------------------------------


def test_qq_send_while_disconnected_is_delivery_deferred():
    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.qq import QQAdapter

    adapter = QQAdapter({"url": "ws://127.0.0.1:3001", "peer_id": "999"})
    assert adapter._socket is None  # the reconnect loop owns the socket; it is down
    with pytest.raises(DeliveryDeferred):
        adapter.send("speak while the link is down")


def test_qq_disconnected_send_does_not_crash_the_gateway(fast_send_retry, caplog):
    import logging

    from lunamoth.messaging.qq import QQAdapter

    handle = FakeHandle()
    adapter = QQAdapter({"url": "ws://127.0.0.1:3001", "peer_id": "999"})
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi", message_id="q1"))
    with caplog.at_level(logging.ERROR, logger="lunamoth.messaging.gateway"):
        assert gateway.tick(timeout=0)  # the turn runs; only delivery is deferred

    assert handle.user_calls == ["hi"]
    assert any("could not deliver" in m for m in caplog.messages)  # logged, not raised


# ---- anti-loop silence-narration filter (audit #33; hermes gateway/delivery.py) ------


def test_is_silence_narration_flags_only_whole_string_tokens():
    from lunamoth.messaging.filters import is_silence_narration

    # Silence tokens — dropped before delivery.
    for token in [
        "*(silent)*", "_silent_", "`silence`", "(silent)", "silent", "Silent.",
        ".", "…", "...", "🔇", "  🔇  ", "(no response)", "no reply",
        "nothing to say", "*~silence~*", "", "   ", None,
    ]:
        assert is_silence_narration(token) is True, token

    # Substantive prose that merely mentions silence — never flagged.
    for prose in [
        "The deployment ran silently.",
        "Silence is golden — here is the plan...",
        "I have nothing to say about that, but here's the report.",
        "No response from the server, retrying now.",
        "Hello there!",
        ".env file updated",  # leading dot but real content
    ]:
        assert is_silence_narration(prose) is False, prose


class _SilenceSayHandle(FakeHandle):
    """A handle whose say-channel output is only a silence token."""

    def __init__(self, token="*(silent)*"):
        super().__init__()
        self._token = token

    def stream_user(self, text):
        self.user_calls.append(text)
        yield TextDelta(self._token, SAY)

    def stream_idle(self):
        self.idle_calls += 1
        yield TextDelta(self._token, SAY)


def test_gateway_drops_silence_narration_before_delivery(caplog, monkeypatch):
    import logging

    # obs.setup_logging (run by sibling tests) cuts propagation on "lunamoth";
    # restore it so caplog can see gateway INFO records regardless of order.
    monkeypatch.setattr(logging.getLogger("lunamoth"), "propagate", True)

    handle = _SilenceSayHandle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    with caplog.at_level(logging.INFO, logger="lunamoth.messaging.gateway"):
        assert gateway.tick(timeout=0)

    assert handle.user_calls == ["hi"]   # the turn still ran
    assert adapter.sent == []            # ...but the silence token never went out
    assert any("silence-narration" in m for m in caplog.messages)


def test_gateway_drops_idle_silence_narration():
    handle = _SilenceSayHandle(token="🔇")
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=0)

    assert gateway.tick(timeout=0)
    assert handle.idle_calls == 1
    assert adapter.sent == []   # an idle 🔇 is dropped, not mirrored into the channel


def test_gateway_delivers_real_say_after_silence_filter():
    # The filter must not swallow substantive replies that contain the word.
    class _Handle(FakeHandle):
        def stream_user(self, text):
            self.user_calls.append(text)
            yield TextDelta("Silence is golden — here is the plan.", SAY)

    handle = _Handle()
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=999)

    gateway.enqueue(adapter, InboundMessage("u1", "Alice", "hi"))
    assert gateway.tick(timeout=0)
    assert adapter.sent == ["Silence is golden — here is the plan."]


# ---- Telegram long-poll adapter (roadmap C2: Telegram after qq.py) -------------------


def _telegram_http_error(code, body):
    import io
    from urllib.error import HTTPError

    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return HTTPError("https://api.telegram.org/botSECRET/method", code, "error", {}, io.BytesIO(raw))


class FakeTelegramTransport:
    """Each entry is a 200 payload dict, or an exception instance to raise."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req, timeout=0):
        self.requests.append((req, timeout))
        if not self.payloads:
            raise AssertionError("unexpected HTTP request")
        item = self.payloads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeHTTPResponse(item)

    def body(self, index):
        return json.loads(self.requests[index][0].data.decode("utf-8"))


def _telegram_update(update_id, text="hello", chat_id=42, chat_type="private", **message_extra):
    message = {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id, "first_name": "Alice", "is_bot": False},
    }
    if text is not None:
        message["text"] = text
    message.update(message_extra)
    return {"update_id": update_id, "message": message}


def test_telegram_offset_persists_and_restart_never_replays(tmp_path):
    from lunamoth.messaging.telegram import TelegramAdapter

    state_path = tmp_path / "telegram_state.json"
    transport = FakeTelegramTransport([
        {"ok": True, "result": [_telegram_update(100), _telegram_update(101, text="again")]},
    ])
    adapter = TelegramAdapter({"bot_token": "tok"}, opener=transport, state_path=state_path)
    inbox = queue.Queue()

    assert adapter.poll_once(inbox) == 2
    assert inbox.get_nowait().text == "hello"
    assert inbox.get_nowait().text == "again"
    assert transport.body(0).get("offset") is None  # fresh start: no offset yet
    assert transport.body(0)["timeout"] == 25       # server-side long poll
    assert transport.requests[0][1] > 25            # socket timeout outlives it

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["offset"] == 102  # last update_id + 1: confirmed, never replayed
    assert state["last_chat_id"] == "42"
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"

    # A NEW adapter (the restart) resumes from the persisted offset.
    transport2 = FakeTelegramTransport([{"ok": True, "result": []}])
    restarted = TelegramAdapter({"bot_token": "tok"}, opener=transport2, state_path=state_path)
    assert restarted.poll_once(queue.Queue()) == 0
    assert transport2.body(0)["offset"] == 102


def test_telegram_default_state_path_honors_config_dir(monkeypatch, tmp_path):
    from lunamoth.messaging.telegram import default_state_path

    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path))
    assert default_state_path() == tmp_path.resolve() / "telegram_state.json"


def test_telegram_update_carries_update_id_for_dedup():
    from lunamoth.messaging.telegram import parse_update

    msg = parse_update(_telegram_update(778899, chat_id=123456))
    assert msg is not None
    assert msg.sender_id == "123456"   # chat id = sender id in private chats
    assert msg.sender_name == "Alice"
    assert msg.message_id == "778899"  # str(update_id) keys the gateway dedup


def test_telegram_ignores_groups_edits_channels_and_media():
    from lunamoth.messaging.telegram import parse_update

    assert parse_update(_telegram_update(1, chat_type="group")) is None
    assert parse_update(_telegram_update(2, chat_type="supergroup")) is None
    assert parse_update({"update_id": 3, "edited_message": {"text": "edited"}}) is None
    assert parse_update({"update_id": 4, "channel_post": {"text": "post"}}) is None
    assert parse_update(_telegram_update(5, text=None, photo=[{"file_id": "x"}])) is None
    bot_update = _telegram_update(6)
    bot_update["message"]["from"]["is_bot"] = True  # reply-loop guard
    assert parse_update(bot_update) is None


def test_telegram_declares_4096_split_for_the_gateway_splitter(tmp_path):
    from lunamoth.messaging.telegram import TELEGRAM_TEXT_MAX, TelegramAdapter

    adapter = TelegramAdapter({"bot_token": "tok"}, opener=FakeTelegramTransport([]),
                              state_path=tmp_path / "telegram_state.json")
    assert adapter.max_message_length == TELEGRAM_TEXT_MAX == 4096


def test_telegram_429_send_is_delivery_deferred_and_honors_retry_after(tmp_path):
    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.telegram import TelegramAdapter

    now = [1000.0]
    transport = FakeTelegramTransport([
        _telegram_http_error(429, {"ok": False, "error_code": 429,
                                   "description": "Too Many Requests: retry after 7",
                                   "parameters": {"retry_after": 7}}),
    ])
    adapter = TelegramAdapter(
        {"bot_token": "tok"}, opener=transport,
        state_path=tmp_path / "telegram_state.json", monotonic=lambda: now[0],
    )
    adapter.set_reply_target(InboundMessage("42", "Alice", "hi"))

    with pytest.raises(DeliveryDeferred, match="retry after 7s"):
        adapter.send("first")
    # retry_after is honored WITHOUT sleeping: the flood window defers
    # preemptively (no HTTP request) until it has passed.
    with pytest.raises(DeliveryDeferred, match="flood control active"):
        adapter.send("second")
    assert len(transport.requests) == 1
    now[0] += 8.0
    transport.payloads.append({"ok": True, "result": {"message_id": 1}})
    adapter.send("third")
    assert transport.body(1) == {"chat_id": 42, "text": "third"}


def test_telegram_bad_token_is_a_clear_startup_error_not_a_retry_loop(tmp_path):
    from lunamoth.messaging.telegram import TelegramAdapter

    transport = FakeTelegramTransport([
        _telegram_http_error(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
    ])
    adapter = TelegramAdapter({"bot_token": "bad"}, opener=transport,
                              state_path=tmp_path / "telegram_state.json")

    with pytest.raises(RuntimeError, match="401 Unauthorized.*bot_token") as excinfo:
        adapter.run(queue.Queue())
    assert len(transport.requests) == 1   # exactly one getMe, no silent retrying
    assert "bad" not in str(excinfo.value)  # the token never leaks into the error


def test_telegram_send_network_failure_is_delivery_deferred(tmp_path):
    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.telegram import TelegramAdapter

    transport = FakeTelegramTransport([ConnectionResetError("socket dropped")])
    adapter = TelegramAdapter({"bot_token": "tok"}, opener=transport,
                              state_path=tmp_path / "telegram_state.json")
    adapter.set_reply_target(InboundMessage("42", "Alice", "hi"))

    with pytest.raises(DeliveryDeferred, match="network failure"):
        adapter.send("hello")


def test_telegram_unattended_speak_before_first_contact_is_deferred(tmp_path):
    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.telegram import TelegramAdapter

    adapter = TelegramAdapter({"bot_token": "tok"}, opener=FakeTelegramTransport([]),
                              state_path=tmp_path / "telegram_state.json")
    with pytest.raises(DeliveryDeferred, match="cannot message a user first"):
        adapter.send("speak with nobody on record")


def test_telegram_allowed_senders_filter_through_the_gateway(tmp_path):
    from lunamoth.messaging.telegram import TelegramAdapter

    handle = FakeHandle()
    transport = FakeTelegramTransport([
        {"ok": True, "result": {"message_id": 1}},  # the one refusal send
        {"ok": True, "result": {"message_id": 2}},  # the allowed reply
    ])
    adapter = TelegramAdapter({"bot_token": "tok"}, opener=transport,
                              state_path=tmp_path / "telegram_state.json")
    adapter.run = lambda inbox: None  # the polling thread is not under test here
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["42"], patience=999)

    gateway.enqueue(adapter, InboundMessage("666", "Mallory", "hi", message_id="1"))
    gateway.enqueue(adapter, InboundMessage("42", "Alice", "hi", message_id="2"))
    assert gateway.tick(timeout=0)
    assert gateway.tick(timeout=0)

    assert handle.user_calls == ["hi"]  # only the allowed sender reached the chara
    refusal = transport.body(0)
    assert refusal["chat_id"] == 666 and "configured contacts" in refusal["text"]
    assert transport.body(1) == {"chat_id": 42, "text": "reply"}


def test_gateway_config_error_exits_fatal_not_retried(tmp_path, monkeypatch):
    """A malformed messaging config is fatal (EX_CONFIG 78), so the supervisor
    marks the gateway `fatal` and never auto-restarts it (audit #27/#13)."""
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    import argparse
    import json as _json

    from lunamoth.front import cli
    from lunamoth.server.supervisor import GATEWAY_FATAL_EXIT
    from lunamoth.session import sessions as S

    meta = S.create_session("gw", isolation="admin")
    (meta.root / "config.json").write_text(
        _json.dumps({"provider": "mock", "model": "m", "character_path": ""}), encoding="utf-8")
    ns = argparse.Namespace(name="gw", patience=600, debug=False)

    # unknown adapter -> ValueError -> fatal exit
    (meta.root / "messaging.json").write_text('{"adapters": {"bogus": {}}}', encoding="utf-8")
    assert cli.cmd_gateway(ns) == GATEWAY_FATAL_EXIT

    # missing config file -> fatal exit too (retrying can't create it)
    (meta.root / "messaging.json").unlink()
    assert cli.cmd_gateway(ns) == GATEWAY_FATAL_EXIT
