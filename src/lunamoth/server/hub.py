"""Desktop hub — roster-level JSON-RPC for the web/desktop renderer.

The hub is the board-level brain of `lunamoth desktop`: it lists charas and
cards, wakes new charas (freezing a card copy), toggles live/idle daemons,
deletes/exports sessions, manages the global model defaults + key testing,
transcribes natural language into card drafts, and reads cross-session files
(works/memory/goals) straight from session directories.

It deliberately NEVER imports core/ or tools/: one process = one activated
session (env-based), so the hub talks to a living chara only through a child
`lunamoth serve <name> --stdio` process (see desktop.py for the proxy). State
the hub reports comes from the documented stable interfaces: session dirs,
`session.json`, `config.json`, the sandbox tree and the transcript SQLite.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..config import ROOT, content_dir
from ..content.cards import CharacterCard, detect_language, looks_like_world_book, merge_world_into_card
from ..content.imaging import CAP_ART, avatar_thumb_data_uri, compress_image_bytes
from ..content.knobs import normalize_embodiment
from ..session import sessions as S
from ..session.settings import PRESETS, Settings
from .dispatch import RpcError, error_response, ok_response, _normalize_request

_log = logging.getLogger("lunamoth.server.hub")

# session isolation level -> python tool execution backend (mirror of front/cli.py)
_ISOLATION_TO_BACKEND = {"sandbox": "sandbox", "admin": "admin"}

# Models with a reputation for prose ("书写 ★"); heuristic, substring match.
_WRITING_STAR = ("claude", "deepseek-v4", "gpt-5", "gemini-2", "kimi", "grok-4", "qwen3-max")

_HTTP_TIMEOUT = 20.0


class HubRpcError(RpcError):
    """Hub-scoped JSON-RPC error that may carry machine-readable error data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None):
        super().__init__(code, message)
        self.data = data


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

# image_api_key/image_model: the GLOBAL image-generation credential + model, set in
# Settings·生图 and read by tools/builtin/_image_gen.py (which reads desktop.json
# directly — tools/ must not import server/). The secret never echoes back (see
# _public_defaults → has_image_key), parallel to the text api_key/has_key.
# matte_model: the active local matting (抠像) model id, set in Settings·生图 and
# read by lunamoth.visuals.matte.selected_model(). Not a secret.
_DEFAULT_FIELDS = ("provider", "base_url", "api_key", "model", "ui_lang", "ui_theme",
                   "image_api_key", "image_model", "matte_model", "reasoning", "vision_model",
                   "card_model", "image_prompt_model", "model_context")
# Default fields whose value is a secret: stripped from every public payload,
# surfaced only as a has_<field> presence flag.
_SECRET_FIELDS = ("api_key", "image_api_key")


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
    out["has_image_key"] = bool(data.get("image_api_key"))
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


def _atomic_write_json(path: Path, data: dict[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if private:
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def apply_default_key(names: list[str], defaults: dict[str, str] | None = None) -> dict[str, Any]:
    """No-op since SEC-2: the provider key is no longer copied into per-session
    configs — every chara resolves it at load from the global keyring, so changing
    the default key in Settings applies everywhere with no per-session rewrite. We
    deliberately do NOT write keys into session files anymore. Kept as a stable RPC
    so older clients calling it get a clean empty result instead of an error."""
    return {"updated": [], "skipped": [], "candidates": []}


# ---- provider HTTP (no core/ import; plain OpenAI-compatible calls) ------------

def _http_json(url: str, api_key: str = "", payload: dict | None = None, timeout: float = _HTTP_TIMEOUT) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


_models_cache: dict[str, tuple[float, list[dict]]] = {}


def _catalogue(base_url: str, api_key: str = "") -> list[dict]:
    """Provider /models catalogue, cached for the hub's lifetime (10 min TTL)."""
    base = base_url.rstrip("/")
    now = time.monotonic()
    hit = _models_cache.get(base)
    if hit and now - hit[0] < 600:
        return hit[1]
    data = _http_json(base + "/models", api_key)
    models = data.get("data") if isinstance(data, dict) else None
    models = models if isinstance(models, list) else []
    _models_cache[base] = (now, models)
    return models


def model_capabilities(base_url: str, model: str, api_key: str = "") -> dict[str, Any]:
    """Capability badges for one model: tools / vision / writing / context.

    OpenRouter's catalogue is authoritative; other providers report unknown
    (null) rather than guessed values."""
    caps: dict[str, Any] = {"tools": None, "vision": None, "context": None,
                            "writing": any(s in model.lower() for s in _WRITING_STAR)}
    try:
        for m in _catalogue(base_url, api_key):
            if m.get("id") == model:
                params = m.get("supported_parameters") or []
                caps["tools"] = "tools" in params
                arch = m.get("architecture") or {}
                caps["vision"] = "image" in (arch.get("input_modalities") or [])
                caps["context"] = m.get("context_length")
                break
    except Exception:  # noqa: BLE001 - capability probing is best-effort
        _log.debug("capability probe failed", exc_info=True)
    return caps


def test_key(provider: str, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """One tiny completion: the only honest connectivity test."""
    base = base_url.rstrip("/")
    try:
        data = _http_json(
            base + "/chat/completions", api_key,
            {"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 4},
            timeout=30.0,
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": _classify_http_error(exc.code, detail)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": {"kind": "network", "detail": str(getattr(exc, "reason", exc))}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"kind": "unknown", "detail": str(exc)}}
    text = ""
    try:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError, TypeError):
        pass
    if not text and isinstance(data, dict) and data.get("error"):
        err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
        return {"ok": False, "error": {"kind": "provider", "detail": str(err.get("message", ""))}}
    return {"ok": True, "model": model, "capabilities": model_capabilities(base, model, api_key)}


def _classify_http_error(code: int, detail: str) -> dict[str, str]:
    """Human-language error classes the UI shows verbatim (design §3.2)."""
    if code in (401, 403):
        return {"kind": "auth", "detail": detail}
    if code == 402 or "credit" in detail.lower() or "balance" in detail.lower():
        return {"kind": "credit", "detail": detail}
    if code == 404:
        return {"kind": "model", "detail": detail}
    if code == 429:
        return {"kind": "ratelimit", "detail": detail}
    return {"kind": "provider", "detail": detail or f"HTTP {code}"}


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or "")
        return raw[:500]
    except Exception:  # noqa: BLE001
        return ""


def _complete(defaults: dict[str, str], system: str, user: str, model: str = "",
              max_tokens: int = 4096, temperature: float = 0.8,
              response_format: dict[str, Any] | None = None) -> str:
    base = (defaults.get("base_url") or "").rstrip("/")
    if not base:
        raise HubRpcError(
            -32030, "no model configured — set up a provider first",
            {"kind": "model", "detail": "missing base_url"},
        )
    payload: dict[str, Any] = {
        "model": model or defaults.get("model", ""),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    try:
        data = _http_json(base + "/chat/completions", defaults.get("api_key", ""), payload, timeout=180.0)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        classified = _classify_http_error(exc.code, detail)
        raise HubRpcError(-32037, classified["detail"] or classified["kind"], classified) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = str(getattr(exc, "reason", exc))
        raise HubRpcError(-32037, detail or "network error", {"kind": "network", "detail": detail}) from exc
    except Exception as exc:  # noqa: BLE001
        raise HubRpcError(-32037, str(exc), {"kind": "unknown", "detail": str(exc)}) from exc
    if isinstance(data, dict) and data.get("error"):
        err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
        raise HubRpcError(-32037, str(err.get("message", "")), {"kind": "provider", "detail": str(err.get("message", ""))})
    try:
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError, TypeError):
        return ""


# ---- AI-assisted card drafts --------------------------------------------------

_CARD_DRAFT_SYSTEM = """You draft editable SillyTavern/LunaMoth character-card material from a user's inspiration.
The human is the author: preserve their ideas, names, relationships, tone, taboos, and wording where possible.
Do not contradict the inspiration. If a detail is missing, choose conservative, editable placeholder-like detail.
Write the persona and all prose in the SAME LANGUAGE as the user's inspiration.

Reply with STRICT JSON ONLY: one object, no markdown, no comments, no trailing prose.
The object must have exactly these keys:
{
  "name": string,
  "user_name": string,
  "description": string,
  "personality": string,
  "scenario": string,
  "first_mes": string,
  "world_entries": [{"keys": [string, ...], "content": string, "constant": boolean}],
  "seed_goals": [string],
  "tagline": string,
  "theme_color": string
}

Requirements:
- user_name: who "you" — the human who will talk to this character — ARE inside this world: a short name or role and your relationship to the character. Use whatever the inspiration says about the reader / "you". If the inspiration does NOT say who you are, do NOT invent a second protagonist: assign a neutral, moderate role that simply fits the world — name it neutrally (e.g. "friend" / "朋友") and make "you" an ordinary person of this world. Never leave it empty.
- description: the character persona, 150-400 words when the language uses spaces; for CJK, a similarly rich 2-5 paragraphs. Convey the character's goals and motivations, not just appearance.
- personality: a concise distillation of the character's temperament and traits (a phrase or a few sentences).
- scenario: the current situation / setting the character is in right now (1-3 sentences).
- first_mes: an opening message in character — the FIRST thing the character says, in their own voice.
- world_entries: up to 4 lorebook entries (0 is fine). keys are short trigger words/names. At most one entry may be constant=true.
- seed_goals: up to 3 short ongoing pursuits (0 is fine).
- tagline: one line.
- theme_color: a hex color like "#5B9FD4".
The avatar is NOT generated here — the human uploads one or generates it on demand later."""

_THEME_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SVG_MAX_CHARS = 1500
_SVG_EVENT_ATTR_RE = re.compile(r"\son[a-zA-Z0-9_.:-]*\s*=")
_SVG_EXTERNAL_REF_RE = re.compile(r"""\b(?:href|xlink:href)\s*=\s*["']\s*(?!#)[^"']+["']|url\(\s*["']?\s*(?!#)[^)]+""",
                                  re.IGNORECASE)
_SVG_SCRIPT_RE = re.compile(r"<\s*/?\s*script(?:\s|>|/)", re.IGNORECASE)
_SVG_FOREIGN_RE = re.compile(r"<\s*/?\s*foreignobject(?:\s|>|/)", re.IGNORECASE)
_SVG_TEXT_RE = re.compile(r"<\s*/?\s*text(?:\s|>|/)", re.IGNORECASE)
_SVG_VIEWBOX_RE = re.compile(r"""\bviewbox\s*=\s*["']0\s+0\s+64\s+64["']""", re.IGNORECASE)


def _invalid_draft(message: str) -> HubRpcError:
    return HubRpcError(-32050, f"the model returned an invalid draft: {message}",
                       {"kind": "draft_schema", "detail": message})


def _sanitize_avatar_svg(value: Any) -> tuple[str, str]:
    """Return (safe_svg, note). Unsafe SVG is dropped, never repaired."""
    if value is None:
        return "", "avatar_svg dropped: missing"
    if not isinstance(value, str):
        return "", "avatar_svg dropped: not a string"
    svg = value.strip()
    low = svg.lower()
    if not svg:
        return "", "avatar_svg dropped: empty"
    if len(svg) > _SVG_MAX_CHARS:
        return "", "avatar_svg dropped: over 1500 characters"
    if not low.startswith("<svg"):
        return "", "avatar_svg dropped: it does not start with <svg"
    if not _SVG_VIEWBOX_RE.search(svg):
        return "", "avatar_svg dropped: missing viewBox 0 0 64 64"
    if _SVG_SCRIPT_RE.search(svg):
        return "", "avatar_svg dropped: script element"
    if _SVG_FOREIGN_RE.search(svg):
        return "", "avatar_svg dropped: foreignObject element"
    if _SVG_TEXT_RE.search(svg):
        return "", "avatar_svg dropped: text element"
    if _SVG_EVENT_ATTR_RE.search(svg):
        return "", "avatar_svg dropped: event handler attribute"
    if _SVG_EXTERNAL_REF_RE.search(svg):
        return "", "avatar_svg dropped: external reference"
    return svg, ""


def _theme_color(value: Any) -> str:
    if not isinstance(value, str) or not _THEME_RE.match(value.strip()):
        raise _invalid_draft("theme_color must be a #RRGGBB hex color")
    return value.strip().upper()


def _clean_theme_color(value: Any) -> str:
    if isinstance(value, str) and _THEME_RE.match(value.strip()):
        return value.strip().upper()
    return ""


def _clean_theme(value: Any, legacy: Any = None) -> dict[str, str]:
    """Normalize the dual theme `{primary, secondary}`; back-compat with the
    legacy single `theme_color`. Returns only the keys that have a valid color
    (an empty dict when nothing is set)."""
    primary = ""
    secondary = ""
    if isinstance(value, dict):
        primary = _clean_theme_color(value.get("primary"))
        secondary = _clean_theme_color(value.get("secondary"))
    if not primary:
        primary = _clean_theme_color(legacy)
    out: dict[str, str] = {}
    if primary:
        out["primary"] = primary
    if secondary:
        out["secondary"] = secondary
    return out


def _string_field(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid_draft(f"{key} must be a non-empty string")
    return value.strip()


def _validate_world_entries(value: Any) -> list[dict[str, Any]]:
    """Lenient: keep the well-formed entries (cap 4, at most one constant) and skip
    the rest. An empty or odd-sized list is fine — a card may simply have little
    world. Generation must NOT fail because the model returned the wrong count."""
    out: list[dict[str, Any]] = []
    constants = 0
    if not isinstance(value, list):
        return out
    for entry in value:
        if len(out) >= 4 or not isinstance(entry, dict):
            continue
        keys = entry.get("keys")
        clean_keys = [str(k).strip() for k in keys if isinstance(k, str) and str(k).strip()] if isinstance(keys, list) else []
        content = entry.get("content")
        if not clean_keys or not isinstance(content, str) or not content.strip():
            continue
        constant = bool(entry.get("constant")) and constants == 0
        constants += 1 if constant else 0
        out.append({"keys": clean_keys[:6], "content": content.strip(), "constant": constant})
    return out


def _validate_seed_goals(value: Any) -> list[str]:
    """Lenient: keep up to 3 non-empty goals; an empty list is fine."""
    if not isinstance(value, list):
        return []
    return [str(g).strip() for g in value if isinstance(g, str) and str(g).strip()][:3]


# Who "you" are in the world, when the model leaves it blank: a neutral, moderate
# ordinary-person role in the card's language (never empty — the operator name is
# fixed at wake and must always resolve to something).
_DEFAULT_USER_BY_LANG = {"zh": "朋友", "en": "friend"}


def _validate_user_name(value: Any, lang: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _DEFAULT_USER_BY_LANG.get(lang, "friend")


def _parse_card_draft(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise HubRpcError(
            -32050,
            f"the model did not return strict JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})",
            {"kind": "draft_json", "detail": str(exc)},
        ) from exc
    if not isinstance(obj, dict):
        raise _invalid_draft("top-level JSON must be an object")
    # Tolerant schema: the essentials must be present (else it's not a card draft),
    # extra keys are rejected (a wholly-wrong/parallel schema), but the rest may be
    # absent and are defaulted — generation should not fail on a small deviation.
    required = {"name", "description"}
    allowed = required | {"user_name", "personality", "scenario", "first_mes",
                          "world_entries", "seed_goals", "tagline", "theme_color"}
    got = set(obj)
    missing = required - got
    extra = got - allowed
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            parts.append(f"unexpected: {', '.join(sorted(extra))}")
        raise _invalid_draft("draft keys must match the requested schema (" + "; ".join(parts) + ")")
    name = _string_field(obj, "name")
    description = _string_field(obj, "description")
    opt = lambda k: str(obj.get(k) or "").strip()  # noqa: E731 — soft string field
    # The card's language drives the neutral user_name fallback (朋友 / friend).
    lang = detect_language(text=f"{description} {name}")
    draft = {
        "name": name,
        "user_name": _validate_user_name(obj.get("user_name"), lang),
        "description": description,
        "personality": opt("personality"),
        "scenario": opt("scenario"),
        "first_mes": opt("first_mes"),
        "world_entries": _validate_world_entries(obj.get("world_entries")),
        "seed_goals": _validate_seed_goals(obj.get("seed_goals")),
        "tagline": opt("tagline"),
        "theme_color": _theme_color(obj.get("theme_color")),
        "embodiment": "literal",
    }
    # No avatar is drafted — it's a manual upload/generate step (stored as a sidecar).
    return draft


def draft_card_from_inspiration(defaults: dict[str, str], inspiration: str, model: str = "") -> dict[str, Any]:
    text = inspiration.strip()
    if not text:
        raise RpcError(-32602, "cards.draft needs inspiration")
    raw = _complete(
        defaults,
        _CARD_DRAFT_SYSTEM,
        text,
        model=model,
        max_tokens=4096,
        temperature=0.75,
        response_format={"type": "json_object"},
    )
    if not raw.strip():
        raise HubRpcError(-32050, "the model returned an empty draft", {"kind": "draft_json", "detail": "empty response"})
    return _parse_card_draft(raw)


# ---- per-field AI edit (natural-language rewrite of ONE card field) -------------

_FIELD_REWRITE_SYSTEM = (
    "You are editing ONE field of a SillyTavern/LunaMoth character card. Rewrite just "
    "that field. Keep the SAME language as the current text. Preserve the character's "
    "established name, voice, world and facts unless the instruction says otherwise. "
    "Return ONLY the new field text — no quotes, no markdown, no labels, no commentary."
)

# Human-readable shape hint per field, so the model returns the right kind of text.
_FIELD_REWRITE_LABEL = {
    "name": "the character's name (a short name)",
    "description": "the character persona/description (rich prose)",
    "personality": "the character's personality (concise traits)",
    "scenario": "the scene/setting the character is in",
    "first_mes": "the character's opening message, in their own voice",
    "tagline": "a one-line tagline",
    "user_name": "who YOU (the human) are in this world — a short name or role",
    "user_persona": "a short description of who YOU (the human) are to the character",
    "goals": "the character's seed goals, one short goal per line",
    "world_entries": "world-book lorebook entries, one per line as 'key1, key2 — content'",
}


def rewrite_card_field(defaults: dict[str, str], field: str, value: str = "",
                       instruction: str = "", context: str = "", model: str = "") -> dict[str, Any]:
    """Rewrite ONE card field with the LLM. Empty instruction = free rephrase of the
    current value; a non-empty instruction steers the change. Returns {field, text}.
    No fallback: a failed/empty model call surfaces as a visible error."""
    field = (field or "").strip()
    if not field:
        raise RpcError(-32602, "card.rewrite_field needs a field")
    value = value if isinstance(value, str) else ""
    label = _FIELD_REWRITE_LABEL.get(field, f"the '{field}' field")
    directive = (instruction or "").strip() or (
        "Rephrase it freely — keep the meaning and language, but improve the wording and flavor."
    )
    parts = [f"Field: {label}"]
    if (context or "").strip():
        parts.append(f"\nCharacter context (do not rewrite this, just for consistency):\n{context.strip()}")
    parts.append(f"\nCurrent text:\n{value.strip() or '(empty)'}")
    parts.append(f"\nInstruction: {directive}")
    raw = _complete(defaults, _FIELD_REWRITE_SYSTEM, "\n".join(parts),
                    model=model, max_tokens=2048, temperature=0.9)
    text = _strip_text_fence(raw).strip()
    if not text:
        raise HubRpcError(-32050, "the model returned an empty rewrite",
                          {"kind": "rewrite", "detail": "empty response"})
    return {"field": field, "text": text}


def _strip_text_fence(raw: str) -> str:
    """Drop a ```...``` fence the model may wrap text in, despite instructions."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


# ---- cards ---------------------------------------------------------------------

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


def _writable_card_path(path: str) -> Path:
    """A JSON card path we may edit: a user-deck card OR a chara's own frozen
    session card (so the in-chat Visuals editor can change the LIVING chara's
    art). Both are traversal-confined to their root; anything else is refused."""
    p = Path(str(path or ""))
    if not p.is_file():
        raise RpcError(-32035, f"no such card: {path}")
    if p.suffix.lower() != ".json":
        raise RpcError(-32031, "avatar editing needs a JSON card (PNG cards are read-only here)")
    rp = p.resolve()
    if user_cards_dir().resolve() in rp.parents:
        return p
    # A frozen session card lives at <sessions>/<name>/card.json (exactly one
    # level deep). Sidecars the asset RPCs write land beside it, inside the
    # session dir — confined. This is what lets the chat Visuals tab edit the
    # active chara's own card.
    sessions = S.sessions_dir().resolve()
    if rp.name == "card.json" and rp.parent.parent == sessions:
        return p
    raise RpcError(-32031, "only a deck card or a chara's own session card can be edited")


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


def _asset_url(p: Path | None) -> str | None:
    """A same-origin URL the static server resolves to an art-asset sidecar.

    The avatar stays an inline data-URI (tiny); the heavier art (sprite /
    background / keyvisual / stickers) rides cacheable URLs so list_cards (sent
    in every hub.state) doesn't carry megabytes of base64. Served by the
    /asset route in supervisor.WebHandler, which confines reads to the card &
    session dirs."""
    if p is None:
        return None
    return "/asset?p=" + urllib.parse.quote(str(p))


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


def asset_delete(path: str, kind: str) -> dict[str, Any]:
    """Remove an art asset (avatar / sprite / background / keyvisual): delete its
    sidecar file(s) and drop the card's pointer. Idempotent."""
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
    else:
        raise RpcError(-32602, f"unknown asset kind: {kind}")
    target.write_text(json.dumps(raw_card, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target), "kind": kind, "removed": removed}


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
    for kind in ("sprite", "background", "keyvisual"):
        v = a.get(kind)
        if isinstance(v, str):
            rels.append(v)
    stk = a.get("stickers")
    if isinstance(stk, list):
        rels += [s for s in stk if isinstance(s, str)]
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


def _iter_card_files(base: Path):
    """Card files under a deck dir: per-character folders (`<Name>/card*.json|png`)
    plus legacy flat files (`*.json|png`) for back-compat. Skips hidden/LICENSE."""
    for p in sorted(base.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            for c in sorted(p.glob("card*.json")) + sorted(p.glob("card*.png")):
                yield c
        elif p.suffix.lower() in (".json", ".png") and not p.stem.startswith("LICENSE"):
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
    embodiment = ""
    theme = card.theme_colors()
    avatar_uri = _avatar_thumb_uri(path, card)
    if isinstance(ext, dict):
        theme_color = theme.get("primary", "")
        avatar_svg = _sanitize_avatar_svg(ext.get("avatar_svg"))[0]
        tagline = str(ext.get("tagline") or "")
        embodiment = str(ext.get("embodiment") or "")
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
        "embodiment": embodiment if embodiment in ("literal", "actor") else "",
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


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str, fallback: str = "chara") -> str:
    s = _SLUG_RE.sub("-", name).strip("-._")
    if not s or not S.valid_name(s):
        s = fallback
    return s[:48]


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
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
    if lunamoth.get("embodiment") not in ("literal", "actor"):
        lunamoth["embodiment"] = "literal"


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
    if safe.get("embodiment") not in ("literal", "actor"):
        safe["embodiment"] = ""
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
        from ..content.cards import _card_json_from_png

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
    return save_card(card)  # user-deck write + sanitization + unique filename


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


# ---- sessions / charas -----------------------------------------------------------

def _read_config(meta: S.SessionMeta) -> dict[str, Any]:
    try:
        return json.loads(meta.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _transcript_preview(meta: S.SessionMeta) -> dict[str, Any] | None:
    """Last conversational line, read-only, straight from the transcript DB.

    Returns {role, text, ts, awaiting} where awaiting=True means the last chat
    line is the chara's (it spoke and nobody answered — '等你回话')."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT role, content, ts FROM messages "
                "WHERE kind='chat' AND role IN ('user','assistant') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    role, content, ts = row
    text = " ".join(str(content).split())
    return {"role": role, "text": text[:160], "ts": ts, "awaiting": role == "assistant"}


def _speak_texts_from_struct(content: str) -> list[str]:
    try:
        msg = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(msg, dict):
        return []
    out: list[str] = []
    calls = msg.get("tool_calls")
    if not isinstance(calls, list):
        return out
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict) or fn.get("name") != "speak":
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                continue
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            continue
        if not isinstance(args, dict):
            continue
        raw_text = args.get("text")
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if text:
            out.append(" ".join(text.split())[:240])
    return out


def _transcript_speaks(meta: S.SessionMeta, limit: int = 3) -> list[dict[str, Any]]:
    """Newest speak-tool utterances for the board Super Chat feed."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT content, ts FROM messages "
                "WHERE kind='struct' AND role='assistant' AND content LIKE '%speak%' "
                "ORDER BY id DESC LIMIT 80"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for content, ts in rows:
        for text in reversed(_speak_texts_from_struct(str(content))):
            out.append({"text": text, "ts": float(ts or 0.0)})
            if len(out) >= limit:
                return out
    return out


def _last_error(meta: S.SessionMeta) -> str:
    """Most recent line of the chara's error log, if it is fresh (< 10 min)."""
    err = meta.sandbox_dir / "logs" / "errors.log"
    try:
        if time.time() - err.stat().st_mtime > 600:
            return ""
        lines = [ln for ln in err.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return lines[-1][:200] if lines else ""
    except OSError:
        return ""


_HTTP_CODE_RE = re.compile(r"\bHTTP\s+(401|402|403|404|408|429|500|502|503|504|520|522|524)\b", re.IGNORECASE)


def board_error_kind(line: str) -> str:
    """Classify a recent errors.log line for the desktop board chip."""
    low = line.lower()
    m = _HTTP_CODE_RE.search(line)
    code = int(m.group(1)) if m else 0
    if code in (401, 403) or "auth" in low or "invalid key" in low or "unauthorized" in low:
        return "auth"
    if code == 402 or "credit" in low or "balance" in low:
        return "credit"
    if code == 404 or "model not found" in low:
        return "model"
    if code == 429 or "rate limit" in low or "ratelimit" in low:
        return "ratelimit"
    if any(s in low for s in ("timeout", "connect", "network", "unreachable", "connection failed")):
        return "network"
    return "provider" if line else ""


def _superchat_path(meta: S.SessionMeta) -> Path:
    return meta.root / "superchat.json"


def superchat_read_ts(meta: S.SessionMeta) -> float:
    try:
        data = json.loads(_superchat_path(meta).read_text(encoding="utf-8"))
        return float(data.get("read_ts") or 0.0) if isinstance(data, dict) else 0.0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def set_superchat_read(meta: S.SessionMeta, ts: float) -> dict[str, Any]:
    cur = superchat_read_ts(meta)
    want = max(cur, float(ts or 0.0))
    path = _superchat_path(meta)
    _atomic_write_json(path, {"read_ts": want}, private=True)
    return {"read_ts": want, "superchat_unread": superchat_unread(meta)}


def superchat_unread(meta: S.SessionMeta) -> int:
    read_ts = superchat_read_ts(meta)
    return sum(1 for sp in _transcript_speaks(meta, limit=1000) if float(sp.get("ts") or 0.0) > read_ts)


# ---- messaging gateway config (masked secrets) -----------------------------------

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
    from ..messaging.weixin import DEFAULT_BOT_TYPE, WeixinAPI, qr_fallback_url

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
    from ..messaging.weixin import WeixinAPI, save_login_state

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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        adapters = data.get("adapters") if isinstance(data, dict) else None
        if isinstance(adapters, dict):
            platform = ",".join(sorted(str(k) for k in adapters))
    except (OSError, json.JSONDecodeError):
        pass
    # No live supervisor: a gateway is always "stopped" on disk. Carry the
    # error_message field so the shape matches GatewayChild.status() for the web.
    return {"platform": platform, "state": "stopped", "detail": "", "error_message": "", "pid": 0}


def _await_supervisor(supervisor: Any, coro):
    # Hub handlers run in worker threads; submit coroutines back to the
    # supervisor's event loop and wait for the JSON-RPC result.
    import asyncio

    loop = getattr(supervisor, "loop", None)
    if loop is None:
        # Unit-test/fake supervisor path.
        return asyncio.run(coro)
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=60.0)


def session_entry(meta: S.SessionMeta, supervisor: Any | None = None) -> dict[str, Any]:
    cfg = _read_config(meta)
    last_error = _last_error(meta)
    char_path = (cfg.get("character_path") or "").strip()
    char_name, lang = meta.name, "zh"
    if char_path:
        try:
            card = CharacterCard.load(char_path)
            char_name, lang = card.name or Path(char_path).stem, card.language
        except Exception:  # noqa: BLE001
            char_name = Path(char_path).stem
    life = supervisor.life_state(meta.name) if supervisor is not None else None
    gateway = supervisor.gateway_status(meta.name) if supervisor is not None else _gateway_status_from_disk(meta)
    child_status = supervisor.chara_status(meta.name) if supervisor is not None else None
    status = meta.status()
    error = last_error
    if isinstance(child_status, dict) and child_status.get("state") == "crashed":
        status = "crashed"
        error = str(child_status.get("detail") or "crashed")
    # Autonomy is the chara's persisted `mode` (live = autonomous, chat = plain
    # chat agent) — the ONE switch the board and the in-chat panel both flip.
    # `paused` = autonomy off; the board shows it even while the child is up.
    paused = str(cfg.get("mode") or "live") != "live"
    if paused and status != "crashed":
        status = "paused"
    return {
        "name": meta.name,
        "char_name": char_name,
        "lang": lang,
        "status": status,
        "paused": paused,
        "chara": child_status,
        "isolation": meta.isolation,
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "live"),
        "created_at": meta.created_at,
        "last_active": meta.last_active or meta.created_at,
        "preview": _transcript_preview(meta),
        "speaks": _transcript_speaks(meta),
        "life": life,
        "gateway": gateway,
        "superchat_unread": superchat_unread(meta),
        "error": error,
        "error_kind": board_error_kind(error),
    }


def wake(card_path: str, name: str = "", isolation: str = "sandbox",
         model: str = "", toolpack: str = "", embodiment: str = "",
         key: str = "", card_data: "dict[str, Any] | None" = None) -> dict[str, Any]:
    """Instantiate a card: create the session, freeze a card copy, write config.

    The card describes WHO the chara is; this call decides where it lives
    (isolation), what it thinks with (model) and — once, at wake — how tools
    relate to its fiction (embodiment). Embodiment is a wake-time choice, never
    hot-swapped: identity-layer switches would rebuild the stable prefix and
    destroy the prompt cache. The frozen copy means later edits to the deck
    never drift a living chara's persona."""
    stance = ""
    if embodiment:
        stance = normalize_embodiment(embodiment)
        if not stance:
            raise RpcError(-32602, f"invalid embodiment {embodiment!r} — expected literal|actor")
    card = CharacterCard.load(card_path)  # validates before any disk writes
    defaults = load_defaults()
    if key:
        # A named key (webui-needs #10): its provider/base_url/api_key drive
        # this chara; its model fills in only when wake didn't pick one.
        defaults = {**defaults, **_key_overrides(key)}  # wake's `model` param still wins below
    if not (defaults.get("base_url") and defaults.get("api_key")) and defaults.get("provider") != "mock":
        raise RpcError(-32030, "no model configured — set up a provider first")
    session_name = _slug(name or Path(card_path).stem)
    base = session_name
    n = 2
    while S.load_session(session_name) is not None:
        session_name = f"{base}-{n}"
        n += 1
    iso = S.normalize_isolation(isolation)  # legacy dir/local/docker → admin
    meta = S.create_session(session_name, isolation=iso if iso in S.ISOLATION_LEVELS else "sandbox")

    frozen = meta.root / "card.json"
    src = Path(card_path)
    if card_data is not None:
        # Wake-time edits: freeze the EDITED card as this chara's own card; the
        # source template is never mutated (it stays unlocked and re-wakeable).
        if not isinstance(card_data, dict) or not isinstance(card_data.get("data"), dict):
            raise RpcError(-32602, "card_data must be a {data:{...}} card object")
        edited = dict(card_data)
        edited.setdefault("version", "1.0")  # our own card format (no ST spec markers)
        # ROOT FIX (data-loss): merge the edit ONTO the freshly-loaded SOURCE card
        # so a blank/partial submission from the wake editor can never freeze a
        # persona-less, greeting-less chara. The editor renders no field for
        # mes_example / system_prompt / post_history_instructions, and a card.read
        # hiccup blanks the rest — without this merge those overwrite the source
        # with "". An empty edited field keeps the source value; a real edit wins.
        from ..content.cards import _card_json_from_png as _png_json
        try:
            src_dict = _png_json(src) if src.suffix.lower() == ".png" else json.loads(src.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — unreadable source → freeze the edit as-is
            src_dict = {}
        if isinstance(src_dict, dict) and isinstance(src_dict.get("data"), dict):
            edited["data"] = _merge_preserving(src_dict["data"], edited.get("data") or {})
            if not str(edited.get("name") or "").strip() and src_dict.get("name"):
                edited["name"] = src_dict["name"]
        _sanitize_card_extensions(edited)
        frozen.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
    elif src.suffix.lower() == ".png":
        # PNG cards keep their embedded payload; copy byte-for-byte.
        frozen = meta.root / "card.png"
        shutil.copyfile(src, frozen)
    else:
        shutil.copyfile(src, frozen)
    (meta.root / "card_source").write_text(str(src), encoding="utf-8")
    # Freeze a copy of the art-asset library beside the frozen card, so a living
    # chara owns its own visuals (the deck/chat resolve them from the session dir,
    # not the deck template that may later change or be deleted). Use the FROZEN
    # card's declarations (it may have been edited) but read the files from the
    # source template folder where they actually live.
    asset_decl = card
    if card_data is not None and src.suffix.lower() != ".png":
        try:
            asset_decl = CharacterCard.load(frozen)
        except Exception:  # noqa: BLE001 - fall back to the template's declarations
            asset_decl = card
    _copy_card_assets(asset_decl, meta.root, src_base=Path(card_path).parent)

    card_defaults = card.defaults() if hasattr(card, "defaults") else {}
    cfg = dataclasses.asdict(Settings())
    cfg.update({
        "provider": defaults.get("provider", "openrouter"),
        "base_url": defaults.get("base_url", ""),
        # SEC-2: do NOT copy the api_key into the session config — it's resolved at
        # load from the global keyring (settings.global_api_key). Sessions hold only
        # non-secret overrides, so the key isn't duplicated into every chara's dir.
        "model": model or defaults.get("model", cfg["model"]),
        "model_context": int(defaults.get("model_context") or 0),
        "character_path": str(frozen),
        "py_backend": _ISOLATION_TO_BACKEND.get(meta.isolation, "sandbox"),
    })
    cfg.pop("api_key", None)
    if toolpack:
        cfg["toolpack"] = toolpack
    elif isinstance(card_defaults, dict) and card_defaults.get("toolpack"):
        cfg["toolpack"] = str(card_defaults["toolpack"])
    if stance:
        # Operator's wake-time choice persists as the override; absent, the
        # resolution chain stays card declaration > literal.
        cfg["embodiment_override"] = stance
    meta.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        meta.config_path.chmod(0o600)
    except OSError:
        pass
    return session_entry(meta)


def start_daemon(meta: S.SessionMeta, patience: float | None = None) -> bool:
    """Spawn the detached background life (mirror of front/cli._start_daemon)."""
    if meta.daemon_pid():
        return True
    if not meta.is_configured():
        return False
    env = {**os.environ, **meta.env()}
    env.setdefault("LUNAMOTH_PY_BACKEND", _ISOLATION_TO_BACKEND.get(meta.isolation, "sandbox"))
    log = meta.daemon_log.open("ab")
    argv = [sys.executable, "-m", "lunamoth.front.terminal"]
    if patience is not None:
        argv += ["--patience", str(patience)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=env, cwd=str(ROOT),
    )
    meta.daemon_pid_path.write_text(str(proc.pid), encoding="utf-8")
    meta.last_active = time.time()
    meta.save()
    return True


def stop_daemon(meta: S.SessionMeta) -> bool:
    import signal

    pid = meta.daemon_pid()
    if not pid:
        meta.daemon_pid_path.unlink(missing_ok=True)
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    meta.daemon_pid_path.unlink(missing_ok=True)
    return True


def _transcript_export_jsonl(meta: S.SessionMeta) -> str:
    """Hermes-style complete conversation export of the CURRENT epoch, read
    straight from the session's transcript DB (read-only — works while the
    chara is stopped). Every row (chat/think/struct/tool/summary) becomes one
    JSON line, oldest first; struct/tool rows expanded back to their full
    message dict. The hub reads the DB directly (never imports core/)."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return ""
    try:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            epoch = int(row[0]) if row and row[0] else 0
        except (sqlite3.Error, ValueError):
            epoch = 0
        try:
            rows = conn.execute(
                "SELECT id, ts, role, content, kind FROM messages WHERE epoch=? ORDER BY id",
                (epoch,),
            ).fetchall()
        except sqlite3.Error:
            return ""
    finally:
        conn.close()
    out_lines: list[str] = []
    for row_id, ts, role, content, kind in rows:
        obj: dict[str, Any] = {"id": int(row_id), "ts": float(ts or 0.0),
                               "role": str(role), "kind": str(kind)}
        if kind in ("struct", "tool"):
            try:
                msg = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                msg = None
            if isinstance(msg, dict):
                for k, v in msg.items():
                    obj.setdefault(k, v)
                obj["role"] = str(msg.get("role") or role)
            else:
                obj["content"] = str(content)
        else:
            obj["content"] = str(content)
        out_lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def export_session(meta: S.SessionMeta) -> dict[str, Any]:
    """Zip the whole session dir, AND emit the complete conversation as JSONL.

    The zip stays the raw forensic bundle (sandbox + transcript + memory +
    config). Alongside it, for debugging like hermes's export, we write:
      <name>-conversation.jsonl — every transcript row of the current epoch
        (prompts/tool calls/results/reasoning), oldest first;
      <name>-requests.jsonl — a copy of sandbox/logs/requests.jsonl (the
        faithful per-turn request log) when it exists.
    Both are placed inside the zip AND as standalone files next to it. The zip
    path is still the primary return value."""
    downloads = Path.home() / "Downloads"
    target_dir = downloads if downloads.is_dir() else Path.home()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"lunamoth-{meta.name}-{stamp}.zip"

    conversation = _transcript_export_jsonl(meta)
    conv_path = target_dir / f"lunamoth-{meta.name}-{stamp}-conversation.jsonl"
    conv_path.write_text(conversation, encoding="utf-8")

    requests_src = meta.sandbox_dir / "logs" / "requests.jsonl"
    requests_path: Path | None = None
    requests_text = ""
    if requests_src.exists():
        try:
            requests_text = requests_src.read_text(encoding="utf-8")
        except OSError:
            requests_text = ""
        requests_path = target_dir / f"lunamoth-{meta.name}-{stamp}-requests.jsonl"
        requests_path.write_text(requests_text, encoding="utf-8")

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(meta.root.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(meta.root.parent))
        zf.writestr(f"{meta.name}-conversation.jsonl", conversation)
        if requests_path is not None:
            zf.writestr(f"{meta.name}-requests.jsonl", requests_text)

    result: dict[str, Any] = {"path": str(target), "conversation": str(conv_path)}
    if requests_path is not None:
        result["requests"] = str(requests_path)
    return result


def list_toolpacks() -> list[dict[str, Any]]:
    """Bundled tool packs for the wake sheet's picker (webui-needs #8/#12).

    Pure data read of toolpacks/*.json — the server never imports tools/."""
    base = content_dir("toolpacks")
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log.warning("unreadable toolpack skipped: %s", p)
            continue
        out.append({
            "name": str(d.get("name") or p.stem),
            "description": str(d.get("description") or ""),
            "tools": [str(t) for t in (d.get("tools") or [])],
            "mcp_servers": [str(x) for x in (d.get("mcp_servers") or [])],
            "path": str(p),
        })
    return out


# ---- sandbox reads for the drawer ------------------------------------------------

# assets/ is the card's staged reference art (roleplay visuals), not the chara's
# own work — exclude it from the works listing just like skills/ know-how.
_WORK_SKIP_DIRS = {"logs", "memory", "skills", "assets", "__pycache__", ".git", "node_modules"}
_KIND_BY_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
    ".html": "web", ".htm": "web",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio", ".mid": "audio",
    ".md": "text", ".txt": "text",
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code", ".json": "code", ".css": "code",
}


def list_works(meta: S.SessionMeta, limit: int = 200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # The Works tab is the chara's shareable SHELF: workspace/works/ ONLY. The
    # rest of the workspace is the chara's private working area and is NOT
    # surfaced here; assets/ is a read-only reference sibling (also not "works").
    # _WORK_SKIP_DIRS still drops stray logs/skills/etc if they appear under works/.
    base = meta.sandbox_dir / "workspace" / "works"
    if base.is_dir():
        for p in base.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            # Judge only the path UNDER the works tree: the sandbox itself may
            # live below a dot-dir (~/.lunamoth/...), which must not hide it.
            if any(part in _WORK_SKIP_DIRS or part.startswith(".") for part in p.relative_to(base).parts):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "rel": str(p.relative_to(meta.sandbox_dir)),
                "path": str(p),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "kind": _KIND_BY_EXT.get(p.suffix.lower(), "file"),
            })
    out.sort(key=lambda w: w["mtime"], reverse=True)
    return out[:limit]


_WORK_READ_CAP = 512 * 1024
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}
_TEXT_READ_EXTS = {
    ".md", ".txt", ".py", ".js", ".ts", ".sh", ".json", ".css", ".html", ".htm",
    ".csv", ".yml", ".yaml", ".toml", ".log",
}


def read_work(meta: S.SessionMeta, rel: str) -> dict[str, Any]:
    """In-app preview of one sandbox work (the deck's works page).

    `rel` comes from works.list and must stay inside the sandbox's workspace/
    tree (or the read-only assets/ sibling, so the same preview can render a
    reference asset) — anything else is refused (no traversal). Over-cap files
    return truncated so the UI can offer works.open instead.
    """
    if not rel:
        raise RpcError(-32602, "works.read needs rel")
    sandbox = meta.sandbox_dir.resolve()
    target = (sandbox / rel).resolve()
    workspace = (sandbox / "workspace").resolve()
    assets = (sandbox / "assets").resolve()
    under_ws = workspace == target or workspace in target.parents
    under_assets = assets == target or assets in target.parents
    if not (under_ws or under_assets):
        raise RpcError(-32031, "works.read only serves files under workspace/ or assets/")
    if not target.is_file():
        raise RpcError(-32035, f"no such work: {rel}")
    size = target.stat().st_size
    suffix = target.suffix.lower()
    if suffix in _IMAGE_MIME:
        if size > _WORK_READ_CAP:
            return {"kind": "image", "size": size, "truncated": True}
        data = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"kind": "image", "size": size, "truncated": False,
                "data_uri": f"data:{_IMAGE_MIME[suffix]};base64,{data}"}
    if suffix in _TEXT_READ_EXTS:
        raw = target.read_bytes()
        return {"kind": "text", "size": size, "truncated": len(raw) > _WORK_READ_CAP,
                "content": raw[:_WORK_READ_CAP].decode("utf-8", errors="replace")}
    return {"kind": "binary", "size": size, "truncated": size > _WORK_READ_CAP}


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


def _read_optional(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def chara_extras(meta: S.SessionMeta) -> dict[str, Any]:
    """Drawer data the hub can read without a living process."""
    sandbox = meta.sandbox_dir
    goals: Any = None
    raw_goals = _read_optional(sandbox / "goals.json")
    if raw_goals:
        try:
            goals = json.loads(raw_goals)
        except json.JSONDecodeError:
            goals = None
    return {
        "memory": _read_optional(sandbox / "memory" / "memory.md"),
        "user_memory": _read_optional(sandbox / "memory" / "user.md"),
        "goals": goals,
        "sandbox_root": str(sandbox),
        "workspace_root": str(sandbox / "workspace"),
    }


def open_path(path: str, reveal: bool = False) -> dict[str, Any]:
    """Hand a file to the OS (design: we present existence, the system opens it)."""
    p = Path(path)
    home = S.lunamoth_home()
    if not p.exists():
        raise RpcError(-32040, "file not found")
    allowed = home in p.parents or p == home or (Path.home() / "Downloads") in p.parents
    if not allowed:
        raise RpcError(-32041, "path is outside the LunaMoth home")
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(p)] if reveal else ["open", str(p)]
    else:
        cmd = ["xdg-open", str(p.parent if reveal else p)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ---- natural language -> card draft ----------------------------------------------

_TRANSCRIBE_SYSTEM = """You turn a person's free-form description of an original character (OC) \
into a structured character card. Write in the SAME LANGUAGE as the user's text. Preserve their \
ideas and wording where possible — you are a careful editor, not a co-author. Fill gaps \
conservatively and tastefully; never invent contradictions. Reply with ONLY a JSON object, \
no markdown fence, with exactly these keys:
{"name": str, "appearance": str, "personality": str, "scenario": str, "first_mes": str,
 "alternate_greetings": [str], "world": [{"key": str, "desc": str, "constant": bool}],
 "relationship": str, "goals": [str], "rules": str, "toolpack_hint": str}
- appearance: who they are + how they look, 2-4 sentences, prose.
- personality: temperament and voice, 2-4 sentences, prose.
- first_mes: their in-character opening line when meeting the user.
- world: 2-5 lorebook entries (key = a name/term, desc = one sentence); constant=true for at most one core entry.
- relationship: the user's place in this character's life, 1-2 sentences.
- goals: 1-3 ongoing pursuits, short phrases.
- rules: boundaries/never-dos if implied, else "".
- toolpack_hint: "sandbox" if this character would plausibly make things (art/code/writing), else ""."""


def transcribe_card(defaults: dict[str, str], text: str, model: str = "") -> dict[str, Any]:
    raw = _complete(defaults, _TRANSCRIBE_SYSTEM, text.strip(), model=model)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RpcError(-32050, f"the model did not return a usable draft ({exc})") from exc
    if not isinstance(draft, dict) or not draft.get("name"):
        raise RpcError(-32050, "the model did not return a usable draft")
    return draft


def _draft_world_entries(draft: dict[str, Any]) -> list[dict[str, Any]]:
    source = draft.get("world_entries") if isinstance(draft.get("world_entries"), list) else draft.get("world")
    out: list[dict[str, Any]] = []
    for i, w in enumerate(source or []):
        if not isinstance(w, dict):
            continue
        raw_keys = w.get("keys")
        if isinstance(raw_keys, list):
            keys = [str(k).strip() for k in raw_keys if str(k).strip()]
        else:
            key = str(w.get("key") or "").strip()
            keys = [key] if key else []
        content = str(w.get("content") if "content" in w else w.get("desc", "")).strip()
        if not keys or not content:
            continue
        out.append({
            "id": i,
            "keys": keys[:6],
            "content": content,
            "constant": bool(w.get("constant")),
            "enabled": True,
            "insertion_order": i,
        })
    return out


def _draft_goals(draft: dict[str, Any]) -> list[str]:
    goals = draft.get("seed_goals") if isinstance(draft.get("seed_goals"), list) else draft.get("goals")
    if not isinstance(goals, list):
        return []
    return [str(g).strip() for g in goals if str(g).strip()][:5]


def draft_to_card(draft: dict[str, Any], origin_text: str = "", as_draft: bool = False) -> dict[str, Any]:
    """Assemble a V3 card object from a (possibly user-edited) draft."""
    world_entries = _draft_world_entries(draft)
    ext: dict[str, Any] = {"origin": origin_text[:8000], "embodiment": "literal"}
    if as_draft:
        ext["draft"] = True
    wishes = _draft_goals(draft)
    if wishes:
        ext["wishes"] = wishes
    if draft.get("rules"):
        ext["rules"] = str(draft["rules"])
    if draft.get("toolpack_hint"):
        ext["toolpack"] = str(draft["toolpack_hint"])
    if draft.get("tagline"):
        ext["tagline"] = str(draft["tagline"]).strip()
    # Who "you" are in this world (the SillyTavern persona convention) rides the card.
    if str(draft.get("user_name") or "").strip():
        ext["user_name"] = str(draft["user_name"]).strip()
    if str(draft.get("user_persona") or "").strip():
        ext["user_persona"] = str(draft["user_persona"]).strip()
    theme = _clean_theme(draft.get("theme"), draft.get("theme_color"))
    if theme:
        ext["theme"] = theme
    # No avatar from the draft — it's a manual upload/generate step (sidecar).
    embodiment = str(draft.get("embodiment") or "literal")
    ext["embodiment"] = embodiment if embodiment in ("literal", "actor") else "literal"

    description = str(draft.get("description") if draft.get("description") is not None else draft.get("appearance", ""))
    data: dict[str, Any] = {
        "name": str(draft.get("name", "")),
        "description": description,
        "personality": str(draft.get("personality", "")),
        "scenario": str(draft.get("scenario", "")) + (
            ("\n\n" + str(draft["relationship"])) if draft.get("relationship") else ""),
        "first_mes": str(draft.get("first_mes", "")),
        "mes_example": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [str(g) for g in (draft.get("alternate_greetings") or [])][:4],
        "creator_notes": str(draft.get("tagline", "")),
        "tags": ["original"],
        "extensions": {"lunamoth": ext},
    }
    if world_entries:
        data["character_book"] = {"name": f"{data['name']} world", "entries": world_entries}
    if detect_language(text=description + " " + data["first_mes"]) == "zh" and "中文" not in data["tags"]:
        data["tags"].append("中文")
    return {"version": "1.0", "name": data["name"], "data": data}


# ---- the dispatcher ----------------------------------------------------------------

class HubDispatcher:
    """Board-level JSON-RPC. All handlers are synchronous and run off the event
    loop (the transport calls dispatch() in a worker thread)."""

    def __init__(self, write: Callable[[dict[str, Any]], object], supervisor: Any | None = None):
        self._write = write
        self.supervisor = supervisor

    def dispatch(self, req: Any) -> dict[str, Any] | None:
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        rid, method, params, wants_response = normalized
        try:
            result = self._handle(method, params)
        except HubRpcError as exc:
            if not wants_response:
                return None
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": rid, "error": error}
        except RpcError as exc:
            return error_response(rid, exc.code, exc.message) if wants_response else None
        except Exception as exc:  # noqa: BLE001 - JSON-RPC is the public error boundary
            _log.exception("hub handler failed method=%s", method)
            return error_response(rid, -32000, f"handler error: {exc}") if wants_response else None
        return ok_response(rid, result) if wants_response else None

    # -- handlers ---------------------------------------------------------------

    def _handle(self, method: str, p: dict[str, Any]) -> Any:
        if method == "hub.state":
            defaults = load_defaults()
            sessions = [session_entry(m, self.supervisor) for m in S.list_sessions()]
            return {
                "version": __version__,
                "first_run": not desktop_config_path().exists() and not sessions,
                "defaults": _public_defaults(defaults),
                "presets": {k: {kk: vv for kk, vv in v.items() if kk != "api_key"} for k, v in PRESETS.items()},
                "sessions": sessions,
                "cards": list_cards(),
                "home": str(S.lunamoth_home()),
            }
        if method == "sessions.list":
            return [session_entry(m, self.supervisor) for m in S.list_sessions()]
        if method in {"session.start", "chara.start"}:
            meta = self._meta(p)
            if self.supervisor is not None:
                _await_supervisor(self.supervisor, self.supervisor.start_chara(meta.name))
            elif not start_daemon(meta):
                raise RpcError(-32033, "chara is not set up yet")
            return session_entry(meta, self.supervisor)
        if method in {"session.stop", "chara.stop"}:
            meta = self._meta(p)
            if self.supervisor is not None:
                _await_supervisor(self.supervisor, self.supervisor.stop_chara(meta.name))
            else:
                stop_daemon(meta)
            return session_entry(meta, self.supervisor)
        if method == "chara.set_autonomy":
            # Toggle autonomous running without killing the chat you're in
            # (the in-chat 'autonomy' switch). The board's start/stop touches
            # the child; this only flips the persisted pause marker.
            meta = self._meta(p)
            on = bool(p.get("on"))
            if self.supervisor is not None:
                _await_supervisor(self.supervisor, self.supervisor.set_autonomy(meta.name, on))
            else:
                from .supervisor import Supervisor
                Supervisor.set_mode_on_disk(meta, "live" if on else "chat")
            return session_entry(meta, self.supervisor)
        if method == "gateway.start":
            meta = self._meta(p)
            if self.supervisor is None:
                raise RpcError(-32060, "gateway supervision requires lunamothd")
            return _await_supervisor(self.supervisor, self.supervisor.start_gateway(meta.name, persist=True))
        if method == "gateway.stop":
            meta = self._meta(p)
            if self.supervisor is None:
                raise RpcError(-32060, "gateway supervision requires lunamothd")
            return _await_supervisor(self.supervisor, self.supervisor.stop_gateway(meta.name, persist=True))
        if method == "gateway.status":
            meta = self._meta(p)
            if self.supervisor is None:
                return _gateway_status_from_disk(meta)
            # Live query: ask the in-child host whether it is actually running,
            # waiting for a QR (needs_login), or stopped — not a heuristic.
            return _await_supervisor(self.supervisor, self.supervisor.gateway_status_live(meta.name))
        if method == "gateways.list":
            # Global gateway view: live status for every chara, one source of
            # truth shared with the per-chara panel.
            if self.supervisor is None:
                return {"gateways": [
                    {"name": m.name, "enabled": bool((_read_messaging(m) or {}).get("enabled")),
                     "gateway": _gateway_status_from_disk(m)}
                    for m in S.list_sessions()
                ]}
            return _await_supervisor(self.supervisor, self.supervisor.gateways_all_live())
        if method == "superchat.read":
            meta = self._meta(p)
            return set_superchat_read(meta, float(p.get("ts") or 0.0))
        if method == "session.delete":
            meta = self._meta(p)
            if p.get("confirm") != meta.name:
                raise RpcError(-32034, "confirmation text does not match")
            if self.supervisor is not None:
                _await_supervisor(self.supervisor, self.supervisor.stop_chara(meta.name))
                _await_supervisor(self.supervisor, self.supervisor.stop_gateway(meta.name, persist=False))
            else:
                stop_daemon(meta)
            S.delete_session(meta.name)
            return {"ok": True}
        if method == "session.export":
            return export_session(self._meta(p))
        if method == "session.wake":
            cd = p.get("card_data")
            return wake(
                card_path=str(p.get("card") or ""),
                name=str(p.get("name") or ""),
                isolation=str(p.get("isolation") or "sandbox"),
                model=str(p.get("model") or ""),
                toolpack=str(p.get("toolpack") or ""),
                embodiment=str(p.get("embodiment") or ""),
                key=str(p.get("key") or ""),
                card_data=cd if isinstance(cd, dict) else None,
            )
        if method == "toolpacks.list":
            return list_toolpacks()
        if method == "keys.list":
            return list_keys()
        if method == "keys.save":
            return save_key(str(p.get("label") or ""), provider=str(p.get("provider") or ""),
                            base_url=str(p.get("base_url") or ""), api_key=str(p.get("api_key") or ""),
                            model=str(p.get("model") or ""))
        if method == "keys.delete":
            return delete_key(str(p.get("label") or ""))
        if method == "defaults.use_key":
            return use_key(str(p.get("label") or ""))
        if method == "matte.status":
            from ..visuals import matte
            return matte.status()
        if method == "matte.install_deps":
            from ..visuals import matte
            matte.install_deps_async()
            return matte.status()
        if method == "matte.download":
            from ..visuals import matte
            mid = str(p.get("model") or "")
            if mid not in matte.MODELS:
                raise RpcError(-32602, f"unknown matte model: {mid}")
            if not matte.deps_available():
                raise RpcError(-32050, "the visuals extra isn't installed — run "
                               "`uv sync --extra visuals` (rembg/onnxruntime) first")
            matte.download_async(mid)
            return matte.status()
        if method == "matte.delete":
            from ..visuals import matte
            matte.delete(str(p.get("model") or ""))
            return matte.status()
        if method == "matte.use":
            from ..visuals import matte
            mid = str(p.get("model") or "")
            if mid not in matte.MODELS:
                raise RpcError(-32602, f"unknown matte model: {mid}")
            save_defaults({"matte_model": mid})
            return matte.status()
        if method == "chara.extras":
            return chara_extras(self._meta(p))
        if method == "works.list":
            return list_works(self._meta(p))
        if method == "works.read":
            return read_work(self._meta(p), str(p.get("rel") or ""))
        if method == "works.open":
            return open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal")))
        if method == "messaging.get":
            return messaging_get(self._meta(p))
        if method == "messaging.save":
            return messaging_save(self._meta(p), p.get("config"))
        if method == "weixin.qr":
            return weixin_qr(self._meta(p))
        if method == "weixin.qr_status":
            return weixin_qr_status(self._meta(p), str(p.get("qrcode") or ""))
        if method == "card.avatar_upload":
            return avatar_upload(str(p.get("path") or ""), str(p.get("data_b64") or ""),
                                 str(p.get("ext") or ""))
        if method == "card.visual_brief":
            # R9: build (only) the visual brief for a card via the GLOBAL default
            # text model — the UI shows/edits it, then reuses it across the set so
            # "generate all" pays for ONE brief, not one per asset.
            from ..visuals import pipeline
            try:
                card = json.loads(Path(str(p.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RpcError(-32035, f"unreadable card: {exc}") from exc
            defaults = load_defaults()
            _vp_model = str(defaults.get("image_prompt_model") or "")
            try:
                return {"brief": pipeline.build_brief(
                    card, lambda s, u: _complete(defaults, s, u, model=_vp_model, temperature=0.7, max_tokens=3000))}
            except (RuntimeError, ValueError) as exc:
                raise HubRpcError(-32050, str(exc), {"kind": "visual_brief"}) from exc
        if method == "card.visual_generate":
            # R9: card → brief (GLOBAL default text model, or a reused one) →
            # Seedream image (optionally guided by user refs) → optional matte →
            # preview bytes. Unopinionated about placement: it returns the image for
            # the UI to show/save (avatars via avatar_upload, art via asset_save).
            from ..visuals import pipeline
            path = str(p.get("path") or p.get("card_path") or "")
            kind = str(p.get("kind") or "avatar")
            if kind not in pipeline.KINDS:
                raise RpcError(-32602, f"unknown visual kind: {kind} "
                               f"(one of {', '.join(pipeline.KINDS)})")
            try:
                card = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RpcError(-32035, f"unreadable card: {exc}") from exc
            defaults = load_defaults()
            matte_opt = p.get("matte")
            brief_in = p.get("brief") if isinstance(p.get("brief"), dict) else None
            refs_in = [str(r) for r in p.get("refs")] if isinstance(p.get("refs"), list) else None
            try:
                out = pipeline.generate(
                    card, kind,
                    llm_call=lambda s, u: _complete(defaults, s, u, temperature=0.7, max_tokens=3000),
                    brief=brief_in,
                    refs=refs_in,
                    matte=(None if matte_opt is None else bool(matte_opt)),
                )
            except (RuntimeError, ValueError) as exc:
                raise HubRpcError(-32050, str(exc), {"kind": "visual_generate"}) from exc
            return {
                "data_b64": base64.b64encode(out["data"]).decode("ascii"),
                "mime": out["mime"], "ext": out["ext"], "kind": out["kind"],
                "matted": out["matted"], "note": out["note"], "brief": out["brief"],
            }
        if method == "card.asset_save":
            return asset_save(str(p.get("path") or ""), str(p.get("kind") or ""),
                              str(p.get("data_b64") or ""), str(p.get("ext") or ""))
        if method == "card.asset_delete":
            return asset_delete(str(p.get("path") or ""), str(p.get("kind") or ""))
        if method == "card.avatar_read":
            return avatar_read(str(p.get("path") or ""))
        if method == "cards.list":
            return list_cards()
        if method == "card.read":
            path = Path(str(p.get("path") or ""))
            try:
                card = CharacterCard.load(path)
            except Exception as exc:  # noqa: BLE001
                raise RpcError(-32035, f"unreadable card: {exc}") from exc
            raw: Any = None
            if path.suffix.lower() == ".json":
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = None
            return {"name": card.name, "description": card.description,
                    "personality": card.personality, "scenario": card.scenario,
                    "first_mes": card.first_mes, "alternate_greetings": card.alternate_greetings,
                    "creator_notes": card.creator_notes, "tags": card.tags,
                    "language": card.language, "extensions": _safe_extensions_for_ui(card.extensions),
                    "character_book": _book_to_dict(card.character_book),
                    "raw": raw}
        if method == "card.save":
            return save_card(p.get("data"), path=str(p.get("path") or ""))
        if method == "card.delete":
            return delete_card(str(p.get("path") or ""))
        if method == "card.restore":
            return restore_card(str(p.get("trash_id") or ""))
        if method == "card.duplicate":
            return duplicate_card(str(p.get("path") or ""))
        if method == "card.rewrite_field":
            return rewrite_card_field(load_defaults(), field=str(p.get("field") or ""),
                                      value=str(p.get("value") or ""),
                                      instruction=str(p.get("instruction") or ""),
                                      context=str(p.get("context") or ""),
                                      model=str(p.get("model") or ""))
        if method == "card.merge_world":
            return merge_world(str(p.get("card_path") or p.get("path") or ""), p.get("world"))
        if method == "cards.draft":
            inspiration = str(p.get("inspiration") or "").strip()
            if not inspiration:
                raise RpcError(-32602, "cards.draft needs inspiration")
            # Card drafting uses the per-task card_model override when set, else the
            # system default model (Settings · 模型 · 其他模态 · 生成角色卡).
            _d = load_defaults()
            return draft_card_from_inspiration(_d, inspiration, model=str(_d.get("card_model") or ""))
        if method == "card.from_draft":
            draft = p.get("draft")
            if not isinstance(draft, dict):
                raise RpcError(-32602, "card.from_draft expects a draft object")
            return save_card(
                draft_to_card(draft, origin_text=str(p.get("origin") or ""), as_draft=bool(p.get("as_draft"))),
                path=str(p.get("path") or ""),
            )
        if method == "defaults.get":
            return _public_defaults(load_defaults())
        if method == "defaults.set":
            updates = {k: v for k, v in p.items() if k in _DEFAULT_FIELDS and isinstance(v, str)}
            before = load_defaults()
            defaults = save_defaults(updates)
            public = _public_defaults(defaults)
            changed_key = "api_key" in updates and updates.get("api_key") != before.get("api_key")
            if changed_key and defaults.get("api_key"):
                public["key_update_candidates"] = key_update_candidates(defaults)
            else:
                public["key_update_candidates"] = []
            return public
        if method == "defaults.apply_key":
            names = p.get("names")
            if not isinstance(names, list):
                raise RpcError(-32602, "defaults.apply_key expects names: [...]")
            return apply_default_key([str(n) for n in names])
        if method == "key.test":
            defaults = load_defaults()
            return test_key(
                provider=str(p.get("provider") or defaults.get("provider", "")),
                base_url=str(p.get("base_url") or defaults.get("base_url", "")),
                api_key=str(p.get("api_key") or defaults.get("api_key", "")),
                model=str(p.get("model") or defaults.get("model", "")),
            )
        if method == "models.list":
            defaults = load_defaults()
            base = str(p.get("base_url") or defaults.get("base_url", ""))
            key = str(p.get("api_key") or defaults.get("api_key", ""))
            try:
                models = _catalogue(base, key)
            except Exception as exc:  # noqa: BLE001
                raise RpcError(-32036, f"could not list models: {exc}") from exc
            out = []
            for m in models:
                params = m.get("supported_parameters") or []
                arch = m.get("architecture") or {}
                out.append({
                    "id": m.get("id"), "name": m.get("name") or m.get("id"),
                    "context": m.get("context_length"),
                    "tools": ("tools" in params) if params else None,
                    "vision": "image" in (arch.get("input_modalities") or []),
                    "writing": any(s in str(m.get("id", "")).lower() for s in _WRITING_STAR),
                })
            return out
        if method == "transcribe.card":
            text = str(p.get("text") or "").strip()
            if not text:
                raise RpcError(-32602, "transcribe.card needs text")
            return transcribe_card(load_defaults(), text)
        if method == "open.path":
            return open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal")))
        raise RpcError(-32601, f"unknown method: {method}")

    @staticmethod
    def _meta(p: dict[str, Any]) -> S.SessionMeta:
        name = str(p.get("name") or "")
        meta = S.load_session(name)
        if meta is None:
            raise RpcError(-32004, f"no chara named {name!r}")
        return meta
