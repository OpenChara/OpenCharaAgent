"""Image-generation provider catalogue — the ONE source of truth for which
image platforms OpenCharaAgent can drive, their selectable models, and how to find the
credential for each.

PURE DATA + pure functions (stdlib only, no file IO, no imports from tools/ or
server/). Both sides import this:
  - ``tools/builtin/_image_gen.py`` (the runtime adapters) routes by it and
    resolves the per-provider key from a desktop.json dict.
  - ``server/hub.py`` (the ``image.catalog`` RPC) lists providers + models +
    per-provider key presence for the Settings UI.

Design (handover 2026-06-18 → multi-provider, owner-scoped 2026-06-19 to
Volcano Ark + Alibaba DashScope, with OpenAI + OpenRouter added by owner request):

  - The active provider is EXACTLY the one selected in Settings · 模型 · 生图模型
    (``image_provider`` in desktop.json). No inference, no fallback — an unset
    value yields a plain error at generation time.
  - The per-provider key REUSES the named provider keyring (the desktop ``keys``
    map that powers the text providers): a key is matched by its ``provider`` id
    OR by its ``base_url`` host — the SAME unified path for every provider. So a
    single Volcano / Alibaba / OpenAI / OpenRouter key serves both text and image.

No brand text here is model-facing — these labels appear only in the Settings UI
and the catalogue RPC, never in a prompt the chara sees.
"""
from __future__ import annotations

from typing import Any

# Each provider:
#   id          stable key persisted as desktop ``image_provider``
#   label       UI display name (provider brand; UI-only, never model-facing)
#   adapter     which request/response shape _image_gen dispatches to
#   base_url    default endpoint base (a matched keyring entry's base_url wins,
#               so relays / self-hosted OpenAI-compatible work); DashScope ignores
#               this (its image API lives on a different path than its text base)
#   domains     host fragments that identify this provider's keyring entry
#   key_provider the provider id a keyring entry may carry for this platform
#   models      curated selectable models {id, label}
IMAGE_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "volcano",
        "label": "火山方舟 Seedream",
        "adapter": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "domains": ("volces.com",),
        "key_provider": "volcano",
        "models": [
            # Ark wants the lowercase-hyphen model id (e.g. doubao-seedream-4-0-250828),
            # NOT the display name — a display-name id ("Doubao-Seedream-5.0-lite") is
            # rejected by Ark as an unknown model (the "连接模型失败" the user hit).
            # Order: 4.5 > 5.0 Lite > 4.0.
            {"id": "doubao-seedream-4-5-251128", "label": "Doubao Seedream 4.5"},
            {"id": "doubao-seedream-5-0-lite-260128", "label": "Doubao Seedream 5.0 Lite"},
            {"id": "doubao-seedream-4-0-250828", "label": "Doubao Seedream 4.0"},
        ],
    },
    {
        "id": "dashscope",
        "label": "阿里云 通义万相",
        "adapter": "dashscope",
        # The DashScope IMAGE API lives on /api/v1 (async tasks), NOT the
        # OpenAI-compatible /compatible-mode/v1 base used for text. The adapter
        # hard-codes its create/poll paths; only the KEY is taken from the keyring.
        "base_url": "https://dashscope.aliyuncs.com",
        "domains": ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                    "dashscope-us.aliyuncs.com"),
        "key_provider": "dashscope",
        # The message-based wan2.x image models (our adapter speaks that API). The
        # older wanx2.x text2image endpoint is a different shape — free-type those.
        "models": [
            {"id": "wan2.6-image", "label": "通义万相 Wan2.6"},
            {"id": "wan2.5-image", "label": "通义万相 Wan2.5"},
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "adapter": "openai",
        "base_url": "https://api.openai.com/v1",
        "domains": ("api.openai.com",),
        "key_provider": "openai",
        "models": [
            {"id": "gpt-image-1", "label": "GPT Image 1"},
            {"id": "dall-e-3", "label": "DALL·E 3"},
            {"id": "dall-e-2", "label": "DALL·E 2"},
        ],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "adapter": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "domains": ("openrouter.ai",),
        "key_provider": "openrouter",
        # Curated picks (incl. grok-imagine, which OpenRouter's /models does NOT
        # list yet still serves). The hub MERGES the live image-output models from
        # OpenRouter's catalogue on top of these — see catalogue()/merge_dynamic.
        "models": [
            {"id": "x-ai/grok-imagine-image-quality", "label": "Grok Imagine (quality)"},
            {"id": "x-ai/grok-imagine-image", "label": "Grok Imagine"},
        ],
        "dynamic": True,
    },
]

_BY_ID = {p["id"]: p for p in IMAGE_PROVIDERS}


def provider_ids() -> list[str]:
    return [p["id"] for p in IMAGE_PROVIDERS]


def spec(provider_id: str) -> dict[str, Any] | None:
    return _BY_ID.get(str(provider_id or ""))


def resolve_provider(image_provider: str) -> str:
    """The active image provider = EXACTLY the one selected in Settings · 模型 ·
    生图模型 (``image_provider``). No inference, no fallback: an unset/invalid value
    returns "" and the caller surfaces a plain error. The chosen model is used as
    selected; we never guess a provider from the model id."""
    pid = str(image_provider or "").strip()
    return pid if pid in _BY_ID else ""


def _keys_entries(raw_desktop: dict[str, Any]):
    keys = raw_desktop.get("keys")
    if isinstance(keys, dict):
        for label, item in keys.items():
            if isinstance(item, dict):
                yield str(label), item


def _matched_entry(raw_desktop: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    """The keyring entry serving this image provider: by provider id OR base_url
    host. Returns the raw entry dict (may carry api_key/base_url) or None."""
    sp = spec(provider_id)
    if sp is None:
        return None
    key_provider = str(sp.get("key_provider") or "")
    domains = tuple(sp.get("domains") or ())
    for _label, item in _keys_entries(raw_desktop):
        if not str(item.get("api_key") or ""):
            continue
        if key_provider and str(item.get("provider") or "") == key_provider:
            return item
        base = str(item.get("base_url") or "")
        if base and any(d in base for d in domains):
            return item
    return None


def resolve_key(raw_desktop: dict[str, Any], provider_id: str) -> str:
    """The api_key for an image provider, from the named provider keyring (matched
    by provider id OR base_url host) — the SAME unified path for every provider,
    identical to how the text providers resolve their key. "" if none."""
    entry = _matched_entry(raw_desktop, provider_id)
    if entry and str(entry.get("api_key") or ""):
        return str(entry["api_key"])
    return ""


def resolve_base_url(raw_desktop: dict[str, Any], provider_id: str) -> str:
    """The endpoint base for an image provider: a matched keyring entry's
    base_url (so relays / self-hosted endpoints work) else the catalogue default.
    DashScope ALWAYS uses its catalogue default (its image API path differs from
    the text base), so the adapter ignores this for DashScope."""
    sp = spec(provider_id)
    if sp is None:
        return ""
    entry = _matched_entry(raw_desktop, provider_id)
    if entry and str(entry.get("base_url") or "").strip():
        return str(entry["base_url"]).strip()
    return str(sp.get("base_url") or "")


def has_key(raw_desktop: dict[str, Any], provider_id: str) -> bool:
    return bool(resolve_key(raw_desktop, provider_id))


def merge_models(curated: list[dict[str, Any]],
                 fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Curated picks first, then any fetched models not already listed (dedup by
    id). Used to graft a provider's LIVE catalogue (e.g. OpenRouter's image-output
    models) on top of the hand-curated quick-picks."""
    seen = {m["id"] for m in curated}
    out = [dict(m) for m in curated]
    for m in fetched:
        mid = str(m.get("id") or "")
        if mid and mid not in seen:
            seen.add(mid)
            out.append({"id": mid, "label": str(m.get("label") or mid)})
    return out


def catalogue(raw_desktop: dict[str, Any],
              dynamic: "Any" = None) -> list[dict[str, Any]]:
    """The UI-facing catalogue: each provider with its models + whether a usable
    key is configured. ``active`` marks the currently-selected provider.

    ``dynamic`` is an optional callable ``(provider_id, base_url, api_key) ->
    [{id,label}]`` the caller (the hub) supplies to fetch a provider's LIVE image
    models (only providers flagged ``dynamic`` in the catalogue ask for it). It is
    injected so this module stays pure (no network); failures fall back to curated."""
    active = resolve_provider(str(raw_desktop.get("image_provider") or ""))
    out: list[dict[str, Any]] = []
    for p in IMAGE_PROVIDERS:
        models = [dict(m) for m in p["models"]]
        if p.get("dynamic") and dynamic is not None and has_key(raw_desktop, p["id"]):
            try:
                fetched = dynamic(p["id"], resolve_base_url(raw_desktop, p["id"]),
                                  resolve_key(raw_desktop, p["id"]))
                if fetched:
                    models = merge_models(models, fetched)
            except Exception:  # noqa: BLE001 — live fetch is best-effort
                pass
        out.append({
            "id": p["id"],
            "label": p["label"],
            "adapter": p["adapter"],
            "models": models,
            "has_key": has_key(raw_desktop, p["id"]),
            "active": p["id"] == active,
        })
    return out
