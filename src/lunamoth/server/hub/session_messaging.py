"""Messaging-gateway config + personal-WeChat QR login for the hub.

The self-contained messaging slice of the session RPCs (config get/save with
secret masking, the WeChat iLink QR flow, and the on-disk gateway status). Split
out of sessions.py to keep that module focused; sessions.py re-exports the public
names so dispatch.py / hub.__init__ are unchanged.
"""
from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path
from typing import Any

from ...session import sessions as S
from ..dispatch import RpcError

# Secret masking for the messaging config payload (keys matching this regex are
# shown as the mask, never the raw value; an unchanged mask round-trips to the
# stored secret). Used only by the mask/unmask helpers below.
_SECRET_KEY_RE = re.compile(r"token|secret|key|password|aes", re.IGNORECASE)
_SECRET_MASK = "••••••••"


def _messaging_path(meta: S.SessionMeta) -> Path:
    return meta.root / "messaging.json"


def _read_messaging(meta: S.SessionMeta) -> dict[str, Any]:
    try:
        data = json.loads(_messaging_path(meta).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _mask_secrets(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (_SECRET_MASK if _SECRET_KEY_RE.search(str(k)) and isinstance(v, str) and v
                else _mask_secrets(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_mask_secrets(v) for v in node]
    return node


def _unmask_secrets(node: Any, old: Any) -> Any:
    """Replace mask placeholders with the previously saved secrets.

    A mask with no stored original is a visible error — we never persist the
    placeholder itself as a credential."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            old_v = old.get(k) if isinstance(old, dict) else None
            if v == _SECRET_MASK:
                if not isinstance(old_v, str) or not old_v:
                    raise RpcError(-32602, f"masked value for '{k}' has no stored original")
                out[k] = old_v
            else:
                out[k] = _unmask_secrets(v, old_v)
        return out
    if isinstance(node, list):
        if _SECRET_MASK in node:
            raise RpcError(-32602, "masked values inside arrays cannot be matched to stored originals")
        return [_unmask_secrets(v, None) for v in node]
    return node


def messaging_get(meta: S.SessionMeta) -> dict[str, Any]:
    return {"config": _mask_secrets(_read_messaging(meta)), "path": str(_messaging_path(meta))}


def _merge_messaging(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Field-level merge per the web deck's form contract (webui-needs #7):
    the form sends only the platform on screen and omits unchanged secrets,
    so omitted keys KEEP their stored value, adapters merge per platform,
    and an explicit null deletes a key."""
    out = dict(old)
    for k, v in new.items():
        if v is None:
            out.pop(k, None)
        elif k == "adapters" and isinstance(v, dict):
            adapters = dict(old.get("adapters")) if isinstance(old.get("adapters"), dict) else {}
            for plat, fields in v.items():
                if fields is None:
                    adapters.pop(plat, None)
                    continue
                if not isinstance(fields, dict):
                    adapters[plat] = fields
                    continue
                cur = adapters.get(plat)
                base = dict(cur) if isinstance(cur, dict) else {}
                for f, fv in fields.items():
                    if fv is None:
                        base.pop(f, None)
                    else:
                        base[f] = fv
                adapters[plat] = base
            out["adapters"] = adapters
        else:
            out[k] = v
    return out


def messaging_save(meta: S.SessionMeta, config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise RpcError(-32602, "messaging.save expects config: {...}")
    old = _read_messaging(meta)
    merged = _merge_messaging(old, _unmask_secrets(config, old))
    path = _messaging_path(meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"config": _mask_secrets(merged), "path": str(path)}


# ---- personal WeChat (iLink) QR login for the web gateway page --------------------

def _weixin_config(meta: S.SessionMeta) -> dict[str, Any]:
    adapters = _read_messaging(meta).get("adapters")
    cfg = adapters.get("weixin") if isinstance(adapters, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def weixin_qr(meta: S.SessionMeta) -> dict[str, Any]:
    from ...messaging.weixin import DEFAULT_BOT_TYPE, WeixinAPI, qr_fallback_url

    cfg = _weixin_config(meta)
    api = WeixinAPI(base_url=str(cfg.get("base_url") or ""))
    bot_type = str(cfg.get("bot_type") or DEFAULT_BOT_TYPE)
    try:
        data = api.get_bot_qrcode(bot_type)
    except Exception as exc:  # noqa: BLE001 - surface, never fabricate
        raise RpcError(-32062, f"weixin qr fetch failed: {exc}") from exc
    qrcode_value = str(data.get("qrcode") or "")          # polling token (qr_status)
    scan_content = str(data.get("qrcode_img_content") or "")  # what the phone scans
    if not qrcode_value or not scan_content:
        raise RpcError(-32062, f"weixin returned no qrcode/qrcode_img_content: {data}")
    # The web renders a QR from `scan_content`; `qrcode` only drives qr_status.
    # Encoding the polling token (the old bug) made the QR scan to nothing.
    return {"qrcode": qrcode_value,
            "scan_content": scan_content,
            "img": scan_content,
            "fallback_url": qr_fallback_url(scan_content)}


def weixin_qr_status(meta: S.SessionMeta, qrcode_value: str) -> dict[str, Any]:
    """One poll of the QR login state; a confirmed login is persisted into the
    session's weixin_state.json so the gateway starts already logged in."""
    if not qrcode_value:
        raise RpcError(-32602, "weixin.qr_status needs qrcode")
    from ...messaging.weixin import WeixinAPI, save_login_state

    cfg = _weixin_config(meta)
    api = WeixinAPI(base_url=str(cfg.get("base_url") or ""))
    try:
        status = api.get_qrcode_status(qrcode_value, timeout_ms=5_000)
    except (TimeoutError, urllib.error.URLError) as exc:
        # A slow poll is not a failure — the QR is still pending. The client
        # polls again; surfacing "read operation timed out" as a hard error
        # (which it did) just looked like the gateway broke.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return {"status": "wait"}
        raise RpcError(-32062, f"weixin qr status failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface, never fabricate
        raise RpcError(-32062, f"weixin qr status failed: {exc}") from exc
    raw_status = str(status.get("status") or "wait")
    out: dict[str, Any] = {"status": raw_status}
    if raw_status == "confirmed":
        try:
            out["account_id"] = save_login_state(meta.root / "weixin_state.json", status, cfg)
        except RuntimeError as exc:
            raise RpcError(-32062, str(exc)) from exc
        # Scanning the QR IS configuring the weixin adapter. Ensure messaging.json
        # has an adapters.weixin block (it needs no required fields — login lives
        # in weixin_state.json), else the gateway starts with no adapters and
        # crashes with "no adapters configured" even though you're logged in.
        ensure_weixin_adapter(meta)
    return out


def ensure_weixin_adapter(meta: S.SessionMeta) -> None:
    """Make sure messaging.json declares the weixin adapter so the gateway can
    run it. Idempotent; does not overwrite an existing weixin config."""
    data = _read_messaging(meta)
    adapters = data.get("adapters")
    if not isinstance(adapters, dict):
        adapters = {}
    if not isinstance(adapters.get("weixin"), dict):
        adapters["weixin"] = {}
        data["adapters"] = adapters
        path = _messaging_path(meta)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _gateway_status_from_disk(meta: S.SessionMeta) -> dict[str, Any]:
    path = meta.root / "messaging.json"
    platform = ""
    platforms: list[dict[str, Any]] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        adapters = data.get("adapters") if isinstance(data, dict) else None
        if isinstance(adapters, dict):
            platform = ",".join(sorted(str(k) for k in adapters))
            # Per-platform rows (all stopped on disk): each platform's own
            # `enabled` flag, inheriting the legacy top-level `enabled` when absent.
            legacy = bool(data.get("enabled")) if isinstance(data, dict) else False
            for name in sorted(str(k) for k in adapters):
                ac = adapters.get(name)
                ac = ac if isinstance(ac, dict) else {}
                own = ac.get("enabled")
                on = bool(legacy) if own is None else bool(own)
                platforms.append({"platform": name, "enabled": on, "state": "stopped"})
    except (OSError, json.JSONDecodeError):
        pass
    # No live supervisor: a gateway is always "stopped" on disk. Carry the
    # error_message field so the shape matches GatewayChild.status() for the web.
    return {"platform": platform, "state": "stopped", "detail": "",
            "error_message": "", "pid": 0, "platforms": platforms}
