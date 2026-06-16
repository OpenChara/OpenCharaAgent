"""Volcano Ark (Doubao-Seedream) image generation — the minimal request logic
ported from ``visuals/genviz.py`` (``ark_image``), stdlib-only, so ``src/`` never
imports the dev-only ``visuals/`` package.

This is a HELPER module (underscore prefix → not AST-discovered, not a tool).
``media.py`` is the registered tool that calls into here.

No failure fallbacks: a transient connect/server error is retried (5 s × 5,
matching the text client's policy); a hard failure raises a clear exception
carrying the server's own message — never a fabricated success.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
DEFAULT_MODEL = "doubao-seedream-5-0-260128"

# Transient HTTP statuses worth retrying (rate-limit + upstream gateway errors).
_RETRY_HTTP = (408, 429, 500, 502, 503, 504)
_RETRY_SLEEP = 5.0  # seconds between tries (5 s × 5, like the text client)
_MAX_IMAGE_BYTES = 32 * 1024 * 1024  # cap the download (Ark images are a few MB)

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


def image_key() -> str:
    """Resolve the Ark API key: env ``ARK_API_KEY`` first, else the
    ``~/.lunamoth/ark_api_key`` file (honoring ``LUNAMOTH_HOME``). "" if none."""
    k = (os.getenv("ARK_API_KEY") or "").strip()
    if k:
        return k
    p = _home() / "ark_api_key"
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def image_model() -> str:
    return (os.getenv("ARK_IMAGE_MODEL") or "").strip() or DEFAULT_MODEL


def ark_generate(prompt: str, size: str, *, timeout: int = 240, tries: int = 5) -> list[str]:
    """POST a generation request to Ark; return the list of result image URLs.

    Retries transient failures (HTTP 429/5xx, connect timeouts/URLErrors) up to
    ``tries`` times with a 5 s pause between. On final failure raises
    ``RuntimeError`` carrying the last server message. Never fabricates a result.
    """
    body = {
        "model": image_model(),
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
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


def download_bytes(url: str, *, timeout: int = 120, tries: int = 5,
                   max_bytes: int = _MAX_IMAGE_BYTES) -> bytes:
    """GET ``url`` and return the raw bytes (capped at ``max_bytes`` so a runaway
    or malformed response can't exhaust memory), retrying transient errors 5 × 5 s.
    Raises ``RuntimeError`` carrying the last error on final failure, or if the
    body exceeds the cap."""
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
