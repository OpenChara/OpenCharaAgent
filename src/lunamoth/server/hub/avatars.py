"""Avatar & art-asset sidecar I/O for the hub.

The avatar is a tiny image inlined as a data-URI in every hub.state; the heavier
art (sprite/background/keyvisual) rides cacheable /asset URLs. Both live as
sidecar files beside the card; this module reads, validates and writes them and
keeps the card's ``extensions.lunamoth`` pointers in sync.
"""
from __future__ import annotations

import base64
import binascii
import json
import urllib.parse
from pathlib import Path
from typing import Any

from ...content.cards import CharacterCard
from ...content.imaging import CAP_ART, CAP_STICKER, avatar_thumb_data_uri, compress_image_bytes
from ..dispatch import RpcError
from ._common import HubRpcError, _asset_url, _sanitize_avatar_svg, _writable_card_path

# ---- avatar sidecar storage --------------------------------------------------
# The avatar is a SEPARATE file beside the card (the card stays the soul; the
# avatar is presentation). Supported uploads: png/jpg/jpeg/svg.
_AVATAR_EXTS = ("png", "jpg", "jpeg", "svg")
_AVATAR_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"}
_AVATAR_MAX_BYTES = 1024 * 1024  # ~1MB cap
# Magic-byte sniff so an uploaded ".png" really is one (defence in depth).
_AVATAR_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff"}


def _avatar_sidecar_path(card_path: Path, ext: str) -> Path:
    return card_path.with_name(f"{card_path.stem}.avatar.{ext}")


def _avatar_data_uri(card_path: Path, card: "CharacterCard") -> str:
    """Resolve a card's avatar to a FULL-res data-URI: sidecar first, inline SVG
    fallback, else ''. This is the `card.avatar_read` path — the heavy one a
    caller asks for explicitly. The board list uses `_avatar_thumb_uri`."""
    sidecar = card.avatar_path()
    if sidecar is not None:
        ext = sidecar.suffix.lower().lstrip(".")
        mime = _AVATAR_MIME.get(ext, "application/octet-stream")
        data = base64.b64encode(sidecar.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    ext = card.extensions.get("lunamoth") if isinstance(card.extensions, dict) else None
    if isinstance(ext, dict):
        svg, _note = _sanitize_avatar_svg(ext.get("avatar_svg"))
        if svg:
            return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)
    return ""


def _avatar_thumb_uri(card_path: Path, card: "CharacterCard") -> str:
    """The SMALL inline avatar for list_cards (sent in every hub.state): a
    downscaled WEBP thumbnail (~5–15KB) of the raster sidecar, the inline SVG
    fallback otherwise. The full-res sidecar still rides /asset & avatar_read."""
    sidecar = card.avatar_path()
    if sidecar is not None and sidecar.suffix.lower().lstrip(".") != "svg":
        thumb = avatar_thumb_data_uri(sidecar)
        if thumb:
            return thumb
        # Undecodable raster: fall back to the full-res embed rather than nothing.
        return _avatar_data_uri(card_path, card)
    return _avatar_data_uri(card_path, card)


def avatar_read(path: str) -> dict[str, Any]:
    """The card's avatar as a data-URI an <img> can use (sidecar preferred)."""
    p = Path(str(path or ""))
    if not p.is_file():
        raise RpcError(-32035, f"no such card: {path}")
    try:
        card = CharacterCard.load(p)
    except Exception as exc:  # noqa: BLE001
        raise RpcError(-32035, f"unreadable card: {exc}") from exc
    return {"data_uri": _avatar_data_uri(p, card) or None}


def avatar_upload(path: str, data_b64: str, ext: str) -> dict[str, Any]:
    """Validate an uploaded avatar, write it as a sidecar, point the card at it.

    Accepts png/jpg/jpeg/svg, caps at ~1MB. SVG must pass the same safety
    checks as a generated one (script/foreignObject/text/event-handler/
    external-ref free, viewBox 0 0 64 64). The inline `avatar_svg` fallback is
    dropped once a sidecar exists — the sidecar is now the source of truth."""
    target = _writable_card_path(path)
    ext = str(ext or "").strip().lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpeg"  # keep the extension the caller chose; mime is the same
    if ext not in _AVATAR_EXTS:
        raise RpcError(-32602, f"unsupported avatar type: .{ext} (allowed: {', '.join(_AVATAR_EXTS)})")
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RpcError(-32602, f"avatar data is not valid base64: {exc}") from exc
    if not raw:
        raise RpcError(-32602, "avatar data is empty")
    if len(raw) > _AVATAR_MAX_BYTES:
        raise HubRpcError(-32602, "avatar is too large (max 1MB)",
                          {"kind": "avatar_size", "detail": f"{len(raw)} bytes"})
    if ext == "svg":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RpcError(-32602, f"SVG is not valid UTF-8: {exc}") from exc
        svg, note = _sanitize_avatar_svg(text)
        if not svg:
            raise HubRpcError(-32050, "the SVG did not pass the safety checks",
                              {"kind": "avatar_svg", "detail": note})
        payload = svg.encode("utf-8")
    else:
        magic = _AVATAR_MAGIC.get(ext)
        if magic and not raw.startswith(magic):
            raise HubRpcError(-32602, f"the file does not look like a .{ext} image",
                              {"kind": "avatar_type", "detail": "magic-byte mismatch"})
        payload = raw
    # One sidecar per card: remove any stale sidecar of a different extension.
    for old in _AVATAR_EXTS:
        sc = _avatar_sidecar_path(target, old)
        if sc.name != _avatar_sidecar_path(target, ext).name and sc.exists():
            try:
                sc.unlink()
            except OSError:
                pass
    sidecar = _avatar_sidecar_path(target, ext)
    sidecar.write_bytes(payload)
    # Point the card at the sidecar; drop the inline fallback (sidecar wins now).
    raw_card = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw_card, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw_card.get("data")
    if not isinstance(data, dict):
        data = raw_card["data"] = {}
    ext_root = data.get("extensions")
    if not isinstance(ext_root, dict):
        ext_root = data["extensions"] = {}
    lm = ext_root.get("lunamoth")
    if not isinstance(lm, dict):
        lm = ext_root["lunamoth"] = {}
    lm["avatar_file"] = sidecar.name
    lm.pop("avatar_svg", None)
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "avatar_file": sidecar.name,
            "data_uri": f"data:{_AVATAR_MIME[ext]};base64,{base64.b64encode(payload).decode('ascii')}"}


# ---- art-asset sidecars (sprite / background / keyvisual) --------------------
# The heavy art (R9 visual set + user uploads). Unlike the tiny avatar (inlined as
# a data-URI in every hub.state), these ride cacheable /asset URLs, so the cap is
# generous and they're never base64-inlined into list_cards.
_ART_ASSET_KINDS = ("sprite", "background", "keyvisual")
_ART_EXTS = ("png", "jpg", "jpeg", "webp")
_ART_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
_ART_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff"}
_ART_MAX_BYTES = 16 * 1024 * 1024  # generated art is a few MB; cap well above that


def _art_sidecar_path(card_path: Path, kind: str, ext: str) -> Path:
    return card_path.with_name(f"{card_path.stem}.{kind}.{ext}")


def _looks_like(raw: bytes, ext: str) -> bool:
    if ext == "webp":
        return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    magic = _ART_MAGIC.get(ext)
    return not magic or raw.startswith(magic)


def asset_save(path: str, kind: str, data_b64: str, ext: str) -> dict[str, Any]:
    """Write a sprite/background/keyvisual sidecar (upload OR a saved generation)
    and point the card's ``extensions.lunamoth.assets[kind]`` at it. png/jpg/webp,
    capped at 16MB. One sidecar per kind (stale extensions are removed)."""
    target = _writable_card_path(path)
    kind = str(kind or "").strip().lower()
    if kind not in _ART_ASSET_KINDS:
        raise RpcError(-32602, f"unknown art asset kind: {kind} (one of {', '.join(_ART_ASSET_KINDS)})")
    ext = str(ext or "").strip().lower().lstrip(".")
    if ext not in _ART_EXTS:
        raise RpcError(-32602, f"unsupported art type: .{ext} (allowed: {', '.join(_ART_EXTS)})")
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RpcError(-32602, f"asset data is not valid base64: {exc}") from exc
    if not raw:
        raise RpcError(-32602, "asset data is empty")
    if len(raw) > _ART_MAX_BYTES:
        raise HubRpcError(-32602, "asset is too large (max 16MB)",
                          {"kind": "asset_size", "detail": f"{len(raw)} bytes"})
    if not _looks_like(raw, ext):
        raise HubRpcError(-32602, f"the file does not look like a .{ext} image",
                          {"kind": "asset_type", "detail": "magic-byte mismatch"})
    # Compress on save (cap long side, preserve format+alpha) so user uploads
    # don't reintroduce huge files. Best-effort: a non-shrinkable image is kept
    # as-is, so the already-validated bytes are never lost.
    raw = compress_image_bytes(raw, ext, CAP_ART)
    keep = _art_sidecar_path(target, kind, ext).name
    for old in _ART_EXTS:
        sc = _art_sidecar_path(target, kind, old)
        if sc.name != keep and sc.exists():
            try:
                sc.unlink()
            except OSError:
                pass
    sidecar = _art_sidecar_path(target, kind, ext)
    sidecar.write_bytes(raw)
    raw_card = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw_card, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw_card.get("data")
    if not isinstance(data, dict):
        data = raw_card["data"] = {}
    ext_root = data.get("extensions")
    if not isinstance(ext_root, dict):
        ext_root = data["extensions"] = {}
    lm = ext_root.get("lunamoth")
    if not isinstance(lm, dict):
        lm = ext_root["lunamoth"] = {}
    assets = lm.get("assets")
    if not isinstance(assets, dict):
        assets = lm["assets"] = {}
    assets[kind] = sidecar.name
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "kind": kind, "file": sidecar.name, "url": _asset_url(sidecar)}


# ---- sticker set (表情包) — a LIST of cut cells, not a single sidecar -----------
_STICKER_MAX = 9


def _sticker_sidecar_path(card_path: Path, i: int) -> Path:
    return card_path.with_name(f"{card_path.stem}.sticker.{i}.png")


def stickers_save(path: str, items: list[str]) -> dict[str, Any]:
    """Write a SET of sticker sidecars (``<stem>.sticker.<i>.png``) and point the
    card's ``extensions.lunamoth.assets['stickers']`` at the ordered name list.
    ``items`` = base64 PNG cells (the cut 3x3 grid). Replaces any existing set; each
    cell is magic-checked + compressed to the sticker cap. png only."""
    target = _writable_card_path(path)
    if not isinstance(items, list) or not items:
        raise RpcError(-32602, "stickers payload must be a non-empty list of PNG cells")
    if len(items) > _STICKER_MAX:
        raise RpcError(-32602, f"too many stickers (max {_STICKER_MAX})")
    decoded: list[bytes] = []
    for n, b in enumerate(items):
        try:
            raw = base64.b64decode(str(b or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RpcError(-32602, f"sticker {n} is not valid base64: {exc}") from exc
        if not raw:
            raise RpcError(-32602, f"sticker {n} is empty")
        if len(raw) > _ART_MAX_BYTES:
            raise HubRpcError(-32602, "a sticker is too large (max 16MB)",
                              {"kind": "asset_size", "detail": f"{len(raw)} bytes"})
        if not _looks_like(raw, "png"):
            raise HubRpcError(-32602, "a sticker is not a PNG image",
                              {"kind": "asset_type", "detail": "magic-byte mismatch"})
        decoded.append(compress_image_bytes(raw, "png", CAP_STICKER))
    # clear any stale cells (a previous set may have had more), then write the new set
    for old in range(_STICKER_MAX):
        sc = _sticker_sidecar_path(target, old)
        if sc.exists():
            try:
                sc.unlink()
            except OSError:
                pass
    names: list[str] = []
    for i, raw in enumerate(decoded):
        sc = _sticker_sidecar_path(target, i)
        sc.write_bytes(raw)
        names.append(sc.name)
    raw_card = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw_card, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw_card.get("data")
    if not isinstance(data, dict):
        data = raw_card["data"] = {}
    ext_root = data.get("extensions")
    if not isinstance(ext_root, dict):
        ext_root = data["extensions"] = {}
    lm = ext_root.get("lunamoth")
    if not isinstance(lm, dict):
        lm = ext_root["lunamoth"] = {}
    assets = lm.get("assets")
    if not isinstance(assets, dict):
        assets = lm["assets"] = {}
    assets["stickers"] = names
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    urls = [_asset_url(_sticker_sidecar_path(target, i)) for i in range(len(names))]
    return {"path": str(target), "kind": "stickers", "files": names, "urls": urls}


def visual_brief_save(path: str, brief: dict) -> dict[str, Any]:
    """Persist the visual brief on the card (``extensions.lunamoth.visual_brief``) so
    it's REUSED instead of re-generated (the brief is an LLM call). Writable cards
    only (deck cards + a chara's own session card); builtin/PNG raise."""
    target = _writable_card_path(path)
    if not isinstance(brief, dict):
        raise RpcError(-32602, "visual_brief must be an object")
    raw_card = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw_card, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw_card.get("data")
    if not isinstance(data, dict):
        data = raw_card["data"] = {}
    ext_root = data.get("extensions")
    if not isinstance(ext_root, dict):
        ext_root = data["extensions"] = {}
    lm = ext_root.get("lunamoth")
    if not isinstance(lm, dict):
        lm = ext_root["lunamoth"] = {}
    lm["visual_brief"] = brief
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(target)}


def asset_delete(path: str, kind: str) -> dict[str, Any]:
    """Remove an art asset (avatar / sprite / background / keyvisual / stickers):
    delete its sidecar file(s) and drop the card's pointer. Idempotent."""
    target = _writable_card_path(path)
    kind = str(kind or "").strip().lower()
    raw_card = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw_card, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw_card.get("data") if isinstance(raw_card.get("data"), dict) else {}
    ext_root = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    lm = ext_root.get("lunamoth") if isinstance(ext_root.get("lunamoth"), dict) else {}
    removed = False
    if kind == "avatar":
        for e in _AVATAR_EXTS:
            sc = _avatar_sidecar_path(target, e)
            if sc.exists():
                try:
                    sc.unlink(); removed = True
                except OSError:
                    pass
        if isinstance(lm, dict):
            lm.pop("avatar_file", None)
            lm.pop("avatar_svg", None)
    elif kind in _ART_ASSET_KINDS:
        for e in _ART_EXTS:
            sc = _art_sidecar_path(target, kind, e)
            if sc.exists():
                try:
                    sc.unlink(); removed = True
                except OSError:
                    pass
        assets = lm.get("assets") if isinstance(lm, dict) else None
        if isinstance(assets, dict):
            assets.pop(kind, None)
    elif kind == "stickers":
        for i in range(_STICKER_MAX):
            sc = _sticker_sidecar_path(target, i)
            if sc.exists():
                try:
                    sc.unlink(); removed = True
                except OSError:
                    pass
        assets = lm.get("assets") if isinstance(lm, dict) else None
        if isinstance(assets, dict):
            assets.pop("stickers", None)
    else:
        raise RpcError(-32602, f"unknown asset kind: {kind}")
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "kind": kind, "removed": removed}
