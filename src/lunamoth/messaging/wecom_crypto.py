from __future__ import annotations

import base64
import hashlib
import os
import struct
from xml.etree import ElementTree as ET


class WeComCryptoError(ValueError):
    """Invalid WeCom callback signature, key, padding, or receive id."""


def sha1_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    """WeCom callback signature: sha1(sorted(token, timestamp, nonce, encrypt))."""

    raw = "".join(sorted([token, timestamp, nonce, encrypted]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _aes_key(encoding_aes_key: str) -> bytes:
    try:
        key = base64.b64decode(encoding_aes_key + "=", validate=True)
    except Exception as e:  # noqa: BLE001 - surfaced as config error
        raise WeComCryptoError("invalid encoding_aes_key") from e
    if len(key) != 32:
        raise WeComCryptoError("invalid encoding_aes_key length")
    return key


def _pad(data: bytes) -> bytes:
    amount = 32 - (len(data) % 32)
    if amount == 0:
        amount = 32
    return data + bytes([amount]) * amount


def _unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("empty plaintext")
    amount = data[-1]
    if amount < 1 or amount > 32:
        raise WeComCryptoError("invalid padding")
    if data[-amount:] != bytes([amount]) * amount:
        raise WeComCryptoError("invalid padding")
    return data[:-amount]


def _cipher(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # pragma: no cover - exercised in deployments without the extra
        raise RuntimeError(
            "WeCom callback crypto requires the optional messaging extra: "
            "uv sync --extra messaging"
        ) from e

    return Cipher(algorithms.AES(key), modes.CBC(key[:16]))


def aes_encrypt(plain_xml: str, receive_id: str, encoding_aes_key: str, *, random16: bytes | None = None) -> str:
    """Encrypt plaintext XML into the base64 `<Encrypt>` body."""

    key = _aes_key(encoding_aes_key)
    random16 = random16 if random16 is not None else os.urandom(16)
    if len(random16) != 16:
        raise ValueError("random16 must be exactly 16 bytes")
    xml_bytes = plain_xml.encode("utf-8")
    payload = random16 + struct.pack("!I", len(xml_bytes)) + xml_bytes + receive_id.encode("utf-8")
    encryptor = _cipher(key).encryptor()
    encrypted = encryptor.update(_pad(payload)) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def aes_decrypt(encrypted: str, receive_id: str, encoding_aes_key: str) -> str:
    """Decrypt a base64 `<Encrypt>` body and validate the receive/corp id."""

    key = _aes_key(encoding_aes_key)
    try:
        data = base64.b64decode(encrypted, validate=True)
    except Exception as e:  # noqa: BLE001 - surfaced as callback error
        raise WeComCryptoError("invalid encrypted payload") from e
    decryptor = _cipher(key).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    plain = _unpad(padded)
    if len(plain) < 20:
        raise WeComCryptoError("illegal plaintext buffer")
    msg_len = struct.unpack("!I", plain[16:20])[0]
    xml_start = 20
    xml_end = xml_start + msg_len
    if xml_end > len(plain):
        raise WeComCryptoError("illegal plaintext length")
    xml = plain[xml_start:xml_end].decode("utf-8")
    actual_receive_id = plain[xml_end:].decode("utf-8")
    if actual_receive_id != receive_id:
        raise WeComCryptoError("receive id mismatch")
    return xml


def extract_encrypt(encrypted_xml: bytes | str) -> str:
    data = encrypted_xml.decode("utf-8") if isinstance(encrypted_xml, bytes) else encrypted_xml
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise WeComCryptoError("invalid encrypted xml") from e
    node = root.find("Encrypt")
    if node is None or not (node.text or "").strip():
        raise WeComCryptoError("missing Encrypt")
    return (node.text or "").strip()


def decrypt_message(
    encrypted_xml: bytes | str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    *,
    token: str,
    encoding_aes_key: str,
    receive_id: str,
) -> str:
    encrypted = extract_encrypt(encrypted_xml)
    expected = sha1_signature(token, timestamp, nonce, encrypted)
    if expected != msg_signature:
        raise WeComCryptoError("invalid message signature")
    return aes_decrypt(encrypted, receive_id, encoding_aes_key)


def verify_url(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
    *,
    token: str,
    encoding_aes_key: str,
    receive_id: str,
) -> str:
    expected = sha1_signature(token, timestamp, nonce, echostr)
    if expected != msg_signature:
        raise WeComCryptoError("invalid url signature")
    return aes_decrypt(echostr, receive_id, encoding_aes_key)


def encrypt_message(
    plain_xml: str,
    nonce: str,
    timestamp: str,
    *,
    token: str,
    encoding_aes_key: str,
    receive_id: str,
    random16: bytes | None = None,
) -> str:
    encrypted = aes_encrypt(plain_xml, receive_id, encoding_aes_key, random16=random16)
    sig = sha1_signature(token, timestamp, nonce, encrypted)
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )
