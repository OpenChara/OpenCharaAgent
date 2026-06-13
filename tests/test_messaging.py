from dataclasses import dataclass
import json
import queue
import time
from xml.etree import ElementTree as ET

import pytest

from lunamoth.messaging.base import Adapter, InboundMessage
from lunamoth.messaging.gateway import MessageDeduplicator, MessagingGateway
from lunamoth.messaging.text import split_text
from lunamoth.messaging.wecom import parse_message_xml
from lunamoth.messaging.wecom_crypto import (
    aes_decrypt,
    decrypt_message,
    encrypt_message,
    sha1_signature,
    verify_url,
)
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
        self.present_calls = []
        self.rest_until = 0.0
        self.quiet = 0
        self.attached = False

    def attach(self, present=False):
        self.attached = True
        self.set_present(present)

    def set_present(self, present: bool):
        self.present_calls.append(bool(present))

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
            user_present=self.present_calls[-1] if self.present_calls else False,
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
    assert True in handle.present_calls


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


def test_wecom_official_sample_signature_and_url_decrypt():
    pytest.importorskip("cryptography")
    # Tencent's WXBizMsgCrypt samples: the POST example documents the first
    # signature; the URL-verify sample decrypts to the echoed nonce-like string.
    key = "6qkdMrq68nTKduznJYO1A37W2oEgpkMUvkttRToqhUt"
    token = "QDG6eK"
    corp_id = "ww1436e0e65a779aee"
    echostr = (
        "fsi1xnbH4yQh0+PJxcOdhhK6TDXkjMyhEPA7xB2TGz6b+g7xyAbEkRxN/"
        "3cNXW9qdqjnoVzEtpbhnFyq6SVHyA=="
    )

    assert sha1_signature(token, "1409659589", "263014780", "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ==") == "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3"
    assert verify_url(
        "59dce1a653fc31a6f0472567199561e9336a8d0b",
        "1476416373",
        "47744683",
        echostr,
        token="xxxxxxx",
        encoding_aes_key=key,
        receive_id=corp_id,
    ) == "1288432023552776189"
    decrypted_xml = aes_decrypt(
        "Kl7kjoSf6DMD1zh7rtrHjFaDapSCkaOnwu3bqLc5tAybhhMl9pFeK8NslNPVdMwmBQTNoW4mY7AIjeLvEl3NyeTkAgGzBhzTtRLNshw2AEew+kkYcD+Fq72Kt00fT0WnN87hGrW8SqGc+NcT3mu87Ha3dz1pSDi6GaUA6A0sqfde0VJPQbZ9U+3JWcoD4Z5jaU0y9GSh010wsHF8KZD24YhmZH4ch4Ka7ilEbjbfvhKkNL65HHL0J6EYJIZUC2pFrdkJ7MhmEbU2qARR4iQHE7wy24qy0cRX3Mfp6iELcDNfSsPGjUQVDGxQDCWjayJOpcwocugux082f49HKYg84EpHSGXAyh+/oxwaWbvL6aSDPOYuPDGOCI8jmnKiypE+",
        corp_id,
        key,
    )
    assert "<Content>你好</Content>" in decrypted_xml


def test_wecom_crypto_encrypt_decrypt_round_trip():
    pytest.importorskip("cryptography")
    key = "6qkdMrq68nTKduznJYO1A37W2oEgpkMUvkttRToqhUt"
    token = "QDG6eK"
    corp_id = "ww1436e0e65a779aee"
    plain = (
        "<xml><ToUserName>ww1436e0e65a779aee</ToUserName>"
        "<FromUserName>ChenJiaShun</FromUserName><MsgType>text</MsgType>"
        "<Content>你好</Content><AgentID>1000002</AgentID></xml>"
    )
    encrypted = encrypt_message(
        plain,
        "1597212914",
        "1476422779",
        token=token,
        encoding_aes_key=key,
        receive_id=corp_id,
        random16=b"abcdefghijklmnop",
    )
    root = ET.fromstring(encrypted)
    decrypted = decrypt_message(
        encrypted,
        root.findtext("MsgSignature") or "",
        root.findtext("TimeStamp") or "",
        root.findtext("Nonce") or "",
        token=token,
        encoding_aes_key=key,
        receive_id=corp_id,
    )
    assert decrypted == plain


def test_wecom_message_xml_parse():
    msg = parse_message_xml(
        "<xml><ToUserName><![CDATA[ww1436e0e65a779aee]]></ToUserName>"
        "<FromUserName><![CDATA[ChenJiaShun]]></FromUserName>"
        "<CreateTime>1476422779</CreateTime><MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[你好]]></Content><MsgId>1456453720</MsgId>"
        "<AgentID>1000002</AgentID></xml>"
    )
    assert msg is not None
    assert msg.sender_id == "ChenJiaShun"
    assert msg.agent_id == "1000002"
    assert msg.text == "你好"


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
    assert d.is_duplicate("wecom:m1") is False   # first sight: recorded
    assert d.is_duplicate("wecom:m1") is True    # redelivery inside the TTL
    now[0] += 299
    assert d.is_duplicate("wecom:m1") is True    # still inside
    now[0] += 2
    assert d.is_duplicate("wecom:m1") is False   # expired: treated as new
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
    a1, a2 = FakeAdapter("qq"), FakeAdapter("wecom")
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


def test_wecom_callback_carries_msgid_for_dedup(monkeypatch):
    # The crypto layer is covered by its own (cryptography-gated) tests; here
    # the decrypt seam is stubbed so the MsgId -> InboundMessage.message_id
    # wiring in the callback handler is exercised without the optional extra.
    import socket
    import threading
    import urllib.request

    import lunamoth.messaging.wecom as wecom_mod
    from lunamoth.messaging.wecom import WeComAdapter

    monkeypatch.setattr(
        wecom_mod, "decrypt_message",
        lambda body, sig, ts, nonce, **kw: body.decode("utf-8"),
    )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    adapter = WeComAdapter(
        {
            "corp_id": "corp", "secret": "s", "agent_id": "1000002",
            "token": "t", "encoding_aes_key": "k",
            "host": "127.0.0.1", "port": port,
        }
    )
    inbox = queue.Queue()
    thread = threading.Thread(target=adapter.run, args=(inbox,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # wait for the server to bind
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)

        plain = (
            "<xml><ToUserName><![CDATA[corp]]></ToUserName>"
            "<FromUserName><![CDATA[u1]]></FromUserName>"
            "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[hello]]></Content>"
            "<MsgId>1456453720</MsgId><AgentID>1000002</AgentID></xml>"
        )
        # a fresh timestamp: the callback handler rejects stale ones (±5 min replay guard)
        url = f"http://127.0.0.1:{port}/callback/command?msg_signature=x&timestamp={int(time.time())}&nonce=2"
        req = urllib.request.Request(url, data=plain.encode("utf-8"), method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

        msg = inbox.get(timeout=5)
        assert msg.text == "hello"
        assert msg.message_id == "1456453720"  # MsgId wired through for the gateway dedup
    finally:
        adapter.close()


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

    meta = S.create_session("gw", isolation="dir")
    (meta.root / "config.json").write_text(
        _json.dumps({"provider": "mock", "model": "m", "character_path": ""}), encoding="utf-8")
    ns = argparse.Namespace(name="gw", patience=600, debug=False)

    # unknown adapter -> ValueError -> fatal exit
    (meta.root / "messaging.json").write_text('{"adapters": {"bogus": {}}}', encoding="utf-8")
    assert cli.cmd_gateway(ns) == GATEWAY_FATAL_EXIT

    # missing config file -> fatal exit too (retrying can't create it)
    (meta.root / "messaging.json").unlink()
    assert cli.cmd_gateway(ns) == GATEWAY_FATAL_EXIT


# ---- WeChatPadPro adapter (weixinpad: iPad protocol via user-run docker) -------------


class FakeWeixinPadTransport:
    """Routes WeChatPadPro REST calls by path to canned payloads.

    Each entry maps a path substring to a payload dict OR an exception instance
    to raise (HTTPError/URLError). A path may map to a list to pop sequentially
    (e.g. CheckLoginStatus: pending then confirmed).
    """

    def __init__(self, routes):
        self.routes = dict(routes)
        self.requests = []

    def __call__(self, req, timeout=0):
        self.requests.append((req, timeout))
        for fragment, payload in self.routes.items():
            if fragment in req.full_url:
                item = payload.pop(0) if isinstance(payload, list) else payload
                if isinstance(item, BaseException):
                    raise item
                return FakeHTTPResponse(item)
        raise AssertionError(f"unexpected WeChatPadPro request: {req.full_url}")

    def body(self, index):
        return json.loads(self.requests[index][0].data.decode("utf-8"))


class _CaptureOut:
    def __init__(self):
        self.chunks = []

    def write(self, s):
        self.chunks.append(s)

    def flush(self):
        return None

    def __str__(self):
        return "".join(self.chunks)


def _wxpad_login_routes(extra=None):
    routes = {
        "/admin/GenAuthKey1": {"Code": 200, "Data": ["auth-xyz"]},
        "/login/GetLoginQrCodeNew": {"Code": 200, "Data": {"QrCodeUrl": "https://qr.example/abc"}},
        "/login/CheckLoginStatus": {"Code": 200, "Data": {"loginState": 1}},
        "/login/GetProfile": {
            "Code": 200,
            "Data": {"userInfo": {"userName": {"str": "wxid_me"}}},
        },
    }
    if extra:
        routes.update(extra)
    return routes


def test_weixinpad_login_persists_auth_key_and_wxid_0600(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    transport = FakeWeixinPadTransport(_wxpad_login_routes())
    out = _CaptureOut()
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "port": 38849, "admin_key": "ADMIN"},
        opener=transport,
        state_path=tmp_path / "weixinpad_state.json",
        output=out,
        sleep=lambda _s: None,
    )
    adapter._ensure_login()

    state = json.loads((tmp_path / "weixinpad_state.json").read_text(encoding="utf-8"))
    assert state["auth_key"] == "auth-xyz"
    assert state["wxid"] == "wxid_me"
    assert oct((tmp_path / "weixinpad_state.json").stat().st_mode & 0o777) == "0o600"
    # the admin_key authenticates GenAuthKey1; the derived auth_key everything else
    assert "key=ADMIN" in transport.requests[0][0].full_url
    assert "/admin/GenAuthKey1" in transport.requests[0][0].full_url
    assert "key=auth-xyz" in transport.requests[1][0].full_url
    # QR rendered + fallback URL offered
    assert "qrserver.com" in str(out)


def test_weixinpad_reuses_saved_auth_key_no_login(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved", "wxid": "wxid_me"}), encoding="utf-8")
    transport = FakeWeixinPadTransport({})
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"}, opener=transport, state_path=state_path
    )
    adapter._ensure_login()
    assert adapter.auth_key == "saved"
    assert transport.requests == []  # nothing re-requested


def test_weixinpad_login_waits_then_confirms(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    routes = _wxpad_login_routes(
        {"/login/CheckLoginStatus": [{"Code": 200, "Data": {"loginState": 0}},
                                     {"Code": 200, "Data": {"loginState": 1}}]}
    )
    transport = FakeWeixinPadTransport(routes)
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"},
        opener=transport,
        state_path=tmp_path / "weixinpad_state.json",
        output=_CaptureOut(),
        sleep=lambda _s: None,
    )
    adapter._login()
    assert adapter.auth_key == "auth-xyz"
    assert adapter.wxid == "wxid_me"


def test_weixinpad_bad_admin_key_is_clear_startup_error(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    transport = FakeWeixinPadTransport(
        {"/admin/GenAuthKey1": {"Code": 401, "Text": "invalid admin key"}}
    )
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "BAD"},
        opener=transport,
        state_path=tmp_path / "weixinpad_state.json",
        output=_CaptureOut(),
    )
    with pytest.raises(RuntimeError, match="admin_key"):
        adapter._login()


def test_weixinpad_inbound_text_frame_to_message_with_id(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"},
        state_path=tmp_path / "weixinpad_state.json",
    )
    inbox = queue.Queue()
    frame = json.dumps(
        {
            "MsgType": 1,
            "FromUserName": {"string": "wxid_friend"},
            "Content": {"str": "hello there"},
            "NewMsgId": 998877,
        }
    )
    assert adapter.handle_frame(frame, inbox) is True
    msg = inbox.get_nowait()
    assert msg.sender_id == "wxid_friend"
    assert msg.text == "hello there"
    assert msg.message_id == "998877"  # NewMsgId wired through for gateway dedup
    assert adapter._last_sender == "wxid_friend"  # becomes the default speak target


def test_weixinpad_non_text_frame_ignored(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"},
        state_path=tmp_path / "weixinpad_state.json",
    )
    inbox = queue.Queue()
    # image message (MsgType 3) and a frame missing a sender are both ignored
    assert adapter.handle_frame(
        json.dumps({"MsgType": 3, "FromUserName": {"string": "wxid_friend"}}), inbox
    ) is False
    assert adapter.handle_frame(
        json.dumps({"MsgType": 1, "Content": {"str": "no sender"}}), inbox
    ) is False
    assert inbox.empty()


def test_weixinpad_send_success(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved", "wxid": "wxid_me"}), encoding="utf-8")
    transport = FakeWeixinPadTransport({"/message/SendTextMessage": {"Code": 200}})
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"}, opener=transport, state_path=state_path
    )
    adapter.set_reply_target(InboundMessage("wxid_friend", "Friend", "hi"))
    adapter.send("reply text")

    body = transport.body(0)
    assert body["MsgItem"][0]["ToUserName"] == "wxid_friend"
    assert body["MsgItem"][0]["TextContent"] == "reply text"
    assert "key=saved" in transport.requests[0][0].full_url


def test_weixinpad_send_failure_is_delivery_deferred(tmp_path):
    import io
    from urllib.error import HTTPError

    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved"}), encoding="utf-8")
    err = HTTPError("http://x/message/SendTextMessage", 500, "boom", {}, io.BytesIO(b""))
    transport = FakeWeixinPadTransport({"/message/SendTextMessage": err})
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"}, opener=transport, state_path=state_path
    )
    adapter.set_reply_target(InboundMessage("wxid_friend", "Friend", "hi"))
    with pytest.raises(DeliveryDeferred, match="dropped, not queued"):
        adapter.send("nope")


def test_weixinpad_send_429_is_delivery_deferred(tmp_path):
    import io
    from urllib.error import HTTPError

    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved"}), encoding="utf-8")
    err = HTTPError("http://x/message/SendTextMessage", 429, "rate", {}, io.BytesIO(b""))
    transport = FakeWeixinPadTransport({"/message/SendTextMessage": err})
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"}, opener=transport, state_path=state_path
    )
    adapter.set_reply_target(InboundMessage("wxid_friend", "Friend", "hi"))
    with pytest.raises(DeliveryDeferred, match="429"):
        adapter.send("too fast")


def test_weixinpad_send_without_destination_is_deferred(tmp_path):
    from lunamoth.messaging.base import DeliveryDeferred
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved"}), encoding="utf-8")
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"},
        opener=FakeWeixinPadTransport({}),
        state_path=state_path,
    )
    with pytest.raises(DeliveryDeferred, match="no inbound sender on record"):
        adapter.send("speak with nobody on record")


def test_weixinpad_default_state_path_honors_config_dir(monkeypatch, tmp_path):
    from lunamoth.messaging.weixinpad import default_state_path

    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path))
    assert default_state_path() == tmp_path.resolve() / "weixinpad_state.json"


def test_weixinpad_ws_reconnect_reads_frame_and_backoff(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved"}), encoding="utf-8")
    frames = [
        json.dumps(
            {
                "MsgType": 1,
                "FromUserName": {"string": "wxid_friend"},
                "Content": {"str": "ping"},
                "NewMsgId": 1,
            }
        )
    ]
    sleeps = []
    attempts = []
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"},
        opener=FakeWeixinPadTransport({}),
        state_path=state_path,
        sleep=lambda s: sleeps.append(s),
        recv_timeout=0,
    )

    def connect(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("first drop")
        return FakeQQSocket(frames=frames, error=ConnectionError("done"), on_error=adapter.close)

    adapter._connect_func = connect
    inbox = queue.Queue()
    adapter.run(inbox)

    assert sleeps == [1.0]  # one backoff after the first drop, reset after success
    assert all("/ws/GetSyncMsg?key=saved" in u for u in attempts)
    msg = inbox.get_nowait()
    assert msg.sender_id == "wxid_friend" and msg.text == "ping"


def test_weixinpad_allowed_senders_filter_through_gateway(tmp_path):
    from lunamoth.messaging.weixinpad import WeixinPadAdapter

    handle = FakeHandle()
    state_path = tmp_path / "weixinpad_state.json"
    state_path.write_text(json.dumps({"auth_key": "saved"}), encoding="utf-8")
    transport = FakeWeixinPadTransport({"/message/SendTextMessage": [{"Code": 200}, {"Code": 200}]})
    adapter = WeixinPadAdapter(
        {"host": "127.0.0.1", "admin_key": "ADMIN"}, opener=transport, state_path=state_path
    )
    adapter.run = lambda inbox: None  # the WS thread is not under test here
    gateway = MessagingGateway(
        handle=handle, adapters=[adapter], allowed_senders=["wxid_ok"], patience=999
    )

    gateway.enqueue(adapter, InboundMessage("wxid_bad", "Mallory", "hi", message_id="1"))
    gateway.enqueue(adapter, InboundMessage("wxid_ok", "Alice", "hi", message_id="2"))
    assert gateway.tick(timeout=0)
    assert gateway.tick(timeout=0)

    assert handle.user_calls == ["hi"]  # only the allowed sender reached the chara
    refusal = transport.body(0)
    assert refusal["MsgItem"][0]["ToUserName"] == "wxid_bad"
    assert "configured contacts" in refusal["MsgItem"][0]["TextContent"]
    assert transport.body(1)["MsgItem"][0]["TextContent"] == "reply"
