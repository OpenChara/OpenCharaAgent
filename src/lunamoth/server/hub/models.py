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
import time
import urllib.error
import urllib.request
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


_models_cache: dict[str, tuple[float, list[dict]]] = {}


def _catalogue(base_url: str, api_key: str = "") -> list[dict]:
    """Provider /models catalogue, cached for the hub's lifetime (10 min TTL)."""
    base = base_url.rstrip("/")
    now = time.monotonic()
    hit = _models_cache.get(base)
    if hit and now - hit[0] < 600:
        return hit[1]
    data = _pkg()._http_json(base + "/models", api_key)
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
