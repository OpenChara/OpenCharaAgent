"""Image generation backend — multi-provider, stdlib-only, so ``src/`` never
imports the dev-only ``visuals/`` package.

This is a HELPER module (underscore prefix → not AST-discovered, not a tool).
``media.py`` is the registered tool that calls in here; ``visuals/pipeline.py``
calls in too (card visual set).

Providers (catalogue in ``content/image_providers.py``):
  - **ark** (Volcano Ark / Doubao-Seedream) — synchronous POST images/generations,
    returns ``data[].url``. The original, simplest path.
  - **openai** — synchronous POST images/generations, ``data[].b64_json`` (GPT
    Image) or ``data[].url`` (DALL·E).
  - **dashscope** (Alibaba 通义万相 / Wan) — ASYNC: create a task, poll until
    SUCCEEDED, then the image URL.
  - **openrouter** — image via chat/completions with ``modalities:[image,text]``;
    the image comes back as a ``data:`` URL in ``message.images[]``.

The provider AND model are EXACTLY what Settings · 模型 · 生图模型 holds
(``image_provider`` / ``image_model`` in ``~/.lunamoth/desktop.json``) — no
inference, no default. The per-provider key REUSES the named provider keyring via
ONE unified path (matched by provider id or base_url host), the same way the text
providers resolve their key — a single Volcano / Alibaba / OpenAI / OpenRouter key
serves both text and image. No env / legacy-file special-cases.

No failure fallbacks: a transient connect/server error is retried (5 s × 5,
matching the text client's policy); a hard failure raises a clear exception
carrying the server's own message — never a fabricated success, never a silent
switch to another provider.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ...content import image_providers as _ip

# The Volcano Ark images endpoint (the ark adapter posts here; base_url is fixed
# for Ark in practice). Other providers use the catalogue base URLs.
ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

# Transient HTTP statuses worth retrying (rate-limit + upstream gateway errors).
_RETRY_HTTP = (408, 429, 500, 502, 503, 504)
_RETRY_SLEEP = 5.0  # seconds between tries (5 s × 5, like the text client)
_MAX_IMAGE_BYTES = 32 * 1024 * 1024  # cap the download (images are a few MB)

# DashScope async image API (the IMAGE path lives on /api/v1, distinct from the
# OpenAI-compatible /compatible-mode/v1 text base).
_DASHSCOPE_HOST = "https://dashscope.aliyuncs.com"
_DASHSCOPE_CREATE = "/api/v1/services/aigc/image-generation/generation"
_DASHSCOPE_TASK = "/api/v1/tasks/"
_DASHSCOPE_POLL_SLEEP = 6.0   # seconds between status polls
_DASHSCOPE_MAX_POLLS = 60     # ~6 min ceiling before giving up (visible error)

# Magic-byte signatures of the image formats we accept. Validating these before
# saving means a non-image error body returned at HTTP 200 is REJECTED, not
# written to disk as a fake ".png" and reported as success (no soft fallback).
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
    b"GIF87a", b"GIF89a",    # GIF
    b"BM",                   # BMP
)


def is_image_bytes(data: bytes) -> bool:
    """True if *data* starts with a known image signature (PNG/JPEG/GIF/BMP/WebP)."""
    if not data:
        return False
    if any(data.startswith(sig) for sig in _IMAGE_SIGNATURES):
        return True
    # WebP: 'RIFF'....'WEBP'
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _home() -> Path:
    return Path(os.getenv("LUNAMOTH_HOME", str(Path.home() / ".lunamoth")))


def _desktop_json() -> dict:
    """The global web keyring/defaults at ``~/.lunamoth/desktop.json`` (honoring
    ``LUNAMOTH_HOME``). Read directly, stdlib-only — ``tools/`` must never import
    ``server/`` (the hub owns the WRITE side; this is only the READ side)."""
    try:
        raw = json.loads((_home() / "desktop.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---- provider / key / model resolution -------------------------------------------

def active_provider() -> str:
    """The selected image provider id = EXACTLY ``image_provider`` from Settings ·
    模型 · 生图模型. No inference from the model id, no default — "" if unset."""
    return _ip.resolve_provider(str(_desktop_json().get("image_provider") or ""))


def image_key_for(provider_id: str) -> str:
    """The api key for one image provider, from the shared provider keyring — the
    SAME unified path for every provider (no env / no legacy file special-cases).
    "" if none."""
    return _ip.resolve_key(_desktop_json(), provider_id)


def image_key() -> str:
    """The key for the ACTIVE image provider ("" if none). Used by the tool's
    capability gate and the visuals pipeline guard."""
    return image_key_for(active_provider())


def image_model() -> str:
    """The image-generation model = EXACTLY ``image_model`` from Settings · 模型 ·
    生图模型. No env override, no default — "" if unset."""
    return str(_desktop_json().get("image_model") or "").strip()


def _base_url_for(provider_id: str) -> str:
    return _ip.resolve_base_url(_desktop_json(), provider_id)


# ---- low-level HTTP (shared by the openai / dashscope / openrouter adapters) -------

def _request_json(url: str, *, data: bytes | None, headers: dict[str, str],
                  method: str, timeout: int, tries: int) -> dict:
    """POST/GET JSON with the shared retry policy (transient HTTP + connect errors
    retried 5 s × ``tries``). Raises ``RuntimeError`` carrying the last server
    message on final failure. Never fabricates a result."""
    last = ""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                last = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                last = str(e)
            if e.code in _RETRY_HTTP and attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            if attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
    raise RuntimeError(f"image request failed: {last}")


def _auth_headers(key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}


def _decode_data_url(url: str) -> bytes:
    """Decode a ``data:image/...;base64,...`` URL to raw bytes (capped)."""
    head, _, b64 = url.partition(",")
    if "base64" not in head:
        raise RuntimeError("unexpected non-base64 data URL from the image endpoint")
    raw = base64.b64decode(b64, validate=False)
    if len(raw) > _MAX_IMAGE_BYTES:
        raise RuntimeError(f"image too large (> {_MAX_IMAGE_BYTES} bytes)")
    return raw


def _bytes_from_result_url(url: str) -> bytes:
    """A result URL may be an http(s) link (download it) or an inline data: URL
    (decode it). Anything else is refused (SSRF guard)."""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme in ("http", "https"):
        return download_bytes(url)
    if scheme == "data":
        return _decode_data_url(url)
    raise RuntimeError(f"refusing a non-http(s)/data image URL: {url[:80]!r}")


# ---- adapter: Volcano Ark (synchronous) ------------------------------------------

def ark_generate(prompt: str, size: str, *, refs: list[str] | None = None,
                 timeout: int = 240, tries: int = 5) -> list[str]:
    """POST a generation request to Ark; return the list of result image URLs.

    ``refs`` (optional) are reference images — http(s) URLs or ``data:`` URIs —
    passed as Seedream's ``image`` input so generation is guided by user-provided
    art (a key visual / pose reference). Retries transient failures (HTTP 429/5xx,
    connect timeouts/URLErrors) up to ``tries`` times with a 5 s pause between. On
    final failure raises ``RuntimeError`` carrying the last server message. Never
    fabricates a result.
    """
    body = {
        "model": image_model(),
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    if refs:
        body["image"] = list(refs)
    data = json.dumps(body).encode("utf-8")
    last = ""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            ENDPOINT, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {image_key()}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read())
            return [d["url"] for d in j["data"]]
        except urllib.error.HTTPError as e:
            try:
                last = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                last = str(e)
            if e.code in _RETRY_HTTP and attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # transient connect error (DNS, refused, timeout) — retry
            last = str(e)
            if attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
    raise RuntimeError(f"image generation failed: {last}")


def _ark_bytes(model: str, key: str, prompt: str, size: str,
               refs: list[str] | None) -> bytes:
    urls = ark_generate(prompt, size, refs=refs)
    if not urls:
        raise RuntimeError("image generation returned no result")
    return download_bytes(urls[0])


# ---- adapter: OpenAI images (synchronous) ----------------------------------------

_OPENAI_SIZES = {"1024x1024", "1536x1024", "1024x1536", "1792x1024", "1024x1792", "auto"}


def _coerce_openai_size(size: str) -> str:
    """OpenAI image models accept only a fixed set of sizes; anything else (e.g.
    the 2048x2048 default) is mapped to the nearest safe square so a request isn't
    rejected outright."""
    s = (size or "").strip().lower()
    if s in _OPENAI_SIZES:
        return s
    if "x" in s:
        w, _, h = s.partition("x")
        if w.isdigit() and h.isdigit():
            iw, ih = int(w), int(h)
            if iw > ih:
                return "1536x1024"
            if ih > iw:
                return "1024x1536"
    return "1024x1024"


def _openai_bytes(base: str, key: str, model: str, prompt: str, size: str) -> bytes:
    body = {"model": model, "prompt": prompt, "size": _coerce_openai_size(size), "n": 1}
    j = _request_json(base.rstrip("/") + "/images/generations",
                      data=json.dumps(body).encode("utf-8"),
                      headers=_auth_headers(key), method="POST", timeout=240, tries=5)
    items = j.get("data") if isinstance(j, dict) else None
    if not isinstance(items, list) or not items:
        raise RuntimeError("image generation returned no result")
    first = items[0] if isinstance(items[0], dict) else {}
    if first.get("b64_json"):
        raw = base64.b64decode(str(first["b64_json"]), validate=False)
        if len(raw) > _MAX_IMAGE_BYTES:
            raise RuntimeError(f"image too large (> {_MAX_IMAGE_BYTES} bytes)")
        return raw
    if first.get("url"):
        return download_bytes(str(first["url"]))
    raise RuntimeError("image endpoint returned neither b64_json nor url")


# ---- adapter: Alibaba DashScope 通义万相 (async task) -------------------------------

def _dashscope_size(size: str) -> str:
    """DashScope uses ``W*H`` (asterisk); the rest of the app uses ``WxH``."""
    s = (size or "").strip().lower().replace("x", "*")
    return s or "1280*1280"


def _dashscope_image_from_output(output: dict) -> str:
    """Find the result image URL in a SUCCEEDED DashScope output (handles both the
    wan2.6 multimodal ``choices[].message.content[].image`` and the older
    ``results[].url`` shapes)."""
    choices = output.get("choices")
    if isinstance(choices, list):
        for ch in choices:
            msg = ch.get("message") if isinstance(ch, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("image"):
                        return str(part["image"])
    results = output.get("results")
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and r.get("url"):
                return str(r["url"])
    raise RuntimeError("DashScope task succeeded but carried no image URL")


def _dashscope_bytes(key: str, model: str, prompt: str, size: str,
                     refs: list[str] | None) -> bytes:
    content: list[dict] = [{"text": prompt}]
    for r in (refs or []):
        content.append({"image": str(r)})
    params: dict = {"n": 1, "size": _dashscope_size(size), "watermark": False}
    # wan2.6's message API requires 1–4 input images UNLESS enable_interleave is
    # on — so for pure text-to-image (no refs) we turn it on; image-editing (refs)
    # leaves it off.
    if not refs:
        params["enable_interleave"] = True
    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": params,
    }
    headers = _auth_headers(key)
    headers["X-DashScope-Async"] = "enable"
    created = _request_json(_DASHSCOPE_HOST + _DASHSCOPE_CREATE,
                            data=json.dumps(body).encode("utf-8"),
                            headers=headers, method="POST", timeout=60, tries=5)
    out = created.get("output") if isinstance(created, dict) else None
    task_id = str(out.get("task_id") or "") if isinstance(out, dict) else ""
    if not task_id:
        raise RuntimeError(f"DashScope did not return a task id: {str(created)[:200]}")
    # Poll until SUCCEEDED / FAILED.
    poll_headers = {"Authorization": f"Bearer {key}"}
    for _ in range(_DASHSCOPE_MAX_POLLS):
        got = _request_json(_DASHSCOPE_HOST + _DASHSCOPE_TASK + task_id,
                            data=None, headers=poll_headers, method="GET",
                            timeout=60, tries=5)
        o = got.get("output") if isinstance(got, dict) else None
        status = str(o.get("task_status") or "") if isinstance(o, dict) else ""
        if status == "SUCCEEDED":
            return download_bytes(_dashscope_image_from_output(o))
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            msg = (o.get("message") if isinstance(o, dict) else "") or str(got)[:200]
            raise RuntimeError(f"DashScope task {status}: {msg}")
        time.sleep(_DASHSCOPE_POLL_SLEEP)
    raise RuntimeError("DashScope task did not finish in time")


# ---- adapter: OpenRouter (image via chat/completions modalities) ------------------

def _openrouter_bytes(base: str, key: str, model: str, prompt: str,
                      refs: list[str] | None) -> bytes:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for r in (refs or []):
        content.append({"type": "image_url", "image_url": {"url": str(r)}})
    url = base.rstrip("/") + "/chat/completions"

    def _call(modalities: list[str]) -> dict:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": modalities,
        }
        return _request_json(url, data=json.dumps(body).encode("utf-8"),
                             headers=_auth_headers(key), method="POST", timeout=240, tries=5)

    # Most image models output both image+text; image-only models (e.g. grok-imagine)
    # reject that pairing — fall back to image-only when the endpoint says so. This
    # adjusts the request to the model's real capability, not a fabricated result.
    try:
        j = _call(["image", "text"])
    except RuntimeError as e:
        if "modalit" in str(e).lower():
            j = _call(["image"])
        else:
            raise
    choices = j.get("choices") if isinstance(j, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("image generation returned no result")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    images = msg.get("images") if isinstance(msg, dict) else None
    if isinstance(images, list) and images:
        first = images[0] if isinstance(images[0], dict) else {}
        url = (first.get("image_url") or {}).get("url") if isinstance(first.get("image_url"), dict) else first.get("url")
        if url:
            return _bytes_from_result_url(str(url))
    raise RuntimeError("OpenRouter returned no image in the response")


# ---- the unified entry point -----------------------------------------------------

def generate_bytes(prompt: str, size: str, *, refs: list[str] | None = None) -> bytes:
    """Generate one image with the SELECTED provider + model and return validated
    image bytes. Uses exactly what Settings · 模型 · 生图模型 holds — no inference,
    no fallback. Raises ``RuntimeError`` on any failure (no provider/model/key
    selected, no result, a non-image body) — never a fabricated success."""
    pid = active_provider()
    sp = _ip.spec(pid)
    if sp is None:
        raise RuntimeError(
            "no image provider selected — choose one in Settings · 模型 · 生图模型.")
    model = image_model()
    if not model:
        raise RuntimeError(
            "no image model selected — choose one in Settings · 模型 · 生图模型.")
    key = image_key_for(pid)
    if not key:
        raise RuntimeError(
            f"no key for {sp['label']} — add it in Settings · 提供商.")
    adapter = sp["adapter"]
    if adapter == "ark":
        data = _ark_bytes(model, key, prompt, size, refs)
    elif adapter == "openai":
        data = _openai_bytes(_base_url_for(pid), key, model, prompt, size)
    elif adapter == "dashscope":
        data = _dashscope_bytes(key, model, prompt, size, refs)
    elif adapter == "openrouter":
        data = _openrouter_bytes(_base_url_for(pid), key, model, prompt, refs)
    else:
        raise RuntimeError(f"unsupported image adapter: {adapter}")
    if not is_image_bytes(data):
        raise RuntimeError(
            "the generation endpoint did not return an image "
            "(got a non-image response); nothing was saved")
    return data


def download_bytes(url: str, *, timeout: int = 120, tries: int = 5,
                   max_bytes: int = _MAX_IMAGE_BYTES) -> bytes:
    """GET ``url`` and return the raw bytes (capped at ``max_bytes`` so a runaway
    or malformed response can't exhaust memory), retrying transient errors 5 × 5 s.
    Raises ``RuntimeError`` carrying the last error on final failure, or if the
    body exceeds the cap.

    SSRF guard: the URL comes from a provider's API response, not the model — but
    we still only fetch http(s), never file://, ftp://, data:, etc., so a
    malformed/hostile result URL can't read a local file or reach an internal
    scheme."""
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise RuntimeError(f"refusing to fetch a non-http(s) image URL: {url[:80]!r}")
    last = ""
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise RuntimeError(f"image too large (> {max_bytes} bytes)")
                return data
        except urllib.error.HTTPError as e:
            try:
                last = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                last = str(e)
            if e.code in _RETRY_HTTP and attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            if attempt < tries:
                time.sleep(_RETRY_SLEEP)
                continue
            break
    raise RuntimeError(f"image download failed: {last}")
