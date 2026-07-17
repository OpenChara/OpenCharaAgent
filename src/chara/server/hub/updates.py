"""Software update + changelog — coupled to GitHub Releases (the mature, standard way).

We READ the repo's GitHub Releases (tag + markdown notes) as both the changelog and the
latest-version signal, and APPLY via the SAME channel-aware steps as ``chara update``:
a dev/git checkout = ``git pull --ff-only`` + ``uv sync``; a wheel install = ``uv tool
upgrade chara``. We only ever CHECK + surface — never auto-update, never default-update.

The release fetch is cached (GitHub's unauthenticated rate limit is 60/hr) in the same
``update_check.json`` stamp the CLI already uses, extended with the release list.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import Any

from ... import __version__
from ... import updater as _updater
from ...session import sessions as S

_APP_DIR = _updater.APP_DIR  # repo checkout (dev) / install dir — same root as the core
_CACHE_TTL = 3600.0  # GitHub unauth limit is 60/hr — cache the release fetch
_TIMEOUT = _updater._TIMEOUT


def _stamp_path() -> Path:
    return S.chara_home() / "update_check.json"


def _is_dev() -> bool:
    return _updater.is_dev()


def _norm(v: str) -> tuple[int, ...]:
    """A version tag → comparable tuple: 'v0.1.1' / '0.1.1' → (0, 1, 1)."""
    parts = []
    for seg in str(v or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _fetch_releases() -> list[dict[str, Any]]:
    """Release list (newest first, each with a ``wheel_url``) — the shared self-update
    core (gh CLI → unauth HTTP). Kept as a thin alias so status()'s caching is unchanged."""
    return _updater.fetch_releases(timeout=_TIMEOUT)


def _commits_behind() -> int | None:
    """Dev channel only: commits on origin/main beyond HEAD (a checkout can be behind
    main without a tagged release). None when not a git checkout / git is unreachable."""
    git = shutil.which("git")
    if not git or not _is_dev():
        return None
    try:
        subprocess.run([git, "-C", str(_APP_DIR), "fetch", "--quiet", "origin", "main"],
                       timeout=_TIMEOUT, check=True, capture_output=True)
        out = subprocess.run([git, "-C", str(_APP_DIR), "rev-list", "--count", "HEAD..origin/main"],
                             timeout=_TIMEOUT, check=True, capture_output=True, text=True)
        return int(out.stdout.strip())
    except Exception:  # noqa: BLE001 - fail silent, the check is best-effort
        return None


def _read_stamp() -> dict[str, Any]:
    try:
        data = json.loads(_stamp_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_stamp(data: dict[str, Any]) -> None:
    try:
        S.chara_home().mkdir(parents=True, exist_ok=True)
        _stamp_path().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def status(force: bool = False) -> dict[str, Any]:
    """Current version + channel + latest release + changelog (release notes). Cached for
    an hour unless ``force``; a fetch failure falls back to the cached releases."""
    cached = _read_stamp()
    fresh = (time.time() - float(cached.get("checked_at") or 0)) < _CACHE_TTL
    if fresh and not force and "releases" in cached:
        releases = cached.get("releases") or []
        behind = cached.get("behind")
    else:
        try:
            releases = _fetch_releases()
        except (urllib.error.URLError, OSError, ValueError, TimeoutError,
                subprocess.TimeoutExpired):
            releases = cached.get("releases") or []  # offline / rate-limited → keep last known
        behind = _commits_behind()
        _write_stamp({"t": time.time(), "checked_at": time.time(),
                      "behind": behind if behind is not None else cached.get("behind", 0),
                      "releases": releases})
    channel = "dev" if _is_dev() else "wheel"
    latest = releases[0]["tag"] if releases else ""
    newer_tag = bool(latest) and _norm(latest) > _norm(__version__)
    update_available = (bool(behind and behind > 0) or newer_tag) if channel == "dev" else newer_tag
    return {
        "current": __version__,
        "channel": channel,
        "latest": latest,
        "behind": int(behind) if behind is not None else 0,
        "update_available": update_available,
        "releases": releases,
        "checked_at": _read_stamp().get("checked_at") or time.time(),
        # The by-hand command — always present so the UI can offer it as the fallback
        # when the in-app update can't run (the AstrBot pattern).
        "manual_command": _updater.manual_command(),
    }


def apply() -> dict[str, Any]:
    """Run the channel-aware in-place update via the shared self-update core. BLOCKING
    (callers run it off the event loop). Returns ``{ok, output, restart_required}`` —
    the running process keeps the OLD code until it restarts, so the UI says to restart.

    The wheel channel reinstalls from the LATEST release wheel URL (``uv tool upgrade``
    is a no-op on a URL-pinned tool — the reason the button never upgraded)."""
    result = _updater.apply()
    if result.get("ok"):
        # Force a fresh check on next status() (clear the cache age), keep last release list.
        _write_stamp({"t": time.time(), "checked_at": 0, "behind": 0,
                      "releases": _read_stamp().get("releases", [])})
    return result
