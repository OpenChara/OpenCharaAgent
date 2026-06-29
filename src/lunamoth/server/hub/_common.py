"""Shared leaf helpers for the hub package — depended on by every submodule.

This module imports nothing from its hub siblings, so it can sit at the bottom
of the dependency graph and break would-be cycles.
"""
from __future__ import annotations

import colorsys
import functools
import hashlib
import json
import os
import re
import threading
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...session import sessions as S
from ..dispatch import RpcError
from .config import user_cards_dir


class HubRpcError(RpcError):
    """Hub-scoped JSON-RPC error that may carry machine-readable error data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None):
        super().__init__(code, message)
        self.data = data


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


# Per-path locks so every read-modify-write of one card.json / config.json serializes.
# The concurrency is real on a single desktop: async image-job completions run on daemon
# threads (visuals/jobs.py) and field auto-save (card.patch) runs on the RPC worker pool —
# 一键生成全部 fans out several at once, all mutating the same card.json. Without this an
# interleave silently drops a gallery entry, and a torn config.json write can drop the
# provider api_key. The lock covers the WHOLE RMW; the write half uses _atomic_write_json.
_PATH_LOCKS: dict[str, "threading.Lock"] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Any) -> "threading.Lock":
    try:
        key = str(Path(str(path)).resolve())
    except (OSError, ValueError, TypeError):
        key = str(path)
    with _PATH_LOCKS_GUARD:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = _PATH_LOCKS[key] = threading.Lock()
        return lk


@contextmanager
def card_write_lock(path: Any):
    """Hold the per-path lock for a manual read-modify-write block (e.g. a config.json
    edit). Pair the write with _atomic_write_json."""
    with _path_lock(path):
        yield


def locked_card_write(fn):
    """Serialize a card-mutating function on its card path (the `path` kwarg, else the
    first positional arg), covering the full read-modify-write so concurrent writers can't
    lose an update or read a torn file. The write half must use _atomic_write_json."""
    @functools.wraps(fn)
    def wrap(*args: Any, **kwargs: Any):
        path = kwargs.get("path")
        if path is None and args:
            path = args[0]
        with _path_lock(path or ""):
            return fn(*args, **kwargs)
    return wrap


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str, fallback: str = "chara") -> str:
    s = _SLUG_RE.sub("-", name).strip("-._")
    if not s or not S.valid_name(s):
        s = fallback
    return s[:48]


# Managed art-asset sidecars beside a card: `<stem>.<kind>[.<id>].<ext>` images the
# visuals editor owns. The deck scan, the asset library and the wake copier all key on
# this ONE marker set + predicate so they can never drift apart.
SIDECAR_MARKERS = (".avatar.", ".sprite.", ".background.", ".keyvisual.",
                   ".sticker.", ".sticker_sheet.")


def is_managed_sidecar_name(name: str) -> bool:
    low = str(name or "").lower()
    return any(m in low for m in SIDECAR_MARKERS)


def _meta(p: dict[str, Any]) -> S.SessionMeta:
    name = str(p.get("name") or "")
    meta = S.load_session(name)
    if meta is None:
        raise RpcError(-32004, f"no chara named {name!r}")
    return meta


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


# ---- avatar-SVG safety (shared by card listing/sanitize and avatar upload) ----
_SVG_MAX_CHARS = 1500
_SVG_EVENT_ATTR_RE = re.compile(r"\son[a-zA-Z0-9_.:-]*\s*=")
_SVG_EXTERNAL_REF_RE = re.compile(r"""\b(?:href|xlink:href)\s*=\s*["']\s*(?!#)[^"']+["']|url\(\s*["']?\s*(?!#)[^)]+""",
                                  re.IGNORECASE)
_SVG_SCRIPT_RE = re.compile(r"<\s*/?\s*script(?:\s|>|/)", re.IGNORECASE)
_SVG_FOREIGN_RE = re.compile(r"<\s*/?\s*foreignobject(?:\s|>|/)", re.IGNORECASE)
_SVG_TEXT_RE = re.compile(r"<\s*/?\s*text(?:\s|>|/)", re.IGNORECASE)
_SVG_VIEWBOX_RE = re.compile(r"""\bviewbox\s*=\s*["']0\s+0\s+64\s+64["']""", re.IGNORECASE)


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


# ---- theme color normalization (shared by card sanitize/UI and draft assembly) -
_THEME_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _clean_theme_color(value: Any) -> str:
    if isinstance(value, str) and _THEME_RE.match(value.strip()):
        return value.strip().upper()
    return ""


def _clean_theme(value: Any, legacy: Any = None) -> dict[str, str]:
    """Normalize the dual theme `{primary, secondary}`; back-compat with the
    legacy single `theme_color`. Returns only the keys that have a valid color
    (an empty dict when nothing is set).

    INVARIANT: a theme MUST have a primary — a secondary alone is dropped. A
    secondary-only dict used to slip through every write path (the editor's
    secondary picker patched just `{secondary}` onto a primary-less card) and
    crashed the card view with `KeyError: 'primary'`. Enforcing it here, the one
    chokepoint all writes pass through, makes the bad state impossible to persist
    and heals legacy cards on their next sanitize."""
    primary = ""
    secondary = ""
    if isinstance(value, dict):
        primary = _clean_theme_color(value.get("primary"))
        secondary = _clean_theme_color(value.get("secondary"))
    if not primary:
        primary = _clean_theme_color(legacy)
    if not primary:
        return {}
    out: dict[str, str] = {"primary": primary}
    if secondary:
        out["secondary"] = secondary
    return out


# ---- deterministic per-card theme (shared by card import paths: market + paste) ----
SEEDED_THEME_DEFAULT = "#5B9FD4"  # deck signature blue — the fallback for a seedless theme


def _theme_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def seeded_theme(seed: str) -> dict[str, str]:
    """A stable, pleasant {primary, secondary} derived from a card's identity, so an
    imported card that carries no theme still gets a distinct, valid color — never the
    same flat blue for every import, and never a missing primary (which crashes the deck).
    The seed is anything stable per card (its source path or name)."""
    seed = str(seed or "").strip()
    if not seed:
        return {"primary": SEEDED_THEME_DEFAULT, "secondary": ""}
    h = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16)
    hue = (h % 360) / 360.0
    primary = _theme_hex(colorsys.hls_to_rgb(hue, 0.60, 0.55))
    secondary = _theme_hex(colorsys.hls_to_rgb((hue + 35 / 360.0) % 1.0, 0.55, 0.50))
    return {"primary": primary, "secondary": secondary}


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
    # Return the RESOLVED path so callers act on the same path the check validated (no
    # check/act asymmetry if `path` is a symlink).
    if user_cards_dir().resolve() in rp.parents:
        return rp
    # A frozen session card lives at <sessions>/<name>/card.json (exactly one
    # level deep). Sidecars the asset RPCs write land beside it, inside the
    # session dir — confined. This is what lets the chat Visuals tab edit the
    # active chara's own card.
    sessions = S.sessions_dir().resolve()
    if rp.name == "card.json" and rp.parent.parent == sessions:
        return rp
    raise RpcError(-32031, "only a deck card or a chara's own session card can be edited")
