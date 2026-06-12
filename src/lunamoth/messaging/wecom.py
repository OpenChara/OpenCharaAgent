from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from xml.etree import ElementTree as ET

from .base import Adapter, InboundMessage
from .text import split_text
from .wecom_crypto import WeComCryptoError, decrypt_message, verify_url

_log = logging.getLogger("lunamoth.messaging.wecom")

API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_TEXT_MAX = 2048


@dataclass(frozen=True)
class WeComMessage:
    sender_id: str
    sender_name: str
    text: str
    agent_id: str
    raw: dict[str, str]


def _child_text(root: ET.Element, name: str) -> str:
    node = root.find(name)
    return (node.text or "").strip() if node is not None else ""


def parse_message_xml(xml: str) -> WeComMessage | None:
    """Parse decrypted WeCom callback XML into our text-only inbound shape."""

    root = ET.fromstring(xml)
    msg_type = _child_text(root, "MsgType").lower()
    event = _child_text(root, "Event").lower()
    sender = _child_text(root, "FromUserName")
    agent = _child_text(root, "AgentID")
    if msg_type == "text":
        text = _child_text(root, "Content")
    elif msg_type == "event" and event:
        text = f"[event:{event}]"
    else:
        return None
    return WeComMessage(
        sender_id=sender,
        sender_name=sender,
        text=text,
        agent_id=agent,
        raw={child.tag: child.text or "" for child in root},
    )


class WeComAPI:
    def __init__(
        self,
        *,
        corp_id: str,
        secret: str,
        agent_id: str,
        api_base: str = API_BASE,
        opener=None,
    ) -> None:
        self.corp_id = corp_id
        self.secret = secret
        self.agent_id = str(agent_id)
        self.api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._access_token = ""
        self._token_expires_at = 0.0

    def _request_json(self, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json"},
        )
        with self._opener(req, timeout=20) as resp:
            out = json.loads(resp.read().decode("utf-8", errors="replace"))
        if int(out.get("errcode", 0) or 0) != 0:
            raise RuntimeError(f"WeCom API error {out.get('errcode')}: {out.get('errmsg')}")
        return out

    def access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        qs = urllib.parse.urlencode({"corpid": self.corp_id, "corpsecret": self.secret})
        data = self._request_json(f"{self.api_base}/gettoken?{qs}")
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("WeCom gettoken returned no access_token")
        self._access_token = token
        self._token_expires_at = now + int(data.get("expires_in", 7200) or 7200)
        return token

    def send_text(self, to_user: str, text: str) -> None:
        qs = urllib.parse.urlencode({"access_token": self.access_token()})
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": int(self.agent_id) if str(self.agent_id).isdigit() else self.agent_id,
            "text": {"content": text},
            "safe": 0,
            "enable_id_trans": 0,
            "enable_duplicate_check": 0,
        }
        self._request_json(f"{self.api_base}/message/send?{qs}", payload)


class WeComAdapter(Adapter):
    """Enterprise WeChat / WeCom self-built app adapter."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.corp_id = str(self.config.get("corp_id") or self.config.get("corpid") or "").strip()
        self.secret = str(self.config.get("secret") or "").strip()
        self.agent_id = str(self.config.get("agent_id") or self.config.get("agentid") or "").strip()
        self.token = str(self.config.get("token") or "").strip()
        self.encoding_aes_key = str(self.config.get("encoding_aes_key") or "").strip()
        self.host = str(self.config.get("host") or self.config.get("callback_server_host") or "0.0.0.0")
        self.port = int(self.config.get("port") or 8128)
        self.path = str(self.config.get("path") or "/callback/command")
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        self.to_user = str(self.config.get("to_user") or "").strip()
        self._last_sender = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._api = WeComAPI(
            corp_id=self.corp_id,
            secret=self.secret,
            agent_id=self.agent_id,
            api_base=str(self.config.get("api_base") or API_BASE),
        )

    @property
    def name(self) -> str:
        return "wecom"

    def _validate(self) -> None:
        missing = [
            name
            for name, value in {
                "corp_id": self.corp_id,
                "secret": self.secret,
                "agent_id": self.agent_id,
                "token": self.token,
                "encoding_aes_key": self.encoding_aes_key,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"WeCom adapter missing required config: {', '.join(missing)}")

    def run(self, inbox: "queue.Queue[InboundMessage]") -> None:
        self._validate()
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
                _log.info("wecom callback: " + fmt, *args)

            def _query(self) -> dict[str, list[str]]:
                parsed = urllib.parse.urlparse(self.path)
                return urllib.parse.parse_qs(parsed.query)

            def _param(self, query: dict[str, list[str]], name: str) -> str:
                return query.get(name, [""])[0]

            def _write(self, status: int, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - stdlib hook
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != adapter.path:
                    self._write(404, "not found")
                    return
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    echo = verify_url(
                        self._param(query, "msg_signature"),
                        self._param(query, "timestamp"),
                        self._param(query, "nonce"),
                        self._param(query, "echostr"),
                        token=adapter.token,
                        encoding_aes_key=adapter.encoding_aes_key,
                        receive_id=adapter.corp_id,
                    )
                except (WeComCryptoError, RuntimeError) as e:
                    _log.warning("WeCom URL verification failed: %s", e)
                    self._write(403, "invalid signature")
                    return
                self._write(200, echo)

            def do_POST(self) -> None:  # noqa: N802 - stdlib hook
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != adapter.path:
                    self._write(404, "not found")
                    return
                query = urllib.parse.parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length)
                try:
                    xml = decrypt_message(
                        body,
                        self._param(query, "msg_signature"),
                        self._param(query, "timestamp"),
                        self._param(query, "nonce"),
                        token=adapter.token,
                        encoding_aes_key=adapter.encoding_aes_key,
                        receive_id=adapter.corp_id,
                    )
                    msg = parse_message_xml(xml)
                except (ET.ParseError, WeComCryptoError, RuntimeError) as e:
                    _log.warning("WeCom callback decrypt/parse failed: %s", e)
                    self._write(403, "invalid message")
                    return
                if msg and msg.text.strip():
                    adapter._last_sender = msg.sender_id
                    inbox.put(
                        InboundMessage(
                            sender_id=msg.sender_id,
                            sender_name=msg.sender_name,
                            text=msg.text,
                            reply=msg.raw,
                        )
                    )
                self._write(200, "success")

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        _log.info("WeCom adapter listening on http://%s:%s%s", self.host, self.port, self.path)
        self._httpd.serve_forever(poll_interval=0.25)

    def send(self, text: str) -> None:
        to_user = self.to_user or self._last_sender
        if not to_user:
            raise RuntimeError("WeCom send needs config.to_user or a prior inbound sender")
        for chunk in split_text(text, WECOM_TEXT_MAX):
            self._api.send_text(to_user, chunk)
            time.sleep(0.2)

    def close(self) -> None:
        httpd = self._httpd
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
