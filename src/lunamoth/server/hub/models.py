"""Provider HTTP: model catalogues, capability badges, key tests, completions.

No core/ import — plain OpenAI-compatible HTTP. `_http_json` and `_complete` are
the two functions the test-suite monkeypatches on the hub PACKAGE namespace
(``H._http_json`` / ``H._complete``), so every internal caller resolves them off
the package at call time (see ``_pkg``) — never a module-local binding that a
package-level patch would miss.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ._common import HubRpcError

_log = logging.getLogger("lunamoth.server.hub")

# Models with a reputation for prose ("书写 ★"); heuristic, substring match.
_WRITING_STAR = ("claude", "deepseek-v4", "gpt-5", "gemini-2", "kimi", "grok-4", "qwen3-max")

_HTTP_TIMEOUT = 20.0


def _pkg():
    """The hub package module — so a test patching ``H._http_json`` / ``H._complete``
    is honored by internal callers (they look the name up here at call time)."""
    from .. import hub
    return hub


def _http_json(url: str, api_key: str = "", payload: dict | None = None, timeout: float = _HTTP_TIMEOUT) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # OpenRouter app attribution (name + icon) so hub-side auxiliary completions
    # — card drafting, field rewrites, image-prompt — group under the same app.
    from ...config import openrouter_attribution_headers
    headers.update(openrouter_attribution_headers(url))
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# A provider's model line-up (id + modality + context) is pulled LIVE from its
# /models endpoint and cached to disk; we only re-pull when the cache is older than
# the refresh interval (default one day, operator-tunable via the desktop default
# `model_refresh_interval`). When a provider is unreachable we degrade to the stale
# disk copy, then to a small curated fallback — so a model picker never goes empty.
_DEFAULT_REFRESH_SECONDS = 86_400  # one day
_MIN_REFRESH_SECONDS = 60.0        # floor: the catalogue is metadata, never re-pull more than once a minute
_DISK_CACHE_MAX_ENTRIES = 32       # cap distinct base_urls on disk so abandoned/typo'd endpoints don't pile up
_models_cache: dict[str, tuple[float, list[dict]]] = {}  # in-process memo: base_url -> (wall_ts, models)


def refresh_interval_seconds() -> float:
    """The configured catalogue refresh interval in seconds (default one day).
    0 / blank / invalid / non-finite → the default; a tiny positive value is floored
    to _MIN_REFRESH_SECONDS so a fat-fingered config can't re-pull /models in a hot loop."""
    try:
        from .config import load_defaults  # local import: avoid any import cycle
        raw = str(load_defaults().get("model_refresh_interval") or "").strip()
    except Exception:  # noqa: BLE001 - defaults unreadable → just use the default
        return _DEFAULT_REFRESH_SECONDS
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_REFRESH_SECONDS
    if not math.isfinite(val) or val <= 0:
        return _DEFAULT_REFRESH_SECONDS
    return max(val, _MIN_REFRESH_SECONDS)


def _catalogue_cache_path() -> Path:
    home = Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser()
    return home / "model_catalogue.json"


def _load_disk_catalogue() -> dict[str, Any]:
    try:
        data = json.loads(_catalogue_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save_disk_catalogue(base: str, fetched_at: float, models: list[dict]) -> None:
    try:
        data = _load_disk_catalogue()
        data[base] = {"fetched_at": fetched_at, "models": models}
        if len(data) > _DISK_CACHE_MAX_ENTRIES:
            # Keep the newest N by fetched_at — a once-tried relay or a typo'd base_url
            # would otherwise linger forever, each holding a full /models payload.
            newest = sorted(
                data.items(),
                key=lambda kv: kv[1].get("fetched_at", 0.0) if isinstance(kv[1], dict) else 0.0,
                reverse=True,
            )[:_DISK_CACHE_MAX_ENTRIES]
            data = dict(newest)
        path = _catalogue_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        _log.debug("could not persist model catalogue", exc_info=True)


def _catalogue_meta(base_url: str, api_key: str = "", *,
                    refresh_seconds: float | None = None, provider: str = "") -> tuple[list[dict], str]:
    """Provider /models catalogue plus its SOURCE, so callers can tell the user when
    a list isn't live. Source is one of:
      ``fresh``    — served from the in-interval memo / disk cache, or a live pull
      ``stale``    — a live pull failed; serving an OLDER disk copy
      ``fallback`` — no cache AND offline → the curated built-in list
    DISK-cached; pulls live only when older than ``refresh_seconds`` (default ~one
    day). Never raises, never empty for a known provider just because it's offline."""
    base = base_url.rstrip("/")
    if not base:
        return _fallback_models(provider, base), "fallback"
    if refresh_seconds is None:
        refresh_seconds = refresh_interval_seconds()
    now = time.time()  # wall clock (the disk cache must survive restarts)
    memo = _models_cache.get(base)
    if memo and now - memo[0] < refresh_seconds:
        return memo[1], "fresh"
    disk = _load_disk_catalogue()
    entry = disk.get(base) if isinstance(disk.get(base), dict) else None
    if entry and isinstance(entry.get("models"), list) \
            and now - float(entry.get("fetched_at") or 0) < refresh_seconds:
        _models_cache[base] = (float(entry["fetched_at"]), entry["models"])
        return entry["models"], "fresh"
    # cache stale or missing → try a live pull
    try:
        data = _pkg()._http_json(base + "/models", api_key)
        models = data.get("data") if isinstance(data, dict) else None
        if isinstance(models, list) and models:
            _models_cache[base] = (now, models)
            _save_disk_catalogue(base, now, models)
            return models, "fresh"
    except Exception:  # noqa: BLE001 - offline / rate-limited / sparse → fall back
        _log.debug("model catalogue fetch failed for %s", base, exc_info=True)
    if entry and isinstance(entry.get("models"), list):
        return entry["models"], "stale"  # stale-but-real beats a guess
    return _fallback_models(provider, base), "fallback"


def _catalogue(base_url: str, api_key: str = "", *,
               refresh_seconds: float | None = None, provider: str = "") -> list[dict]:
    """The model list alone (see ``_catalogue_meta`` for the source/staleness)."""
    return _catalogue_meta(base_url, api_key, refresh_seconds=refresh_seconds, provider=provider)[0]


def _fb(mid: str, name: str, ctx: int, *, vision: bool = False, tools: bool = True) -> dict:
    """A fallback catalogue entry in the same shape /models returns (so the same
    extraction reads id / context / tools / vision off it)."""
    return {"id": mid, "name": name, "context_length": ctx,
            "supported_parameters": (["tools"] if tools else []),
            "architecture": {"input_modalities": (["text", "image"] if vision else ["text"])}}


# Curated last-resort line-ups — used ONLY when a provider's /models is unreachable
# AND nothing is cached on disk. Edit as provider catalogues change; once an endpoint
# is reached once, its live (disk-cached) list supersedes this. IDs current ~2026-06.
_FALLBACK_MODELS: dict[str, list[dict]] = {
    "openai": [
        _fb("gpt-4o", "GPT-4o", 128_000, vision=True),
        _fb("gpt-4o-mini", "GPT-4o mini", 128_000, vision=True),
        _fb("o4-mini", "o4-mini", 200_000),
    ],
    "openrouter": [
        _fb("openai/gpt-4o", "GPT-4o", 128_000, vision=True),
        _fb("anthropic/claude-sonnet-4", "Claude Sonnet 4", 200_000, vision=True),
        _fb("google/gemini-2.5-flash", "Gemini 2.5 Flash", 1_000_000, vision=True),
        _fb("deepseek/deepseek-chat", "DeepSeek Chat", 64_000),
    ],
    "volcano": [
        _fb("doubao-seed-1-6", "Doubao Seed 1.6", 256_000, vision=True),
        _fb("doubao-pro-32k", "Doubao Pro 32k", 32_000),
    ],
    "dashscope": [
        _fb("qwen-max", "Qwen Max", 32_000),
        _fb("qwen-plus", "Qwen Plus", 131_072),
        _fb("qwen-vl-max", "Qwen VL Max", 32_000, vision=True),
    ],
    "hunyuan": [
        _fb("hunyuan-turbos-latest", "Hunyuan TurboS", 32_000),
        _fb("hunyuan-large", "Hunyuan Large", 32_000),
    ],
}

_HOST_TO_PROVIDER = (
    ("openrouter.ai", "openrouter"), ("api.openai.com", "openai"),
    ("volces.com", "volcano"), ("hunyuan", "hunyuan"),
    ("dashscope", "dashscope"), ("aliyuncs.com", "dashscope"),
)


def _fallback_models(provider: str, base: str) -> list[dict]:
    key = provider if provider in _FALLBACK_MODELS else ""
    if not key:
        b = (base or "").lower()
        for host, prov in _HOST_TO_PROVIDER:
            if host in b:
                key = prov
                break
    return list(_FALLBACK_MODELS.get(key, []))


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
    """An honest connectivity + auth test. With a model, one tiny completion;
    without one (a provider key row stores no model), a GET /models reachability +
    auth check — enough to confirm the key works."""
    base = base_url.rstrip("/")
    if not model:
        try:
            _pkg()._http_json(base + "/models", api_key, timeout=30.0)
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": _classify_http_error(exc.code, _http_error_detail(exc))}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "error": {"kind": "network", "detail": str(getattr(exc, "reason", exc))}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"kind": "unknown", "detail": str(exc)}}
        return {"ok": True, "model": ""}
    try:
        data = _pkg()._http_json(
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
        data = _pkg()._http_json(base + "/chat/completions", defaults.get("api_key", ""), payload, timeout=180.0)
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
