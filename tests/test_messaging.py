from dataclasses import dataclass
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
        self.tempo = 1.0
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
            tempo=self.tempo,
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


def test_gateway_idle_pause_respects_chara_tempo():
    handle = FakeHandle()
    handle.tempo = 2.0
    adapter = FakeAdapter()
    gateway = MessagingGateway(handle=handle, adapters=[adapter], allowed_senders=["u1"], patience=10)

    gateway._next_idle_at = 0.0
    assert gateway.tick(timeout=0)

    remaining = gateway._next_idle_at - time.monotonic()
    assert 4.0 <= remaining <= 5.0


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
