"""Provider-specific model metadata — currently just the real context window.

The context window is NOT something the operator or a character card should set
(that was a fake knob). It is a property of the model, so we read it from the
provider. Like Hermes, we start with OpenRouter, whose public `/models` endpoint
reports each model's `context_length` — so we never hand-maintain a table.

Non-OpenRouter providers (local, etc.) fall back to a conservative default until
we add their adapters; a power user can pin it with CHARA_MODEL_CONTEXT.
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

# Default max OUTPUT (completion) tokens when the model's cap is unknown —
# apple-to-apple with hermes (models_dev.py max_output_tokens default 8192). The
# request "follows the model" (OpenRouter's reported max_completion_tokens) and
# falls back to this only when the provider doesn't report one.
DEFAULT_MAX_OUTPUT = 8192

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_DISK_TTL_SECONDS = 86400  # refetch the OpenRouter catalogue at most once a day
# A FAILED fetch is retried after this cooldown, never memoized for process life:
# a transient offline moment at first resolve used to pin a 200K model to the
# 32K default window until restart. The cooldown just keeps a dead network from
# paying a 6 s timeout on every turn.
_FETCH_RETRY_SECONDS = 120

_memo: dict[str, dict[str, int]] = {}  # in-process cache: {"openrouter": {model_id: ctx}}
_fetch_failed_at = 0.0  # monotonic-ish wall clock of the last failed fetch (0 = none)


def _home() -> Path:
    return Path(os.getenv("CHARA_HOME", Path.home() / ".chara")).expanduser()


def _openrouter_catalogue(api_key: str = "") -> dict[str, int]:
    """{model_id: context_length} from OpenRouter, cached in-process and on disk.

    Also populates the PARALLEL output-token map ``_memo['openrouter_out']``
    ({model_id: max_completion_tokens}) from the SAME payload, so the max-output
    resolver below needs no second fetch. The two are always set together.

    Only a SUCCESSFUL fetch is memoized; a failure returns {} and is retried
    after ``_FETCH_RETRY_SECONDS`` (never pinned for the life of the process)."""
    global _fetch_failed_at
    if "openrouter" in _memo:
        return _memo["openrouter"]
    cache = _home() / "openrouter_models.json"
    out_cache = _home() / "openrouter_output.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < _DISK_TTL_SECONDS:
            data = {k: int(v) for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
            _memo["openrouter"] = data
            try:
                _memo["openrouter_out"] = {
                    k: int(v) for k, v in json.loads(out_cache.read_text(encoding="utf-8")).items()
                }
            except (OSError, ValueError, json.JSONDecodeError):
                _memo["openrouter_out"] = {}
            return data
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    # Failure cooldown: don't hammer a dead network with a 6 s timeout per call,
    # but DO retry once the window lapses — the failure is transient, the memo
    # would have made it permanent.
    if _fetch_failed_at and time.time() - _fetch_failed_at < _FETCH_RETRY_SECONDS:
        return {}

    catalogue: dict[str, int] = {}
    out_catalogue: dict[str, int] = {}
    try:
        headers = {"User-Agent": "chara"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(_OPENROUTER_MODELS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        for item in payload.get("data", []):
            mid, ctx = item.get("id"), item.get("context_length")
            if mid and isinstance(ctx, (int, float)) and ctx > 0:
                catalogue[str(mid)] = int(ctx)
            # Output cap: OpenRouter reports it under top_provider.max_completion_tokens
            # (sometimes top-level). Often null → the model has no declared output
            # cap and we fall back to DEFAULT_MAX_OUTPUT at resolve time.
            tp = item.get("top_provider") or {}
            out = tp.get("max_completion_tokens")
            if out is None:
                out = item.get("max_completion_tokens")
            if mid and isinstance(out, (int, float)) and out > 0:
                out_catalogue[str(mid)] = int(out)
        if catalogue:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(catalogue), encoding="utf-8")
                out_cache.write_text(json.dumps(out_catalogue), encoding="utf-8")
            except OSError:
                pass
    except Exception:
        # Offline / rate-limited: degrade to the default THIS call, never raise —
        # but never memoize the failure (a 200K model would run on the 32K
        # default for the rest of the process). Retry after the cooldown.
        _fetch_failed_at = time.time()
        return {}

    if not catalogue:
        # A response with no usable rows is a failure in success clothing —
        # treat it the same: no memo, retry after the cooldown.
        _fetch_failed_at = time.time()
        return {}

    _fetch_failed_at = 0.0
    _memo["openrouter"] = catalogue
    _memo["openrouter_out"] = out_catalogue
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
    pin = os.getenv("CHARA_MODEL_CONTEXT", "").strip()
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


def max_output_tokens(provider: str, base_url: str, model: str, api_key: str = "",
                      override: int = 0) -> int:
    """The model's max OUTPUT (completion) tokens. Like hermes: FOLLOW THE MODEL
    (OpenRouter reports ``max_completion_tokens`` per model), defaulting to
    ``DEFAULT_MAX_OUTPUT`` (8192) when the provider declares no cap or is
    unknown/offline. ``override`` (>0) is an explicit operator cap (``LLM_MAX_TOKENS``)
    that wins outright. Never raises — there is no fabricated fallback beyond the
    documented 8192 default."""
    if override and override > 0:
        return int(override)
    is_openrouter = provider == "openrouter" or "openrouter.ai" in (base_url or "")
    if is_openrouter and model:
        _openrouter_catalogue(api_key)  # populates _memo['openrouter_out'] alongside ctx
        out = _memo.get("openrouter_out", {}).get(model)
        if out and out > 0:
            return int(out)
    return DEFAULT_MAX_OUTPUT
