"""Local matting (抠像) model management — select / download / load BiRefNet &
friends from Settings. This is the cutout step of the in-app visuals pipeline (R9).

``rembg`` + ``onnxruntime`` are a HEAVY optional dependency
(``uv sync --extra visuals``); this module imports them LAZILY (inside
:func:`cut`) so importing the module always works, with or without the extra.

Model weights are large ``.onnx`` files published in rembg's GitHub release. We
download them into rembg's OWN cache dir (``~/.u2net`` / ``U2NET_HOME``) under the
exact filename ``<model_id>.onnx`` that ``rembg.new_session(model_id)`` reads — so
a model downloaded here is reused by rembg at runtime and never re-fetched.

No fabrication, no silent fallback: a missing dependency, a failed download, or a
size-mismatched file is a VISIBLE error. The size of each ``.onnx`` is pinned and
verified after download (a truncated/HTML-error body is rejected, never kept).
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_REL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0"


@dataclass(frozen=True)
class MatteModel:
    id: str          # rembg model id == the cached filename stem (<id>.onnx)
    label: str
    url: str
    size: int        # exact byte size — integrity check + download progress total
    note: str


# The subjects-by-content matters useful for cutting a generated 立绘/sticker.
# size values are the EXACT release-asset byte counts (verified against the rembg
# v0.0.0 release + the on-disk cache), used both for the integrity check and as
# the progress denominator.
MODELS: dict[str, MatteModel] = {
    "birefnet-general": MatteModel(
        "birefnet-general", "BiRefNet General",
        f"{_REL}/BiRefNet-general-epoch_244.onnx", 972666916,
        "Flagship: SOTA hair / fine-edge matting. ~0.97 GB — heaviest, sharpest. The default."),
    "birefnet-general-lite": MatteModel(
        "birefnet-general-lite", "BiRefNet General Lite",
        f"{_REL}/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx", 224005088,
        "Near-flagship quality at ~0.21 GB — the lighter option for a laptop."),
}

DEFAULT_MODEL = "birefnet-general"


# --- paths --------------------------------------------------------------------

def _home() -> Path:
    return Path(os.getenv("LUNAMOTH_HOME", str(Path.home() / ".lunamoth")))


def matte_home() -> Path:
    """The rembg model cache dir: ``U2NET_HOME`` if set, else ``~/.u2net``. The
    SAME dir ``rembg.new_session`` reads — so a model downloaded here is reused."""
    env = (os.getenv("U2NET_HOME") or "").strip()
    return Path(env) if env else (Path.home() / ".u2net")


def model_path(model_id: str) -> Path:
    return matte_home() / f"{model_id}.onnx"


def _desktop_json() -> dict:
    """The global keyring/defaults at ``~/.lunamoth/desktop.json`` (stdlib-only;
    this module must not import ``server/``)."""
    try:
        raw = json.loads((_home() / "desktop.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def selected_model() -> str:
    """The active matte model. Order: env ``LUNAMOTH_MATTE_MODEL`` → the global
    keyring ``matte_model`` (set in Settings·生图) → the bundled default. An
    unknown id falls through to the default (never trusted blindly)."""
    m = (os.getenv("LUNAMOTH_MATTE_MODEL") or "").strip()
    if m in MODELS:
        return m
    m = str(_desktop_json().get("matte_model") or "").strip()
    if m in MODELS:
        return m
    return DEFAULT_MODEL


# --- install state ------------------------------------------------------------

def is_installed(model_id: str) -> bool:
    """True only when the file exists AND its size matches the pinned byte count —
    a half-downloaded or wrong file reads as not-installed (no false 'ready')."""
    spec = MODELS.get(model_id)
    if spec is None:
        return False
    try:
        return model_path(model_id).stat().st_size == spec.size
    except OSError:
        return False


def deps_available() -> bool:
    """True when the optional ML stack (rembg) can be imported — gates the
    Settings UI and :func:`cut`. Never raises."""
    try:
        import rembg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — any import failure means 'not available'
        return False


# --- optional-deps install (background; `uv sync --extra visuals`) ------------
# The visuals extra (rembg/onnxruntime) is heavy + platform-specific, so it is
# installed on demand from the Settings UI rather than bundled. We run the
# project's own `uv sync --extra visuals` (respects the lock, no pip/lock drift)
# in the project root (config.ROOT = the installed app dir or the dev checkout)
# and surface a coarse installing/done/error state (uv gives no per-step %).
_deps_progress: dict = {}


def deps_progress() -> dict:
    with _lock:
        return dict(_deps_progress)


def _uv_bin() -> str:
    import shutil
    return shutil.which("uv") or str(Path.home() / ".lunamoth" / "bin" / "uv")


def install_deps_async() -> dict:
    """Start `uv sync --extra visuals` in the background (idempotent). The hub
    polls :func:`status` (which carries ``deps_progress``). Never raises."""
    with _lock:
        if deps_available():
            _deps_progress.clear(); _deps_progress.update({"state": "done"})
            return dict(_deps_progress)
        if _deps_progress.get("state") == "installing":
            return dict(_deps_progress)
        _deps_progress.clear(); _deps_progress.update({"state": "installing"})

    def _run() -> None:
        import subprocess
        from ..config import ROOT
        try:
            uv = _uv_bin()
            if not (Path(uv).exists() or __import__("shutil").which("uv")):
                raise RuntimeError("uv not found — install.sh provides one")
            proc = subprocess.run(
                [uv, "sync", "--extra", "visuals"], cwd=str(ROOT),
                capture_output=True, text=True, timeout=1800,
            )
            ok = proc.returncode == 0 and deps_available()
            with _lock:
                _deps_progress.clear()
                if ok:
                    _deps_progress.update({"state": "done"})
                else:
                    tail = (proc.stderr or proc.stdout or "install failed").strip()[-400:]
                    _deps_progress.update({"state": "error", "error": tail})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the hub
            with _lock:
                _deps_progress.clear(); _deps_progress.update({"state": "error", "error": str(exc)[:400]})

    threading.Thread(target=_run, daemon=True).start()
    return dict(_deps_progress)


# --- download (background, polled progress) -----------------------------------

_progress: dict[str, dict] = {}
_active: set[str] = set()
_lock = threading.Lock()


def progress(model_id: str) -> dict | None:
    with _lock:
        p = _progress.get(model_id)
        return dict(p) if p else None


def download(model_id: str, *, progress_cb=None, chunk: int = 1 << 20) -> Path:
    """Stream the model ``.onnx`` into the rembg cache, verifying the final size.
    Synchronous; raises on any failure (no partial file is kept). Returns the path.
    A re-download of an already-installed model is a no-op."""
    if model_id not in MODELS:
        raise KeyError(f"unknown matte model: {model_id}")
    dst = model_path(model_id)
    if is_installed(model_id):
        return dst
    spec = MODELS[model_id]
    matte_home().mkdir(parents=True, exist_ok=True)
    part = dst.with_name(f"{model_id}.onnx.part")
    done = 0
    try:
        req = urllib.request.Request(spec.url, headers={"User-Agent": "lunamoth"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, spec.size)
        got = part.stat().st_size
        if got != spec.size:
            raise RuntimeError(
                f"matte download size mismatch for {model_id}: "
                f"got {got} bytes, expected {spec.size}")
        os.replace(part, dst)
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return dst


def download_async(model_id: str) -> dict:
    """Start a background download (idempotent while one is running) and return
    this model's progress record. The hub polls :func:`status`/:func:`progress`."""
    if model_id not in MODELS:
        raise KeyError(f"unknown matte model: {model_id}")
    with _lock:
        if model_id in _active:
            return dict(_progress.get(model_id) or {})
        _active.add(model_id)
        _progress[model_id] = {"state": "downloading", "done": 0,
                               "total": MODELS[model_id].size}

    def _cb(done: int, total: int) -> None:
        with _lock:
            rec = _progress.get(model_id)
            if rec is not None:
                rec["done"], rec["total"] = done, total

    def _run() -> None:
        try:
            download(model_id, progress_cb=_cb)
            with _lock:
                _progress[model_id] = {"state": "done",
                                       "done": MODELS[model_id].size,
                                       "total": MODELS[model_id].size}
        except Exception as e:  # noqa: BLE001 — surfaced to the UI, never swallowed
            with _lock:
                _progress[model_id] = {"state": "error", "error": str(e)}
        finally:
            with _lock:
                _active.discard(model_id)

    threading.Thread(target=_run, name=f"matte-dl-{model_id}", daemon=True).start()
    return dict(_progress[model_id])


def delete(model_id: str) -> bool:
    """Remove a downloaded model file. Returns True if a file was removed."""
    try:
        model_path(model_id).unlink()
        return True
    except OSError:
        return False


def status() -> dict:
    """The full Settings·生图 matte payload: deps presence, the cache dir, and
    every model's install/active/in-progress state."""
    active = selected_model()
    return {
        "deps": deps_available(),
        "deps_progress": deps_progress(),
        "home": str(matte_home()),
        "active": active,
        "models": [
            {
                "id": m.id, "label": m.label, "note": m.note, "size": m.size,
                "installed": is_installed(m.id),
                "active": m.id == active,
                "progress": progress(m.id),
            }
            for m in MODELS.values()
        ],
    }


# --- runtime matte (used by R9; lazy heavy imports) ---------------------------

def cut(src, *, model_id: str | None = None, despill: bool = True) -> bytes:
    """Cut the subject out of *src* (a path, PIL image, or raw image bytes) and
    return PNG (RGBA) bytes. Lazily imports rembg/PIL/numpy — a missing extra is a
    clear error.

    Mirrors the dev pipeline's flagship matte (full-res, post-processed mask,
    optional green-spill suppression for chroma-background sources)."""
    mid = model_id or selected_model()
    try:
        import io

        import numpy as np
        from PIL import Image
        from rembg import new_session, remove
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "matting needs the optional visuals stack — install it with "
            "`uv sync --extra visuals` (rembg/onnxruntime)."
        ) from e

    if not is_installed(mid):
        raise RuntimeError(
            f"matte model '{mid}' is not downloaded yet — download it in "
            "Settings·生图 first.")

    if isinstance(src, (bytes, bytearray)):
        image = Image.open(io.BytesIO(bytes(src)))
    elif hasattr(src, "convert"):
        image = src
    else:
        image = Image.open(src)
    out = remove(image.convert("RGBA"), session=new_session(mid),
                 post_process_mask=True)
    if despill:
        a = np.asarray(out).astype(np.int16)
        r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
        over = (al > 0) & (g > r) & (g > b)
        a[..., 1][over] = np.maximum(r, b)[over]
        out = Image.fromarray(a.astype(np.uint8), "RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
