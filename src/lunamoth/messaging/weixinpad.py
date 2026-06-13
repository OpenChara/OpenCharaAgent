"""Personal WeChat over a user-run WeChatPadPro docker gateway (WeChat iPad protocol).

Unlike the iLink/ClawBot adapter in ``weixin.py`` — which is Tencent-official but
gated behind a grayscale rollout, so its QR "scans to nothing" for most accounts —
WeChatPadPro produces a REAL device-login QR that works on ANY WeChat account.
This is the "AstrBot one-QR" path. The trade-off is ban risk: WeChatPadPro speaks
the unofficial WeChat iPad protocol (see the README setup note). LunaMoth only
TALKS to the user-run container over HTTP/WS; it does not vendor or run it.

Pinned API surface
------------------
Target release: WeChatPadPro **v2.01** (tag ``v2.01``, build ``ios18.61-861``,
2025-08-22; repo github.com/WeChatPadPro/WeChatPadPro, default branch ``main``).
Endpoint paths/request bodies are from that release's swagger
(``static/swagger/swagger.json`` on the ``v2.01`` tag — the swagger documents NO
response schemas); the response-field keys and the WS frame shape are pinned from
the only code that actually parses WeChatPadPro responses, the on-wechat client
``5201213/wechatpadpro-on-wechat`` (``lib/wxpad/client.py``,
``channel/wxpad/wxpad_channel.py``, ``channel/wxpad/wxpad_message.py``). The WS
path + the docker-compose API port 38849 are additionally confirmed by a live
AstrBot deployment (AstrBotDevs/AstrBot issue #1909) and the ccino docker blog.

Auth model: EVERY endpoint authenticates with a ``?key=<...>`` query param.
For ``/admin/*`` the key is the **adminKey**; for ``/login/*``, ``/message/*``
and the WS it is the per-account **authKey** generated from the adminKey.

Endpoints targeted (all paths are module constants below so a post-release drift
is a one-line fix):
- ``POST /admin/GenAuthKey1?key=<adminKey>``  body ``{"Count":1,"Days":365}``
    -> ``{"Code":200,"Data":["<authKey>", ...]}`` (key = ``Data[0]``)
- ``POST /login/GetLoginQrCodeNew?key=<authKey>``  body ``{"Check":false,"Proxy":""}``
    -> ``Data.QrCodeUrl`` | ``qrUrl`` | ``QrUrl`` | ``url`` (a QR *URL* string)
- ``GET  /login/CheckLoginStatus?key=<authKey>``
    -> logged-in when ``Data.loginState == 1``
- ``GET  /login/GetProfile?key=<authKey>``  -> wxid at ``Data.userInfo.userName.str``
- ``POST /message/SendTextMessage?key=<authKey>``  body
    ``{"MsgItem":[{"ToUserName":"<wxid>","TextContent":"<text>","MsgType":1,
    "AtWxIDList":[],"ImageContent":""}]}``
- ``ws://host:port/ws/GetSyncMsg?key=<authKey>`` — one JSON message dict per frame;
    text when ``MsgType == 1``; sender at ``FromUserName.string|str``; text at
    ``Content.str|string``; server id ``NewMsgId`` (fallback ``MsgId``).

UNCONFIRMED until a real login (swagger omits response schemas) — each is a single
module constant so the fix is one line; flagged in ``.done``:
- the QR-url field name set (``_QR_URL_KEYS``) — only ``QrCodeUrl`` family seen,
- ``loginState == 1`` meaning "online" (``_LOGIN_OK_STATE``),
- ``GenAuthKey1`` returning the key at ``Data[0]``,
- the WS frame keys (``_WS_*`` constants), which we read case-insensitively.

Text only; media/voice/file are intentionally out of scope for v1 (same as the
iLink adapter). say-channel-only, supervised by the existing MessagingGateway.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .base import Adapter, DeliveryDeferred, InboundMessage

_log = logging.getLogger("lunamoth.messaging.weixinpad")

DEFAULT_PORT = 38849
DEFAULT_API_TIMEOUT_MS = 15_000
# WeChatPadPro device-login QR is valid for a few minutes; poll until then.
QRCODE_VALID_SECONDS = 4 * 60
QRCODE_REFRESHES = 2
AUTHKEY_DAYS = 365
WEIXINPAD_TEXT_MAX = 4000

# --- pinned endpoint paths (one-line fixes if a release drifts) ---------------
PATH_GEN_AUTH_KEY = "/admin/GenAuthKey1"
PATH_GET_LOGIN_QR = "/login/GetLoginQrCodeNew"
PATH_CHECK_LOGIN = "/login/CheckLoginStatus"
PATH_GET_PROFILE = "/login/GetProfile"
PATH_SEND_TEXT = "/message/SendTextMessage"
PATH_WS_SYNC = "/ws/GetSyncMsg"

# --- pinned (but swagger-undocumented) response field names -------------------
_QR_URL_KEYS = ("QrCodeUrl", "qrUrl", "QrUrl", "url")
_LOGIN_OK_STATE = 1  # Data.loginState == 1 means the account is online
_WS_TEXT_MSGTYPE = 1
# WS frame keys are read case-insensitively (both "string" and "str" appear in
# the wild); these are the canonical spellings we look for.
_WS_FROM_KEYS = ("FromUserName", "from_user_name")
_WS_CONTENT_KEYS = ("Content", "content")
_WS_TYPE_KEYS = ("MsgType", "msg_type")
_WS_ID_KEYS = ("NewMsgId", "new_msg_id", "MsgId", "msg_id")
_WS_NESTED_STR_KEYS = ("str", "string")


def _int_config(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, minimum)


def default_state_path() -> Path:
    root = os.getenv("LUNAMOTH_CONFIG_DIR")
    if root:
        return Path(root).expanduser().resolve() / "weixinpad_state.json"
    session = os.getenv("LUNAMOTH_SESSION", "")
    if session:
        return Path.home().expanduser() / ".lunamoth" / "sessions" / session / "weixinpad_state.json"
    return Path("weixinpad_state.json").resolve()


def qr_fallback_url(qrcode_value: str) -> str:
    qs = urllib.parse.urlencode({"size": "320x320", "data": qrcode_value})
    return f"https://api.qrserver.com/v1/create-qr-code/?{qs}"


def _code_ok(payload: dict[str, Any]) -> bool:
    try:
        return int(payload.get("Code") or 0) == 200
    except (TypeError, ValueError):
        return False


def _format_api_error(payload: dict[str, Any]) -> str:
    code = payload.get("Code")
    text = payload.get("Text") or payload.get("Message") or payload.get("Msg") or ""
    return f"Code={code}, Text={text}"


def _nested_str(value: Any) -> str:
    """WeChatPadPro nests strings as {"str": "..."} or {"string": "..."}."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in _WS_NESTED_STR_KEYS:
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _first(msg: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in msg:
            return msg[key]
    return None


def ws_message_to_inbound(msg: Any) -> InboundMessage | None:
    """Normalize one GetSyncMsg frame into an InboundMessage (text only).

    Returns None for non-text frames and frames without a sender so the caller
    can log-and-ignore. One message dict per WS frame (no AddMsgs/MsgList wrap).
    """

    if not isinstance(msg, dict):
        return None
    raw_type = _first(msg, _WS_TYPE_KEYS)
    try:
        msg_type = int(raw_type) if raw_type is not None else None
    except (TypeError, ValueError):
        msg_type = None
    if msg_type != _WS_TEXT_MSGTYPE:
        _log.debug("ignored non-text WeChatPadPro frame (MsgType=%s)", raw_type)
        return None
    sender_id = _nested_str(_first(msg, _WS_FROM_KEYS))
    if not sender_id:
        _log.debug("ignored WeChatPadPro frame without sender wxid")
        return None
    text = _nested_str(_first(msg, _WS_CONTENT_KEYS))
    if not text:
        _log.debug("ignored empty WeChatPadPro text frame")
        return None
    raw_id = _first(msg, _WS_ID_KEYS)
    message_id = str(raw_id).strip() if raw_id not in (None, "", 0) else ""
    return InboundMessage(
        sender_id=sender_id,
        sender_name=sender_id,
        text=text,
        reply=msg,
        message_id=message_id,
    )


class WeChatPadProAPI:
    """Small urllib client for the WeChatPadPro REST surface (stdlib only)."""

    def __init__(self, base_url: str, *, api_timeout_ms: int = DEFAULT_API_TIMEOUT_MS, opener=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_timeout_ms = api_timeout_ms
        self._opener = opener or urllib.request.urlopen

    def request_json(
        self,
        method: str,
        path: str,
        *,
        key: str,
        payload: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}?{urllib.parse.urlencode({'key': key})}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method.upper(),
            headers={"Content-Type": "application/json"},
        )
        timeout = (timeout_ms if timeout_ms is not None else self.api_timeout_ms) / 1000
        with self._opener(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return {}
        out = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(out, dict):
            raise RuntimeError(f"WeChatPadPro {path} returned non-object JSON")
        return out

    def gen_auth_key(self, admin_key: str) -> dict[str, Any]:
        return self.request_json(
            "POST", PATH_GEN_AUTH_KEY, key=admin_key,
            payload={"Count": 1, "Days": AUTHKEY_DAYS, "Remark": "lunamoth"},
        )

    def get_login_qr(self, auth_key: str) -> dict[str, Any]:
        return self.request_json(
            "POST", PATH_GET_LOGIN_QR, key=auth_key, payload={"Check": False, "Proxy": ""}
        )

    def check_login_status(self, auth_key: str, *, timeout_ms: int | None = None) -> dict[str, Any]:
        return self.request_json("GET", PATH_CHECK_LOGIN, key=auth_key, timeout_ms=timeout_ms)

    def get_profile(self, auth_key: str) -> dict[str, Any]:
        return self.request_json("GET", PATH_GET_PROFILE, key=auth_key)

    def send_text(self, auth_key: str, *, to_wxid: str, text: str) -> dict[str, Any]:
        return self.request_json(
            "POST", PATH_SEND_TEXT, key=auth_key,
            payload={
                "MsgItem": [
                    {
                        "ToUserName": to_wxid,
                        "TextContent": text,
                        "MsgType": _WS_TEXT_MSGTYPE,
                        "AtWxIDList": [],
                        "ImageContent": "",
                    }
                ]
            },
        )


class WeixinPadAdapter(Adapter):
    """Personal WeChat adapter over a user-run WeChatPadPro container.

    The container holds the device-login session; this adapter derives an
    authKey from the adminKey, drives the QR login once, then reads inbound
    over the GetSyncMsg WebSocket and sends via HTTP REST. Unlike iLink, it can
    usually initiate to any friend wxid, so unattended speak works once a
    destination is known. Media/voice/file are out of scope for v1.
    """

    max_message_length = WEIXINPAD_TEXT_MAX

    def __init__(
        self,
        config: dict[str, Any],
        *,
        opener=None,
        connect_func: Callable[..., Any] | None = None,
        state_path: str | Path | None = None,
        output: TextIO | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        recv_timeout: float = 1.0,
    ) -> None:
        self.config = dict(config)
        self.host = str(self.config.get("host") or "").strip()
        self.port = _int_config(self.config.get("port"), DEFAULT_PORT)
        self.admin_key = str(self.config.get("admin_key") or "").strip()
        self.api_timeout_ms = _int_config(self.config.get("api_timeout_ms"), DEFAULT_API_TIMEOUT_MS, 1_000)
        self.state_path = (
            Path(state_path).expanduser().resolve() if state_path is not None else default_state_path()
        )
        self._output = output or sys.stderr
        self._sleep = sleep
        self._monotonic = monotonic
        self._connect_func = connect_func
        self._recv_timeout = recv_timeout
        self._closed = threading.Event()
        self._state_lock = threading.RLock()
        self._socket_lock = threading.RLock()
        self._socket: Any | None = None

        scheme = "https" if str(self.config.get("scheme") or "").strip().lower() == "https" else "http"
        self.base_url = f"{scheme}://{self.host}:{self.port}"
        self._api = WeChatPadProAPI(self.base_url, api_timeout_ms=self.api_timeout_ms, opener=opener)

        self.auth_key = ""
        self.wxid = ""
        self._reply_target = ""
        self._last_sender = ""
        self._load_state()

    @property
    def name(self) -> str:
        return "weixinpad"

    def _validate(self) -> None:
        if not self.host:
            raise RuntimeError("WeChatPadPro adapter missing required config: host")
        if not self.admin_key:
            raise RuntimeError("WeChatPadPro adapter missing required config: admin_key")

    def set_reply_target(self, message: InboundMessage) -> None:
        self._reply_target = str(message.sender_id).strip()
        if self._reply_target:
            self._last_sender = self._reply_target

    def clear_reply_target(self) -> None:
        self._reply_target = ""

    # --- state file (0600, atomic; mirrors weixin.py exactly) -----------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"WeChatPadPro state file {self.state_path} is unreadable: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"WeChatPadPro state file {self.state_path} must contain a JSON object")
        self.auth_key = str(data.get("auth_key") or "").strip()
        self.wxid = str(data.get("wxid") or "").strip()
        self._last_sender = str(data.get("last_sender") or "").strip()

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "auth_key": self.auth_key,
            "wxid": self.wxid,
            "last_sender": self._last_sender,
        }

    def _save_state(self) -> None:
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self._state_snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
            tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.write("\n")
                os.replace(tmp, self.state_path)
                os.chmod(self.state_path, 0o600)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    # --- login dance ----------------------------------------------------------

    def _gen_auth_key(self) -> str:
        try:
            payload = self._api.gen_auth_key(self.admin_key)
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"WeChatPadPro unreachable at {self.base_url} ({e}); is the docker container running?"
            ) from None
        if not _code_ok(payload):
            raise RuntimeError(
                "WeChatPadPro GenAuthKey1 rejected the admin_key "
                f"({_format_api_error(payload)}); check adapters.weixinpad.admin_key"
            )
        keys = payload.get("Data")
        auth_key = ""
        if isinstance(keys, list) and keys:
            auth_key = str(keys[0] or "").strip()
        elif isinstance(keys, str):
            auth_key = keys.strip()
        if not auth_key:
            raise RuntimeError("WeChatPadPro GenAuthKey1 returned no auth key")
        return auth_key

    def _print_login_qr(self, qrcode_value: str) -> None:
        print(
            "WeChatPadPro login: scan this QR with your phone WeChat to log in "
            "this device / 微信 iPad 登录：请用手机微信扫码登录本设备。",
            file=self._output,
            flush=True,
        )
        try:
            import qrcode  # type: ignore[import-not-found]

            qr = qrcode.QRCode(border=1)
            qr.add_data(qrcode_value)
            qr.make(fit=True)
            qr.print_ascii(out=self._output, tty=False, invert=True)
        except ImportError:
            print(
                "Install the optional messaging extra for terminal QR ASCII "
                "(qrcode); using the fallback URL below.",
                file=self._output,
                flush=True,
            )
        except Exception as e:
            _log.debug("failed to render WeChatPadPro login QR as ASCII: %s", e)
        # WeChatPadPro already returns a scannable URL; if it is itself a URL we
        # still offer the qrserver fallback rendering of that URL's contents.
        print(
            f"WeChatPadPro QR fallback URL / 微信二维码备用链接: {qr_fallback_url(qrcode_value)}",
            file=self._output,
            flush=True,
        )

    def _qr_value(self, payload: dict[str, Any]) -> str:
        data = payload.get("Data")
        if isinstance(data, dict):
            for key in _QR_URL_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(data, str) and data.strip():
            return data.strip()
        return ""

    def _resolve_wxid(self) -> None:
        try:
            profile = self._api.get_profile(self.auth_key)
        except Exception as e:
            _log.debug("WeChatPadPro GetProfile failed after login: %s", e)
            return
        if not _code_ok(profile):
            return
        data = profile.get("Data")
        if isinstance(data, dict):
            user_info = data.get("userInfo")
            if isinstance(user_info, dict):
                self.wxid = _nested_str(user_info.get("userName")) or self.wxid

    def _login(self) -> None:
        self.auth_key = self._gen_auth_key()
        self._save_state()
        for refresh in range(QRCODE_REFRESHES + 1):
            payload = self._api.get_login_qr(self.auth_key)
            if not _code_ok(payload):
                raise RuntimeError(f"WeChatPadPro GetLoginQrCodeNew failed: {_format_api_error(payload)}")
            qrcode_value = self._qr_value(payload)
            if not qrcode_value:
                raise RuntimeError("WeChatPadPro login returned no QR url")
            self._print_login_qr(qrcode_value)
            if refresh:
                _log.info("WeChatPadPro QR refreshed (%s/%s)", refresh, QRCODE_REFRESHES)
            deadline = self._monotonic() + QRCODE_VALID_SECONDS
            while not self._closed.is_set() and self._monotonic() < deadline:
                status = self._api.check_login_status(self.auth_key)
                if _code_ok(status):
                    data = status.get("Data")
                    state = data.get("loginState") if isinstance(data, dict) else None
                    try:
                        if state is not None and int(state) == _LOGIN_OK_STATE:
                            self._resolve_wxid()
                            self._save_state()
                            _log.info(
                                "WeChatPadPro login confirmed for %s", self.wxid or "(unknown wxid)"
                            )
                            return
                    except (TypeError, ValueError):
                        pass
                self._sleep(2.0)
        raise RuntimeError("WeChatPadPro QR login expired too many times; run the gateway again")

    def _ensure_login(self) -> None:
        if self.auth_key:
            return
        self._login()

    # --- send -----------------------------------------------------------------

    def _target_for_send(self) -> str:
        if self._reply_target:
            return self._reply_target
        if self._last_sender:
            return self._last_sender
        return ""

    def send(self, text: str) -> None:
        self._ensure_login()
        target = self._target_for_send()
        if not target:
            message = (
                "WeChatPadPro has no destination yet: waiting for the human to say hi "
                "(no inbound sender on record)."
            )
            _log.warning(message)
            raise DeliveryDeferred(message)
        try:
            payload = self._api.send_text(self.auth_key, to_wxid=target, text=text)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _log.warning("WeChatPadPro send rate-limited (HTTP 429); message dropped, not queued")
                raise DeliveryDeferred(
                    "WeChatPadPro rate limit (HTTP 429); this message was dropped, not queued"
                ) from None
            raise DeliveryDeferred(
                f"WeChatPadPro sendText HTTP {e.code}; this message was dropped, not queued"
            ) from None
        except urllib.error.URLError as e:
            raise DeliveryDeferred(
                f"WeChatPadPro sendText network failure ({e.reason}); this message was dropped, not queued"
            ) from None
        if _code_ok(payload):
            return
        raise RuntimeError(f"WeChatPadPro SendTextMessage failed: {_format_api_error(payload)}")

    # --- receive (GetSyncMsg WebSocket) --------------------------------------

    def _ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host_port = self.base_url.split("://", 1)[-1]
        qs = urllib.parse.urlencode({"key": self.auth_key})
        return f"{scheme}://{host_port}{PATH_WS_SYNC}?{qs}"

    def _connect(self) -> Any:
        connect_func = self._connect_func
        if connect_func is None:
            try:
                from websockets.sync.client import connect as ws_connect
            except ImportError as e:
                raise RuntimeError(
                    "WeChatPadPro adapter needs the websockets package for the GetSyncMsg WebSocket: "
                    "uv sync --extra server (or --extra messaging)"
                ) from e
            connect_func = ws_connect
        return connect_func(self._ws_url())

    def handle_frame(self, raw: Any, inbox: "queue.Queue[InboundMessage]") -> bool:
        """Decode one WS frame into the inbox. Exposed for tests."""

        try:
            if isinstance(raw, (bytes, bytearray)):
                data = json.loads(bytes(raw).decode("utf-8", errors="replace"))
            elif isinstance(raw, str):
                data = json.loads(raw)
            else:
                data = raw
        except (TypeError, ValueError, json.JSONDecodeError):
            _log.debug("ignored non-JSON WeChatPadPro WS frame")
            return False
        msg = ws_message_to_inbound(data)
        if msg is None:
            return False
        self._last_sender = msg.sender_id
        inbox.put(msg)
        return True

    def _recv_loop(self, sock: Any, inbox: "queue.Queue[InboundMessage]") -> None:
        while not self._closed.is_set():
            try:
                raw = sock.recv(timeout=self._recv_timeout)
            except TimeoutError:
                continue
            self.handle_frame(raw, inbox)

    def run(self, inbox: "queue.Queue[InboundMessage]") -> None:
        self._validate()
        self._ensure_login()
        backoff = 1.0
        while not self._closed.is_set():
            try:
                _log.info("WeChatPadPro connecting GetSyncMsg WebSocket")
                with self._connect() as sock:
                    with self._socket_lock:
                        self._socket = sock
                    _log.info("WeChatPadPro GetSyncMsg connected")
                    backoff = 1.0
                    self._recv_loop(sock, inbox)
            except RuntimeError:
                raise
            except Exception as e:
                if self._closed.is_set():
                    break
                _log.warning(
                    "WeChatPadPro GetSyncMsg dropped/failed: %s; reconnecting in %.0fs", e, backoff
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                with self._socket_lock:
                    self._socket = None

    def close(self) -> None:
        self._closed.set()
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                _log.debug("WeChatPadPro socket close failed", exc_info=True)
