"""generate_image — the chara makes an image from a text prompt and saves it
into its own workspace (under ``works/`` by default, where the operator sees it).

A OpenCharaAgent chara-life capability with no hermes counterpart. It mirrors the
``web.py`` gating pattern exactly:

- ``check_fn`` (``_check_image_key``) is a STATIC capability gate: it takes no
  args and can't see ``ctx``, so it only answers "is an image key configured?".
  A chara with no key never sees the tool at all → no surprise spend.
- the per-call network check (``if not ctx.network_on()``) lives in the HANDLER,
  because the network toggle is runtime state on ``ctx``.

No failure fallbacks: a generation/download/save error surfaces as a visible
``tool_error`` carrying the real message — never a fabricated success.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid

from ..registry import registry, tool_error, tool_result
from ..sandbox import SandboxViolation
from ._image_gen import generate_bytes, image_key
from ._process_registry import get_registry

# How many reference images the chara may attach to one generation. Matches the
# providers' practical cap (DashScope allows 1-4; Ark takes a small ref list).
_MAX_REFS = 4


def _check_image_key() -> bool:
    """check_fn: the tool is only offered when the active image provider has a key."""
    return bool(image_key())


def _data_uri(data: bytes) -> str | None:
    """A reference image's bytes → a ``data:<mime>;base64,…`` URI the image API
    accepts, or None if the bytes aren't a PNG/JPEG/WebP (detected by magic bytes,
    so a mislabeled extension can't slip a non-image through)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _resolve_refs(ctx, raw) -> tuple[list[str], str]:
    """Resolve the chara-given reference image PATHS into data-URIs for the image
    API — the model hands over workspace paths and the tool reads/encodes them
    itself. Returns ``(refs, error)``: on any bad path, ``refs`` is ``[]`` and
    ``error`` is a message to surface (no fabrication — a missing/!image path is a
    visible tool error, never silently dropped). ``assets/<name>`` resolves to the
    chara's own card art shelf, so it can use its own portrait/sprite as a ref."""
    if raw is None:
        return [], ""
    paths = [raw] if isinstance(raw, str) else raw
    if not isinstance(paths, list):
        return [], "reference_images must be a path string or a list of path strings"
    paths = [str(p).strip() for p in paths if str(p).strip()]
    if not paths:
        return [], ""
    if len(paths) > _MAX_REFS:
        return [], f"too many reference images ({len(paths)}) — at most {_MAX_REFS}"
    refs: list[str] = []
    for p in paths:
        try:
            fp = ctx.sandbox.resolve_readable(p)
        except SandboxViolation as e:
            return [], f"reference image path not allowed: {p} ({e})"
        if not fp.exists() or not fp.is_file():
            return [], f"reference image not found: {p}"
        try:
            uri = _data_uri(fp.read_bytes())
        except OSError as e:
            return [], f"could not read reference image {p}: {e}"
        if not uri:
            return [], f"reference image isn't a PNG/JPEG/WebP: {p}"
        refs.append(uri)
    return refs, ""


def _run_image_job(reg, ctx, job_id: str, prompt: str, size: str, path: str,
                   refs: list[str] | None = None) -> None:
    """Background worker: generate + save, then push a completion event onto the
    process registry's queue (the agent drains it at the next turn boundary). Never
    raises (it runs on a daemon thread) — a failure is reported, never fabricated.
    ``refs`` are pre-resolved data-URI reference images (already validated by the
    handler) that guide the generation, e.g. so the chara itself appears in shot."""
    # Re-check the network at run time: the operator may have run /net off between
    # submit and this (possibly minutes-later) call. Never make the HTTP request
    # if the network is now off — report a failure instead.
    if not ctx.network_on():
        reg.completion_queue.put({
            "type": "image_gen", "session_id": job_id, "status": "failed",
            "error": "network turned off before generation ran (ask the operator "
                     "for /net on, then retry)",
            "prompt": prompt[:120],
        })
        return
    try:
        data = generate_bytes(prompt, size, refs=refs or None)
        saved = ctx.sandbox.write_bytes(path, data)
        reg.completion_queue.put({
            "type": "image_gen", "session_id": job_id, "status": "ready",
            "path": saved, "bytes": len(data), "prompt": prompt[:120],
        })
    except Exception as e:  # noqa: BLE001 — report the real failure via the queue
        reg.completion_queue.put({
            "type": "image_gen", "session_id": job_id, "status": "failed",
            "error": str(e)[:300], "prompt": prompt[:120],
        })


def generate_image(args, ctx) -> str:
    if not ctx.network_on():
        return tool_error(
            "image generation needs the network — it's off. Ask the operator to "
            "enable it (/net on) first."
        )
    if not image_key():
        return tool_error(
            "no image key configured — pick an image provider/model in "
            "Settings · 模型 · 生图模型 and add that provider's key in Settings · 提供商."
        )

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("generate_image needs a `prompt`")

    size = str(args.get("size") or "2048x2048").strip() or "2048x2048"
    path = str(args.get("path") or "").strip()
    if not path:
        path = f"works/image-{int(time.time())}.png"

    # Resolve any reference images NOW (synchronously) so a bad path is a visible
    # error here, not a silent miss minutes later in the background job. The model
    # hands over workspace paths; the tool reads + encodes them into the refs the
    # image API guides on (e.g. the chara's own portrait so it appears in shot).
    refs, ref_err = _resolve_refs(ctx, args.get("reference_images"))
    if ref_err:
        return tool_error(ref_err)

    # Generation runs in the BACKGROUND (some providers — e.g. async DashScope —
    # take minutes; a synchronous call would freeze the whole tool loop). Return
    # immediately; the worker pushes a completion event onto the process registry's
    # queue, which the agent drains at the next turn boundary and surfaces to the
    # model (hermes' background-job notification shape).
    job_id = f"img-{uuid.uuid4().hex[:8]}"
    reg = get_registry(ctx)
    threading.Thread(
        target=_run_image_job, args=(reg, ctx, job_id, prompt, size, path, refs),
        name=f"imggen-{job_id}", daemon=True,
    ).start()

    return tool_result(
        ok=True, status="submitted", job_id=job_id, path=path, size=size,
        note=(
            "Image generation is running in the BACKGROUND (it can take a while — "
            "some providers are slow). Don't wait on it; continue what you were "
            "doing. You'll be notified automatically when it's ready or if it fails. "
            f"Do NOT show it yet — wait for the ready notification before writing MEDIA:{path}."
        ),
    )


SCHEMA = {
    "description": (
        "Generate an image from a text prompt and save it into your workspace "
        "(under works/ by default, where your user can see it). Runs in the "
        "BACKGROUND: this returns immediately ('submitted') with the intended path; "
        "you are notified automatically when the image is ready (or if it failed). "
        "Don't block waiting — keep working. Once you get the ready notification, "
        "show it to your user by writing a line MEDIA:<path> in your reply. "
        "IF YOU YOURSELF (or another known character) SHOULD APPEAR IN THE IMAGE, "
        "pass that character's reference picture via `reference_images` so the "
        "result actually looks like them — e.g. your own portrait/sprite, or a "
        "photo from a place you're visiting. You give the file PATH(s); the tool "
        "reads and attaches them. "
        "Prefer NOT to generate images unprompted — it spends your user's image credits, "
        "so generate when they've asked for one or clearly agreed, rather than on your own "
        "initiative without their go-ahead. "
        "Needs the network on and an image key configured."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to draw, in your own words.",
            },
            "size": {
                "type": "string",
                "description": "Image size as WIDTHxHEIGHT (default 2048x2048).",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative save path (default works/image-<time>.png).",
            },
            "reference_images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional workspace-relative PATHS to reference images that guide "
                    f"the look of the result (up to {_MAX_REFS}; PNG/JPEG/WebP). Give "
                    "the file paths, not the image data — the tool reads them itself. "
                    "Use this whenever a specific character must appear and look right: "
                    "to put YOURSELF in the picture, pass your own reference image (your "
                    "card art lives under assets/, e.g. assets/sprite.png; images you "
                    "saved or were sent live under your workspace, e.g. uploads/…). "
                    "Without a reference the model only has your text description to go on."
                ),
            },
        },
        "required": ["prompt"],
    },
}

registry.register(
    "generate_image", "media", SCHEMA, generate_image,
    check_fn=_check_image_key, emoji="🎨", max_result_size_chars=4000,
)
