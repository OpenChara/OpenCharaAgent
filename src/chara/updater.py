"""Self-update core — resolve the latest GitHub Release and reinstall IN PLACE.

Flat / backend-neutral (like config.py) so the CLI (``front/cli.py``) and the hub
update RPC (``server/hub/updates.py``) share ONE implementation instead of two that
drift. Two install channels mirror ``install.sh``:

  * dev / git checkout  → ``git pull --ff-only`` + ``uv sync``.
  * wheel (the default) → installed by ``uv tool install --force
    "chara[...] @ <release-wheel-URL>"`` — a URL-PINNED tool. The mature in-place
    upgrade is therefore "fetch the LATEST release's wheel URL + reinstall", NOT
    ``uv tool upgrade`` (a no-op on a URL-pinned tool: the recorded requirement is a
    fixed URL with no newer version to resolve — the reason the update button never
    actually upgraded).

uv is located via ``config.find_uv`` (a desktop launch doesn't inherit the shell
PATH, so plain ``which`` misses it). Failures surface verbatim — no fake success.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import find_uv

REPO = "OpenChara/OpenCharaAgent"
EXTRAS = "server,messaging"  # mirror install.sh's `opencharaagent[server,messaging]`
_INSTALL_URL = f"https://raw.githubusercontent.com/{REPO}/main/install.sh"
_RELEASES_PATH = f"repos/{REPO}/releases?per_page=10"
_RELEASES_API = f"https://api.github.com/{_RELEASES_PATH}"
_TIMEOUT = 6.0
_APPLY_TIMEOUT = 300.0  # ONE wall-clock budget for the WHOLE apply() (download + install),
# under the webui's 320s update.apply RPC timeout → a clean error, not an orphaned install
# Socket-level timeout for the wheel/SHA256SUMS downloads (it fires on a STALL,
# not on total elapsed time). Whatever wall time the downloads consume is deducted
# from the install step's timeout in apply() (floor 60s), so download + install
# can't stack two full timeouts past the webui's RPC ceiling.
_DOWNLOAD_TIMEOUT = 120.0

# The install dir / checkout root: src/chara/updater.py → parents[2]. For a dev
# install this is the git checkout (has .git); for a wheel it's inside the uv tool
# venv (no .git) — the same root server/hub/updates.py and install.sh reason about.
APP_DIR = Path(__file__).resolve().parents[2]


def is_dev() -> bool:
    return (APP_DIR / ".git").exists()


def manual_command() -> str:
    """The copy-paste command to update BY HAND — the always-works fallback when the
    in-app update fails (AstrBot hands package-manager installs the command rather than
    doing in-place surgery; same idea). A wheel re-runs install.sh (full re-resolve +
    `uv tool install --force` + browser/ffmpeg setup); a dev checkout pulls + syncs."""
    if is_dev():
        return f"cd {APP_DIR} && git pull --ff-only origin main && uv sync"
    return f"curl -fsSL {_INSTALL_URL} | bash"


def fetch_releases(timeout: float = _TIMEOUT) -> list[dict[str, Any]]:
    """Releases (newest first), each with a ``wheel_url`` (its .whl asset). The repo is
    PUBLIC, so anonymous HTTP is all the update needs — gh is NOT required. gh is used
    only WHEN it already happens to be installed, to dodge the 60/hr anon rate limit.
    Raises on a hard fetch failure (caller decides whether to fall back to a cache)."""
    data: Any = None
    gh = shutil.which("gh")
    if gh:
        try:
            p = subprocess.run([gh, "api", _RELEASES_PATH],
                               capture_output=True, text=True, timeout=timeout)
            if p.returncode == 0:
                data = json.loads(p.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            data = None
    if data is None:
        req = urllib.request.Request(
            _RELEASES_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "chara"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed GitHub URL
            data = json.loads(r.read().decode("utf-8"))
    out: list[dict[str, Any]] = []
    for rel in data if isinstance(data, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        wheel = ""
        sums = ""
        for a in rel.get("assets") or []:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "")
            asset_url = str(a.get("browser_download_url") or "")
            if not wheel and name.endswith(".whl"):
                wheel = asset_url
            elif not sums and (name == "SHA256SUMS" or name.endswith(".sha256")):
                sums = asset_url  # the checksum manifest release.yml publishes next to the wheel
        out.append({
            "tag": str(rel.get("tag_name") or ""),
            "name": str(rel.get("name") or rel.get("tag_name") or ""),
            "body": str(rel.get("body") or ""),
            "published_at": str(rel.get("published_at") or ""),
            "url": str(rel.get("html_url") or ""),
            "prerelease": bool(rel.get("prerelease")),
            "wheel_url": wheel,
            "sums_url": sums,
        })
    return out


def _latest_wheel_assets() -> tuple[str | None, str | None]:
    """(wheel_url, sums_url) of the NEWEST release, or (None, None) if unreachable.

    Only the newest release counts — the same one ``status()`` advertises as ``latest``.
    If that release carries no wheel (notes-only, or a lagging upload), the wheel is
    None so ``apply()`` fails honestly rather than silently installing an OLDER wheel
    while the UI claimed the newer version was installed."""
    try:
        rels = fetch_releases()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError,
            subprocess.TimeoutExpired):
        return None, None
    if not rels:
        return None, None
    wheel = str(rels[0].get("wheel_url") or "") or None
    sums = str(rels[0].get("sums_url") or "") or None
    return wheel, sums


def latest_wheel_url() -> str | None:
    """The NEWEST release's wheel asset URL, or None if unreachable / it has no wheel."""
    return _latest_wheel_assets()[0]


class ChecksumMismatch(RuntimeError):
    """The wheel bytes do not match the release's published SHA256 — never install."""


def _http_get(url: str, timeout: float = _DOWNLOAD_TIMEOUT) -> bytes:
    req = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream", "User-Agent": "chara"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - release asset URL
        return r.read()


def download_verified_wheel(wheel_url: str, sums_url: str) -> tuple[Path, str]:
    """Download the wheel AND its SHA256SUMS manifest, verify, land the verified bytes
    in a temp file. Returns ``(path, sha256)``; the CALLER owns (and removes) the file.

    Mirrors install.sh's check: pick the hash for THIS wheel by basename (a SHA256SUMS
    may list several files), falling back to the manifest's sole hash. Raises
    ``ChecksumMismatch`` on any verification failure — a mismatch, or a manifest that
    carries no hash at all (a published-but-unreadable manifest is refused, never
    shrugged past) — and ``URLError``/``OSError`` on download failures."""
    blob = _http_get(wheel_url)
    sums = _http_get(sums_url).decode("utf-8", errors="replace")
    basename = wheel_url.rsplit("/", 1)[-1]
    expected = ""
    for line in sums.splitlines():
        if basename in line:
            m = re.search(r"[0-9a-fA-F]{64}", line)
            if m:
                expected = m.group(0).lower()
                break
    if not expected:
        m = re.search(r"[0-9a-fA-F]{64}", sums)
        expected = m.group(0).lower() if m else ""
    if not expected:
        raise ChecksumMismatch(
            f"the release's checksum manifest ({sums_url}) carries no SHA256 — refusing to install")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ChecksumMismatch(
            f"wheel checksum MISMATCH (expected {expected}, got {actual}) — refusing to install")
    fd, tmp = tempfile.mkstemp(prefix="chara-", suffix=".whl")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp), actual


def apply() -> dict[str, Any]:
    """Run the channel-aware in-place update. BLOCKING (run off the event loop).
    Returns ``{ok, output, restart_required, manual_command}`` — the running process
    keeps the OLD code until it restarts, so the UI tells the user to restart. Every
    failure carries the manual command so the user always has a guaranteed way out."""
    def _fail(output: str) -> dict[str, Any]:
        # Always hand back the by-hand command — the AstrBot lesson: when the
        # automatic path can't do it, tell the user exactly what to run.
        return {"ok": False, "restart_required": False, "manual_command": manual_command(),
                "output": f"{output}\n\nTo update manually, run:\n  {manual_command()}"}

    uv = find_uv()
    if uv is None:
        return _fail("uv not found — it should live in ~/.chara/bin (install.sh puts it there)")
    started = time.monotonic()  # the whole apply shares ONE _APPLY_TIMEOUT wall budget
    log: list[str] = []
    tmp_wheel: Path | None = None
    try:
        if is_dev():
            git = shutil.which("git")
            if not git:
                return _fail("git not found")
            steps = [[git, "-C", str(APP_DIR), "pull", "--ff-only", "origin", "main"],
                     [uv, "sync", "--project", str(APP_DIR)]]
        else:
            url, sums_url = _latest_wheel_assets()
            if not url:
                return _fail("could not resolve the latest release wheel — GitHub may be "
                             "unreachable or rate-limited; try again shortly")
            # Download + VERIFY before installing (same contract as install.sh: the
            # release publishes a SHA256SUMS manifest; a tampered wheel is the worst
            # case for a product that hands an LLM a shell). Install the verified
            # LOCAL bytes, never the raw URL, so what we checked is what we install.
            if sums_url:
                try:
                    tmp_wheel, digest = download_verified_wheel(url, sums_url)
                except ChecksumMismatch as e:
                    return _fail(str(e))
                except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                    return _fail(f"wheel download failed: {e}")
                log.append(f"wheel checksum verified ({digest})")
                target = str(tmp_wheel)
            else:
                # No manifest published on this release — say so plainly (install.sh
                # prints the same note) rather than implying the download was verified.
                log.append("NOTE: this release publishes no checksum — wheel integrity NOT verified.")
                target = url
            # Reinstall from the LATEST wheel — `uv tool upgrade` is a no-op on a
            # URL-pinned tool, so this is the only thing that actually moves the version.
            steps = [[uv, "tool", "install", "--force", f"opencharaagent[{EXTRAS}] @ {target}"]]
        for cmd in steps:
            # Deduct the wall time the downloads (and earlier steps) already spent, so
            # download + install together stay under _APPLY_TIMEOUT — two full budgets
            # would blow past the webui's 320s RPC ceiling. The 60s floor still lets a
            # nearly-exhausted budget fail with real subprocess output, not instantly.
            remaining = max(60.0, _APPLY_TIMEOUT - (time.monotonic() - started))
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining)
            except (subprocess.TimeoutExpired, OSError) as e:
                log.append(f"$ {' '.join(map(str, cmd))}\n{e}")
                return _fail("\n".join(log))
            log.append(f"$ {' '.join(map(str, cmd))}\n{((p.stdout or '') + (p.stderr or '')).strip()}")
            if p.returncode != 0:
                return _fail("\n".join(log))
        return {"ok": True, "output": "\n".join(log), "restart_required": True,
                "manual_command": manual_command()}
    finally:
        if tmp_wheel is not None:
            tmp_wheel.unlink(missing_ok=True)
