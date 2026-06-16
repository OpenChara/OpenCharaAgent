"""generate_image — the chara makes an image from a text prompt and saves it
into its own workspace (under ``works/`` by default, where the operator sees it).

A LunaMoth chara-life capability with no hermes counterpart. It mirrors the
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

import time

from ..registry import registry, tool_error, tool_result
from ._image_gen import ark_generate, download_bytes, image_key, is_image_bytes


def _check_image_key() -> bool:
    """check_fn: the tool is only offered when an Ark image key is configured."""
    return bool(image_key())


def generate_image(args, ctx) -> str:
    if not ctx.network_on():
        return tool_error(
            "image generation needs the network — it's off. Ask the operator to "
            "enable it (/net on) first."
        )
    if not image_key():
        return tool_error(
            "no image key configured — set ARK_API_KEY (env) or "
            "~/.lunamoth/ark_api_key before generating images."
        )

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("generate_image needs a `prompt`")

    size = str(args.get("size") or "2048x2048").strip() or "2048x2048"
    path = str(args.get("path") or "").strip()
    if not path:
        path = f"works/image-{int(time.time())}.png"

    try:
        urls = ark_generate(prompt, size)
        if not urls:
            return tool_error("image generation returned no result")
        data = download_bytes(urls[0])
        # Don't save a non-image body (e.g. an error page returned at HTTP 200)
        # as a fake ".png" and report success — reject it as a visible failure.
        if not is_image_bytes(data):
            return tool_error(
                "the generation endpoint did not return an image "
                "(got a non-image response); nothing was saved"
            )
        saved = ctx.sandbox.write_bytes(path, data)
    except Exception as e:  # noqa: BLE001 — visible failure, never fake success
        return tool_error(str(e))

    return tool_result(
        ok=True, path=saved, size=size, bytes=len(data),
        note="saved in your workspace — show it with send_file",
    )


SCHEMA = {
    "description": (
        "Generate an image from a text prompt and save it into your workspace "
        "(under works/ by default, where your user can see it). Returns the saved "
        "path — show it with send_file. Needs the network on and an image key "
        "configured."
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
        },
        "required": ["prompt"],
    },
}

registry.register(
    "generate_image", "media", SCHEMA, generate_image,
    check_fn=_check_image_key, emoji="🎨", max_result_size_chars=4000,
)
