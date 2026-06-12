from dataclasses import dataclass
import json
import queue
import time
from xml.etree import ElementTree as ET

import pytest

from lunamoth.messaging.base import Adapter, InboundMessage
from lunamoth.messaging.gateway import MessagingGateway
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
