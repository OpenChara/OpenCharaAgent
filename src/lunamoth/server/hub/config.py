"""Desktop config: global model defaults + the named provider key store.

The raw desktop.json holds the top-level defaults plus a sibling "keys" map.
Secrets never travel in a public payload — they're reduced to has_<field> flags.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...config import content_dir
from ...content import image_providers as image_providers
from ...session import sessions as S
from ..dispatch import RpcError


# ---- paths -------------------------------------------------------------------

def desktop_config_path() -> Path:
    return S.lunamoth_home() / "desktop.json"


def user_cards_dir() -> Path:
    return S.lunamoth_home() / "cards"


def bundled_cards_dir() -> Path:
    return content_dir("cards")


def user_worlds_dir() -> Path:
    """Uploaded standalone world books wait here until merged into a card."""
    return S.lunamoth_home() / "worlds"


# ---- global model defaults -----------------------------------------------------

# image_provider/image_model: the GLOBAL image-generation selection (provider +
# model), set in Settings · 模型 · 生图模型 and read by tools/builtin/_image_gen.py
# (which reads desktop.json directly — tools/ must not import server/). The image
# KEY is NOT a separate field: it is resolved from the named provider keyring (the
# same unified path as text), surfaced via has_image_key (= the ACTIVE image
# provider has a key).
# matte_model: the active local matting (抠像) model id, set in Settings·生图 and
# read by lunamoth.visuals.matte.selected_model(). Not a secret.
_DEFAULT_FIELDS = ("provider", "base_url", "api_key", "model", "ui_lang", "ui_theme",
                   "image_provider", "image_model", "matte_model",
                   "reasoning", "vision_model", "vision_provider",
                   "card_model", "card_provider",
                   "image_prompt_model", "image_prompt_provider", "model_context",
                   "model_refresh_interval")
# Default fields whose value is a secret: stripped from every public payload,
# surfaced only as a has_<field> presence flag.
_SECRET_FIELDS = ("api_key",)


def _read_desktop_raw() -> dict[str, Any]:
    try:
        raw = json.loads(desktop_config_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_desktop_raw(raw: dict[str, Any]) -> None:
    path = desktop_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)  # holds API keys
    except OSError:
        pass


def load_defaults() -> dict[str, str]:
    raw = _read_desktop_raw()
    return {k: raw[k] for k in _DEFAULT_FIELDS if isinstance(raw.get(k), str)}


def save_defaults(updates: dict[str, str]) -> dict[str, str]:
    # Merge into the RAW file so sibling top-level sections (the named "keys"
    # store) survive a defaults write.
    raw = _read_desktop_raw()
    for k in _DEFAULT_FIELDS:
        if k in updates and isinstance(updates[k], str):
            raw[k] = updates[k]
    _write_desktop_raw(raw)
    return {k: raw[k] for k in _DEFAULT_FIELDS if isinstance(raw.get(k), str)}


# Per-task auxiliary models were removed: card draft, avatar generation and
# field rewrites all use the system default model + reasoning effort. The model
# override surfaces only where the chara itself is configured (wake / chara
# right-side settings), never for these generation helpers.


# ---- named key store (webui-needs #10) -------------------------------------------

def _keys_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = raw.get("keys")
    if not isinstance(keys, dict):
        return {}
    return {str(k): v for k, v in keys.items() if isinstance(v, dict)}


def list_keys() -> list[dict[str, Any]]:
    """Named keys with the secret reduced to its presence — values never travel."""
    raw = _read_desktop_raw()
    active_key = str(raw.get("api_key") or "")
    out = []
    for label, item in sorted(_keys_map(raw).items()):
        secret = str(item.get("api_key") or "")
        out.append({
            "label": label,
            "provider": str(item.get("provider") or ""),
            "base_url": str(item.get("base_url") or ""),
            "model": str(item.get("model") or ""),
            "has_key": bool(secret),
            "active": bool(secret) and secret == active_key,
        })
    return out


def resolve_key(label: str) -> dict[str, str] | None:
    """The full stored record (INCLUDING the secret) for a named key, or None.
    Server-side ONLY — never sent to a client (list_keys is the safe, secret-free
    view). Used by `key.test` to test a specific saved provider key by label."""
    item = _keys_map(_read_desktop_raw()).get(str(label or "").strip())
    if not isinstance(item, dict):
        return None
    return {
        "provider": str(item.get("provider") or ""),
        "base_url": str(item.get("base_url") or ""),
        "api_key": str(item.get("api_key") or ""),
        "model": str(item.get("model") or ""),
    }


def save_key(label: str, provider: str = "", base_url: str = "",
             api_key: str = "", model: str = "") -> list[dict[str, Any]]:
    label = str(label or "").strip()
    if not label:
        raise RpcError(-32602, "keys.save needs a label")
    raw = _read_desktop_raw()
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    cur = keys.get(label) if isinstance(keys.get(label), dict) else {}
    item = dict(cur)
    for field_name, value in (("provider", provider), ("base_url", base_url), ("model", model)):
        if value:
            item[field_name] = str(value)
    if api_key:
        item["api_key"] = str(api_key)  # omitted on update = keep the stored secret
    if not item.get("api_key"):
        raise RpcError(-32602, f"keys.save: '{label}' has no stored api_key — provide one")
    keys[label] = item
    raw["keys"] = keys
    _write_desktop_raw(raw)
    return list_keys()


def delete_key(label: str) -> list[dict[str, Any]]:
    raw = _read_desktop_raw()
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    if label not in keys:
        raise RpcError(-32035, f"no such key: {label}")
    keys.pop(label)
    raw["keys"] = keys
    _write_desktop_raw(raw)
    return list_keys()


def task_defaults(defaults: dict[str, Any], provider_label: str) -> dict[str, Any]:
    """Overlay a saved provider key's route onto `defaults` for an AUXILIARY task
    that runs on its OWN provider — read-image, card draft, image-prompt, and any
    future modality (audio …). `provider_label` is a keyring label; empty (or an
    unusable/keyless entry) → `defaults` unchanged, so the task falls back to the
    main text default. The model id stays whatever the caller passes; only the
    route (provider/base_url/api_key) is swapped. ONE pattern for every modality."""
    label = (provider_label or "").strip()
    if not label:
        return defaults
    from ...session.settings import resolve_named_key

    e = resolve_named_key(label)
    if not (e.get("base_url") and e.get("api_key")):
        return defaults
    return {**defaults, "provider": e.get("provider", ""),
            "base_url": e["base_url"], "api_key": e["api_key"]}


def use_key(label: str) -> dict[str, Any]:
    """Copy a named key into the top-level defaults (= defaults.set fields)."""
    item = _keys_map(_read_desktop_raw()).get(str(label or ""))
    if item is None or not item.get("api_key"):
        raise RpcError(-32035, f"no such key: {label}")
    updates = {k: str(item[k]) for k in ("provider", "base_url", "api_key", "model") if item.get(k)}
    return _public_defaults(save_defaults(updates))


def _key_overrides(label: str) -> dict[str, str]:
    """Resolve a named key for session.wake {key: label}; visible error if absent."""
    item = _keys_map(_read_desktop_raw()).get(str(label or ""))
    if item is None or not item.get("api_key"):
        raise RpcError(-32035, f"no such key: {label}")
    return {k: str(item[k]) for k in ("provider", "base_url", "api_key", "model") if item.get(k)}


def _public_defaults(data: dict[str, str]) -> dict[str, Any]:
    """Defaults with every secret reduced to its presence (never echo secrets)."""
    out: dict[str, Any] = {k: v for k, v in data.items() if k not in _SECRET_FIELDS}
    out["has_key"] = bool(data.get("api_key"))
    # has_image_key = the ACTIVE image provider has a key in the unified keyring
    # (drives the visuals editor's generate affordance + the 提供商 status).
    raw = _read_desktop_raw()
    active_img = image_providers.resolve_provider(str(raw.get("image_provider") or ""))
    out["has_image_key"] = bool(active_img) and image_providers.has_key(raw, active_img)
    return out


def _provider_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _base_url_id(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _config_matches_model_route(cfg: dict[str, Any], defaults: dict[str, str]) -> bool:
    """Same provider/base_url route, ignoring cosmetic case/trailing slashes."""
    return (
        _provider_id(cfg.get("provider")) == _provider_id(defaults.get("provider"))
        and _base_url_id(cfg.get("base_url")) == _base_url_id(defaults.get("base_url"))
    )


def key_update_candidates(defaults: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Obsolete since SEC-2: the provider key is resolved at load from the global
    keyring, never copied into per-session configs — so NO session ever needs a
    per-session key update. Always empty. Kept (with apply_default_key) so the
    board's "update key in N sessions" RPC contract stays intact for older clients."""
    return []


def apply_default_key(names: list[str], defaults: dict[str, str] | None = None) -> dict[str, Any]:
    """No-op since SEC-2: the provider key is no longer copied into per-session
    configs — every chara resolves it at load from the global keyring, so changing
    the default key in Settings applies everywhere with no per-session rewrite. We
    deliberately do NOT write keys into session files anymore. Kept as a stable RPC
    so older clients calling it get a clean empty result instead of an error."""
    return {"updated": [], "skipped": [], "candidates": []}
