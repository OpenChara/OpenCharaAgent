"""Provider-specific model metadata — currently just the real context window.

The context window is NOT something the operator or a character card should set
(that was a fake knob). It is a property of the model, so we read it from the
provider. Like Hermes, we start with OpenRouter, whose public `/models` endpoint
reports each model's `context_length` — so we never hand-maintain a table.

Non-OpenRouter providers (local, etc.) fall back to a conservative default until
we add their adapters; a power user can pin it with LUNAMOTH_MODEL_CONTEXT.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

# Conservative default for providers we don't probe yet: safe for most modern
# models, small enough that a tiny local model won't silently overflow.
DEFAULT_WINDOW = 32768

# Minimum real context window we will run a live model on — apple-to-apple with
# hermes (model_metadata.py MINIMUM_CONTEXT_LENGTH = 64_000). Below this, tool
# use + compaction can't keep a usable working window, so a chara waking on a
# KNOWN-smaller model is refused (see core/agent.context_limit). Only enforced
# when the window was actually determined (env pin / provider catalogue) — an
# unknown/offline model that falls back to DEFAULT_WINDOW is never refused.
MINIMUM_CONTEXT_LENGTH = 64_000

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_DISK_TTL_SECONDS = 86400  # refetch the OpenRouter catalogue at most once a day

_memo: dict[str, dict[str, int]] = {}  # in-process cache: {"openrouter": {model_id: ctx}}


def _home() -> Path:
    return Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser()


def _openrouter_catalogue(api_key: str = "") -> dict[str, int]:
    """{model_id: context_length} from OpenRouter, cached in-process and on disk."""
    if "openrouter" in _memo:
        return _memo["openrouter"]
    cache = _home() / "openrouter_models.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < _DISK_TTL_SECONDS:
            data = {k: int(v) for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
            _memo["openrouter"] = data
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    catalogue: dict[str, int] = {}
    try:
        headers = {"User-Agent": "lunamoth"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(_OPENROUTER_MODELS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        for item in payload.get("data", []):
            mid, ctx = item.get("id"), item.get("context_length")
            if mid and isinstance(ctx, (int, float)) and ctx > 0:
                catalogue[str(mid)] = int(ctx)
        if catalogue:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(catalogue), encoding="utf-8")
            except OSError:
                pass
    except Exception:
        catalogue = {}  # offline / rate-limited: degrade to the default, never raise

    _memo["openrouter"] = catalogue
    return catalogue


def context_window_resolved(provider: str, base_url: str, model: str, api_key: str = "",
                             override: int = 0) -> tuple[int, bool]:
    """``(window, determined)``. ``determined`` is True when the window came from
    an explicit env pin, the provider catalogue, or a configured ``override``;
    False when we fell back to ``DEFAULT_WINDOW`` (unknown / offline / un-probed
    provider). Callers use the flag to refuse a KNOWN-too-small model without
    false-refusing one we simply couldn't measure. Never raises.

    ``override`` is the operator's per-config fallback for a custom / self-hosted
    model whose window the provider can't report; it is IGNORED when the provider
    reports a real window (e.g. OpenRouter's catalogue), so it can't shrink a
    known model."""
    pin = os.getenv("LUNAMOTH_MODEL_CONTEXT", "").strip()
    if pin.isdigit() and int(pin) > 0:
        return int(pin), True
    is_openrouter = provider == "openrouter" or "openrouter.ai" in (base_url or "")
    if is_openrouter and model:
        ctx = _openrouter_catalogue(api_key).get(model)
        if ctx:
            return ctx, True
    if override and override > 0:
        return int(override), True
    return DEFAULT_WINDOW, False


def context_window(provider: str, base_url: str, model: str, api_key: str = "",
                   override: int = 0) -> int:
    """Best guess at the model's real context window. Never raises."""
    return context_window_resolved(provider, base_url, model, api_key, override)[0]
