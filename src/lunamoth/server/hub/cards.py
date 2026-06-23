"""Cards: CRUD, listing, sanitize/UI helpers, and the wake-time merge helper.

This is the card core of the hub: enumerate the deck, save/duplicate/delete/
restore cards, store uploads, fold a world book into a card, and the extension
sanitize/merge helpers shared with the wake path. The LLM-backed drafting lives
in ``card_draft.py``; avatar/art-asset I/O lives in ``avatars.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ...content.cards import CharacterCard, detect_language, looks_like_world_book, merge_world_into_card
from ...content.knobs import normalize_force_roleplay
from ...session import sessions as S
from ..dispatch import RpcError
from ._common import (
    _asset_url,
    _atomic_write_json,
    _clean_theme,
    _sanitize_avatar_svg,
    _slug,
    _writable_card_path,
    is_managed_sidecar_name,
    locked_card_write,
)
from .avatars import _avatar_thumb_uri
from .config import bundled_cards_dir, user_cards_dir, user_worlds_dir

_log = logging.getLogger("lunamoth.server.hub")


def _card_sources() -> dict[str, list[str]]:
    """original card path -> session names that froze a copy of it."""
    refs: dict[str, list[str]] = {}
    for meta in S.list_sessions():
        src = meta.root / "card_source"
        try:
            original = src.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if original:
            refs.setdefault(original, []).append(meta.name)
    return refs


def _copy_card_assets(card: "CharacterCard", dest_dir: Path, src_base: Path | None = None) -> None:
    """Copy the art-asset sidecars a card DECLARES (avatar + sprite/background/
    keyvisual/stickers, preserving their relative names) into dest_dir, reading
    from src_base (defaults to the card's own folder). `card` supplies the
    declared list; `src_base` supplies where the files actually live — so a
    wake that froze an EDITED card still copies from the source template folder.
    Best-effort; a missing/unreadable asset is skipped, never fatal to wake."""
    base = Path(src_base) if src_base else (Path(card.source_path).parent if card.source_path else None)
    if base is None:
        return
    rels: list[str] = []
    if card.avatar_file():
        rels.append(card.avatar_file())
    a = card.assets()
    opts = a.get("options") if isinstance(a.get("options"), dict) else {}
    for kind in ("sprite", "background", "keyvisual"):
        v = a.get(kind)
        if isinstance(v, str):
            rels.append(v)
        # carry the whole candidate gallery, not just the selected one
        lst = opts.get(kind)
        if isinstance(lst, list):
            rels += [s for s in lst if isinstance(s, str)]
    for key in ("stickers", "sticker_sheets"):
        v = a.get(key)
        if isinstance(v, list):
            rels += [s for s in v if isinstance(s, str)]
    # Extra assets (the 素材 tab) travel with the card too: non-managed IMAGE strays in
    # the card root + EVERYTHING in the card's assets/ subdir (any format). So a woken
    # chara keeps its references / docs.
    _EXTRA_IMG = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    try:
        for p in sorted(base.iterdir()):
            if (p.is_file() and not p.name.startswith(".")
                    and p.suffix.lower() in _EXTRA_IMG
                    and p.name not in ("card.png",)
                    and not _is_asset_sidecar(p)):
                rels.append(p.name)
        sub = base / "assets"
        if sub.is_dir():
            for p in sorted(sub.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    rels.append(f"assets/{p.name}")
    except OSError:
        pass
    for rel in rels:
        rel = rel.strip().replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        srcf = base / rel
        if not srcf.is_file():
            continue
        dstf = dest_dir / rel
        try:
            dstf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(srcf, dstf)
        except OSError:
            pass


# Art-asset sidecars live beside the card as `<stem>.<kind>[.<i>].<ext>` images
# (e.g. `Quinn.avatar.png`, `Quinn.sticker.0.png`). They are NOT cards — the deck
# scan must skip them, or each one is tried as a character card and spams a load
# error (avatars always hit this; stickers multiply it 9x). The marker set lives in
# _common (is_managed_sidecar_name) — one source shared with the asset library.
def _is_asset_sidecar(p: Path) -> bool:
    return p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and is_managed_sidecar_name(p.name)


def _iter_card_files(base: Path):
    """Card files under a deck dir: per-character folders (`<Name>/card*.json|png`)
    plus legacy flat files (`*.json|png`) for back-compat. Skips hidden/LICENSE and
    art-asset sidecars (avatar/sprite/background/keyvisual/sticker images)."""
    for p in sorted(base.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            for c in sorted(p.glob("card*.json")) + sorted(p.glob("card*.png")):
                if not _is_asset_sidecar(c):
                    yield c
        elif (p.suffix.lower() in (".json", ".png")
              and not p.stem.startswith("LICENSE") and not _is_asset_sidecar(p)):
            yield p


def _card_entry(path: Path, builtin: bool, refs: dict[str, list[str]]) -> dict[str, Any] | None:
    try:
        card = CharacterCard.load(path)
    except Exception:  # noqa: BLE001 - one bad card must not break the deck
        _log.warning("unreadable card: %s", path, exc_info=True)
        return None
    ext = card.extensions.get("lunamoth", {}) if isinstance(card.extensions, dict) else {}
    # The world is the card's embedded book; surface its name for the deck label.
    world = str(card.character_book.name or "") if card.character_book else ""
    theme_color = ""
    avatar_svg = ""
    tagline = ""
    force_roleplay: bool | None = None
    theme = card.theme_colors()
    avatar_uri = _avatar_thumb_uri(path, card)
    if isinstance(ext, dict):
        theme_color = theme.get("primary", "")
        avatar_svg = _sanitize_avatar_svg(ext.get("avatar_svg"))[0]
        tagline = str(ext.get("tagline") or "")
        # The card FIELD is a boolean; bridge a legacy `embodiment: "actor"` too.
        force_roleplay = normalize_force_roleplay(ext.get("force_roleplay"))
        if force_roleplay is None and str(ext.get("embodiment") or "").lower() == "actor":
            force_roleplay = True
    used_by = refs.get(str(path), [])
    full_tags = [str(t) for t in (card.tags or [])]
    # The default-card marker must survive display truncation: the deck/welcome
    # key on `default`, and a card can carry it past the 4-tag display cap.
    is_default = any(t.strip().lower() == "default" for t in full_tags)
    return {
        "path": str(path),
        "name": card.name or path.stem,
        "lang": card.language,
        "tags": full_tags[:4],
        "default": is_default,
        "world": world,
        "builtin": builtin,
        "draft": bool(isinstance(ext, dict) and ext.get("draft")),
        "frozen": bool(used_by),
        "used_by": used_by,
        "locked": False,   # a deck template — editable/wakeable (overridden for chara cards)
        "owner": "",       # the chara that owns this card, for locked session cards
        "creator_notes": (card.creator_notes or "")[:300],
        "tagline": tagline[:160],
        "theme_color": theme_color,
        "theme": {"primary": theme.get("primary", ""), "secondary": theme.get("secondary", "")},
        "avatar_svg": avatar_svg,
        "avatar_uri": avatar_uri,
        "sprite_url": _asset_url(card.asset_path("sprite")),
        "bg_url": _asset_url(card.asset_path("background")),
        "keyvisual_url": _asset_url(card.asset_path("keyvisual")),
        "stickers_urls": [u for u in (_asset_url(p) for p in card.sticker_paths()) if u],
        "sticker_sheets_urls": [u for u in (_asset_url(p) for p in card.sticker_sheets()) if u],
        # The non-destructive candidate gallery per kind (selected = *_url above).
        "sprite_options": [u for u in (_asset_url(p) for p in card.asset_options("sprite")) if u],
        "bg_options": [u for u in (_asset_url(p) for p in card.asset_options("background")) if u],
        "keyvisual_options": [u for u in (_asset_url(p) for p in card.asset_options("keyvisual")) if u],
        "avatar_options": [u for u in (_asset_url(p) for p in card.asset_options("avatar")) if u],
        "force_roleplay": bool(force_roleplay),
    }


def list_cards() -> list[dict[str, Any]]:
    """Every deck card. Shadowing semantics (webui-needs #11): a USER card
    hides only a BUILTIN of the same name+lang (local-first, like skills),
    and the surviving entry says so via `shadows: <hidden path>`. User cards
    never hide each other — same-name user files all appear (path is the
    identity); silent disappearance is what read as 'the locked card moved
    and unlocked'."""
    refs = _card_sources()
    out: list[dict[str, Any]] = []
    user_by_key: dict[str, dict[str, Any]] = {}
    for base, builtin in ((user_cards_dir(), False), (bundled_cards_dir(), True)):
        if not base.is_dir():
            continue
        for p in _iter_card_files(base):
            entry = _card_entry(p, builtin, refs)
            if not entry:
                continue
            key = entry["name"] + entry["lang"]
            if builtin and key in user_by_key:
                user_by_key[key]["shadows"] = entry["path"]
                continue
            if not builtin:
                user_by_key.setdefault(key, entry)
            out.append(entry)
    # Each living chara owns its own frozen card — a LOCKED deck entry (browse /
    # copy / wake only), so every card in the system is browsable in the deck.
    for meta in S.list_sessions():
        entry = _session_card_entry(meta)
        if entry is not None:
            out.append(entry)
    return out


def _session_card_entry(meta: S.SessionMeta) -> dict[str, Any] | None:
    """A chara's frozen card as a LOCKED deck entry (owned by the chara)."""
    frozen = meta.root / "card.json"
    if not frozen.exists():
        frozen = meta.root / "card.png"
    if not frozen.exists():
        return None
    entry = _card_entry(frozen, False, {})
    if entry is None:
        return None
    entry["locked"] = True
    entry["owner"] = meta.name
    entry["frozen"] = True
    entry["used_by"] = [meta.name]
    return entry


@locked_card_write
def save_card(data: dict[str, Any], path: str = "") -> dict[str, Any]:
    """Write a V3 card JSON into the user deck (create flow / drafts)."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise RpcError(-32602, "card.save expects a {spec, data:{...}} card object")
    name = str(data["data"].get("name") or "").strip()
    if not name:
        raise RpcError(-32602, "the card needs a name")
    target: Path
    if path:
        target = Path(path)
        if user_cards_dir() not in target.parents:
            raise RpcError(-32031, "only cards in the user deck can be written")
    else:
        base = user_cards_dir()
        base.mkdir(parents=True, exist_ok=True)
        stem = _slug(name)
        target = base / f"{stem}.json"
        n = 2
        while target.exists():
            target = base / f"{stem}-{n}.json"
            n += 1
    data.setdefault("version", "1.0")  # our own card format; we no longer emit the ST spec markers
    data["name"] = name
    _sanitize_card_extensions(data)
    _atomic_write_json(target, data)
    return {"path": str(target)}


def _deep_patch(base: Any, over: Any) -> Any:
    """Patch semantics: deep-merge ``over`` onto ``base`` — provided keys WIN (even an
    empty value, which is an intentional clear), nested dicts merge, keys absent from
    ``over`` are preserved. Unlike ``_merge_preserving`` (the wake backstop), the caller
    of a field patch owns exactly what it sends, so empty means clear."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_patch(out.get(k), v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out
    return over


@locked_card_write
def patch_card(path: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Field-level merge-write of a card's ``data``: only the provided keys change,
    everything else is preserved (deep patch). Accepts a deck card OR a LIVING chara's
    own frozen session card (via ``_writable_card_path``) — so editing a running chara
    persists immediately, without the whole-card-REPLACE risk of ``save_card`` (and
    without ``save_card``'s deck-only gate). Activation is the caller's concern: visuals
    are live, volatile-tail fields take effect next turn, identity fields next start /
    on apply. Returns the written path."""
    if not isinstance(fields, dict) or not fields:
        raise RpcError(-32602, "card.patch expects a non-empty fields object")
    target = _writable_card_path(path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RpcError(-32602, "card is not a JSON object")
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    raw["data"] = _deep_patch(data, fields)
    name = raw["data"].get("name")
    if isinstance(name, str) and name.strip():
        raw["name"] = name.strip()  # keep the top-level mirror in sync
    _sanitize_card_extensions(raw)
    _atomic_write_json(target, raw)
    return {"path": str(target)}


def _merge_preserving(base: Any, over: Any) -> Any:
    """Deep-merge ``over`` onto ``base``, but an EMPTY value in ``over`` never
    wipes a non-empty value in ``base``.

    Root-fix for the wake data-loss bug: the wake editor round-trips the WHOLE
    card through UI fields and submits it back, but (a) it renders no field for
    mes_example / system_prompt / post_history_instructions, and (b) a load/value
    hiccup (e.g. card.read caught to null) can blank every field. Either way an
    empty submitted field would overwrite the source's real content and freeze a
    persona-less, greeting-less chara. Merging the edit ONTO the freshly-loaded
    SOURCE card with this rule means a blank edit keeps the source value, so the
    frozen chara always carries the full persona, first_mes, and avatar
    declaration — while a genuinely-edited (non-empty) field still wins."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge_preserving(out[k], v) if k in out else v
        return out
    if over in ("", None, [], {}) and base not in ("", None, [], {}):
        return base
    return over


def _sanitize_card_extensions(card: dict[str, Any]) -> None:
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    ext_root = data.get("extensions")
    if not isinstance(ext_root, dict):
        return
    lunamoth = ext_root.get("lunamoth")
    if not isinstance(lunamoth, dict):
        return
    svg, _note = _sanitize_avatar_svg(lunamoth.get("avatar_svg"))
    if svg:
        lunamoth["avatar_svg"] = svg
    else:
        lunamoth.pop("avatar_svg", None)
    # avatar_file (sidecar reference): keep only a bare, traversal-free filename.
    af = lunamoth.get("avatar_file")
    if isinstance(af, str) and af.strip() and "/" not in af and "\\" not in af and ".." not in af:
        lunamoth["avatar_file"] = af.strip()
    else:
        lunamoth.pop("avatar_file", None)
    # Dual theme {primary, secondary}; fold a legacy theme_color into primary.
    theme = _clean_theme(lunamoth.get("theme"), lunamoth.get("theme_color"))
    if theme:
        lunamoth["theme"] = theme
    else:
        lunamoth.pop("theme", None)
    lunamoth.pop("theme_color", None)
    # The card FIELD is a boolean; bridge a legacy `embodiment: "actor"`, else omit.
    forced = normalize_force_roleplay(lunamoth.get("force_roleplay"))
    if forced is None and str(lunamoth.get("embodiment") or "").lower() == "actor":
        forced = True
    lunamoth.pop("embodiment", None)
    if forced:
        lunamoth["force_roleplay"] = True
    else:
        lunamoth.pop("force_roleplay", None)


def _safe_extensions_for_ui(extensions: dict[str, Any]) -> dict[str, Any]:
    """Copy card extensions with lunamoth visual fields sanitized for rendering."""
    if not isinstance(extensions, dict):
        return {}
    out = dict(extensions)
    lunamoth = out.get("lunamoth")
    if not isinstance(lunamoth, dict):
        return out
    safe = dict(lunamoth)
    svg, _note = _sanitize_avatar_svg(safe.get("avatar_svg"))
    if svg:
        safe["avatar_svg"] = svg
    else:
        safe.pop("avatar_svg", None)
    theme = _clean_theme(safe.get("theme"), safe.get("theme_color"))
    if theme:
        safe["theme"] = theme
        # Mirror primary into the legacy field so older renderers still color.
        safe["theme_color"] = theme["primary"]
    else:
        safe.pop("theme", None)
        safe.pop("theme_color", None)
    forced = normalize_force_roleplay(safe.get("force_roleplay"))
    if forced is None and str(safe.get("embodiment") or "").lower() == "actor":
        forced = True
    safe.pop("embodiment", None)
    if forced:
        safe["force_roleplay"] = True
    else:
        safe.pop("force_roleplay", None)
    out["lunamoth"] = safe
    return out


def duplicate_card(path: str) -> dict[str, Any]:
    """Copy a card into the user deck as a clearly distinct sibling.

    The copy gets a language-appropriate name suffix (otherwise it is
    indistinguishable from a frozen original on the deck — the '锁着的卡片
    复制之后就解锁了' confusion), loses the "default" tag (a copy must never
    steal the bundled-default slot), and PNG cards are lifted to JSON via
    their embedded card data."""
    p = Path(str(path or ""))
    if not p.is_file():
        raise RpcError(-32035, f"no such card: {path}")
    if p.suffix.lower() == ".png":
        from ...content.cards import _card_json_from_png

        try:
            card = _card_json_from_png(p)
        except Exception as exc:  # noqa: BLE001
            raise RpcError(-32035, f"could not read the PNG card: {exc}") from exc
    else:
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32035, f"unreadable card: {exc}") from exc
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        raise RpcError(-32602, "card.duplicate expects a V2/V3 card")
    data = card["data"]
    name = str(data.get("name") or p.stem).strip() or p.stem
    lang = detect_language(str(p), str(data.get("description") or "") + str(data.get("name") or ""))
    suffix = "（副本）" if lang == "zh" else " (copy)"
    if not name.endswith(suffix):
        data["name"] = f"{name}{suffix}"
    tags = data.get("tags")
    if isinstance(tags, list):
        data["tags"] = [t for t in tags if str(t).strip().lower() != "default"]
    # A card is a FOLDER (card.json + art-asset sidecars). Write the copy into its
    # OWN folder and bring the sidecars along, so duplicating doesn't leave the new
    # card pointing at the original's art (or broken). Copy resolves relative names
    # against each card's parent dir, so same names + copied files = correct art.
    base = user_cards_dir()
    base.mkdir(parents=True, exist_ok=True)
    stem = _slug(str(data.get("name") or p.stem)) or "card"
    dest_dir = base / stem
    n = 2
    while dest_dir.exists():
        dest_dir = base / f"{stem}-{n}"
        n += 1
    dest_dir.mkdir(parents=True)
    saved = save_card(card, path=str(dest_dir / "card.json"))  # sanitize + write
    try:
        _copy_card_assets(CharacterCard.load(saved["path"]), dest_dir, src_base=p.parent)
    except Exception:  # noqa: BLE001 — best-effort; a copy without art still beats failing
        pass
    return saved


def merge_world(card_path: str, world: Any) -> dict[str, Any]:
    """Fold a standalone ST world book into a card's embedded character_book.

    This is the import path now that the card is the ONE file: entries are
    appended (identical keys+content are skipped) and the card is saved via
    the normal card-save path, sanitization included. `world` may be a parsed
    world-book object or a path to a world-book .json.
    """
    p = Path(str(card_path or ""))
    if p.suffix.lower() != ".json":
        raise RpcError(-32602, "card.merge_world works on .json cards")
    if isinstance(world, str):
        wp = Path(world)
        if user_worlds_dir() not in wp.parents:
            raise RpcError(-32031, "world paths must live in the uploaded worlds directory")
        try:
            world = json.loads(wp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32035, f"unreadable world book: {exc}") from exc
    if not isinstance(world, dict) or not world.get("entries"):
        raise RpcError(-32602, "card.merge_world expects a world book with entries")
    try:
        card = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RpcError(-32035, f"unreadable card: {exc}") from exc
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        raise RpcError(-32602, "card.merge_world expects a V2/V3 card (with a data block)")
    added = merge_world_into_card(card, world)
    saved = save_card(card, path=str(p))  # user-deck-only write + sanitization
    book = card["data"].get("character_book") or {}
    return {"path": saved["path"], "added": added, "entries": len(book.get("entries") or [])}


def store_upload(name: str, body: bytes) -> dict[str, Any]:
    """Store an uploaded file: cards go to the user deck; a .json that parses
    as a standalone world book (entries, no card data) is stored aside and
    reported as kind="world" so the deck can offer 'merge into card X'."""
    suffix = Path(name).suffix.lower()
    kind, base = "card", user_cards_dir()
    if suffix == ".json":
        try:
            obj = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        if looks_like_world_book(obj):
            kind, base = "world", user_worlds_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = base / Path(name).name
    n = 2
    while target.exists():
        target = base / f"{Path(name).stem}-{n}{suffix}"
        n += 1
    target.write_bytes(body)
    return {"path": str(target), "kind": kind}


def _trash_cards_dir() -> Path:
    d = S.lunamoth_home() / ".trash" / "cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_card(path: str) -> dict[str, Any]:
    """SOFT delete: move the card file into ~/.lunamoth/.trash/cards/<id>/ (with an
    origin manifest) instead of unlinking, so it's recoverable via card.restore.
    Returns the trash_id the UI uses for an Undo affordance."""
    p = Path(path)
    if user_cards_dir() not in p.parents:
        raise RpcError(-32031, "built-in cards cannot be deleted")
    if _card_sources().get(str(p)):
        raise RpcError(-32032, "this card is referenced by a living chara")
    if not p.exists():
        return {"ok": True, "trash_id": None}
    tid = os.urandom(6).hex()
    dest_dir = _trash_cards_dir() / tid
    dest_dir.mkdir(parents=True, exist_ok=True)
    p.replace(dest_dir / p.name)
    (dest_dir / "origin.json").write_text(
        json.dumps({"path": str(p), "name": p.name, "ts": int(time.time())}),
        encoding="utf-8",
    )
    return {"ok": True, "trash_id": tid}


def restore_card(trash_id: str) -> dict[str, Any]:
    """Undo a soft delete: move the trashed card file back to its original path."""
    tid = (trash_id or "").strip()
    # guard against path traversal — trash_id is an opaque hex token
    if not tid or not re.fullmatch(r"[0-9a-f]{1,32}", tid):
        raise RpcError(-32033, "unknown trash id")
    dest_dir = _trash_cards_dir() / tid
    manifest = dest_dir / "origin.json"
    if not manifest.exists():
        raise RpcError(-32033, "nothing to restore")
    info = json.loads(manifest.read_text(encoding="utf-8"))
    orig = Path(str(info.get("path") or ""))
    src = dest_dir / str(info.get("name") or "")
    if not src.exists() or user_cards_dir() not in orig.parents:
        raise RpcError(-32033, "trashed card cannot be restored")
    orig.parent.mkdir(parents=True, exist_ok=True)
    src.replace(orig)
    manifest.unlink(missing_ok=True)
    try:
        dest_dir.rmdir()
    except OSError:
        pass
    return {"ok": True, "path": str(orig)}


def _book_to_dict(book: Any) -> dict[str, Any] | None:
    if book is None or not hasattr(book, "entries"):
        return None
    entries = []
    for i, e in enumerate(getattr(book, "entries", []) or []):
        entries.append({
            "id": getattr(e, "entry_id", i),
            "keys": list(getattr(e, "keys", []) or []),
            "secondary_keys": list(getattr(e, "secondary_keys", []) or []),
            "content": str(getattr(e, "content", "")),
            "constant": bool(getattr(e, "constant", False)),
            "selective": bool(getattr(e, "selective", False)),
            "enabled": bool(getattr(e, "enabled", True)),
            "insertion_order": int(getattr(e, "order", i) or i),
            "comment": str(getattr(e, "comment", "")),
        })
    return {"name": str(getattr(book, "name", "") or ""), "entries": entries}
