"""The shared self-update core (lunamoth/updater.py).

The headline regression: a wheel install is URL-PINNED (install.sh:
`uv tool install "lunamoth @ <wheel-url>"`), so `uv tool upgrade` is a no-op —
the update must REINSTALL from the latest release wheel URL. These pin that, plus
the uv-not-found and no-release error paths, and wheel-URL extraction.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from lunamoth import updater


def _completed(cmd, code=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)


def test_apply_wheel_reinstalls_from_latest_url_never_upgrade(monkeypatch):
    # No checksum manifest on the release → the honest fallback installs the URL
    # directly (with a NOTE), still via reinstall — never `uv tool upgrade`.
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets",
                        lambda: ("https://x/lunamoth-0.9.0-py3-none-any.whl", None))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _completed(cmd, 0, "installed")
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    res = updater.apply()
    assert res["ok"] and res["restart_required"]
    # reinstall from the latest URL — NOT `uv tool upgrade` (the no-op that never worked)
    assert seen["cmd"] == ["/fake/uv", "tool", "install", "--force",
                           "lunamoth[server,messaging] @ https://x/lunamoth-0.9.0-py3-none-any.whl"]
    assert "upgrade" not in seen["cmd"]
    assert "NOT verified" in res["output"]  # no manifest → say so plainly


def test_apply_wheel_verifies_checksum_and_installs_the_local_file(monkeypatch):
    """The release publishes SHA256SUMS → apply downloads BOTH, verifies, and hands
    uv the verified LOCAL bytes (never the raw URL); the temp wheel is cleaned up."""
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets",
                        lambda: ("https://x/lunamoth-1.0.0-py3-none-any.whl",
                                 "https://x/SHA256SUMS"))
    blob = b"wheel-bytes"
    digest = hashlib.sha256(blob).hexdigest()
    sums = f"{digest}  lunamoth-1.0.0-py3-none-any.whl\n".encode()
    monkeypatch.setattr(updater, "_http_get",
                        lambda url, timeout=0: blob if url.endswith(".whl") else sums)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        target = cmd[-1].split(" @ ", 1)[1]
        seen["target"] = target
        seen["bytes"] = Path(target).read_bytes()  # exists AT install time
        return _completed(cmd, 0, "installed")
    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    res = updater.apply()
    assert res["ok"] and res["restart_required"]
    assert not seen["target"].startswith("http")      # a local file, not the URL
    assert seen["bytes"] == blob                      # the exact verified bytes
    assert digest in res["output"]                    # the verification is reported
    assert not Path(seen["target"]).exists()          # temp wheel removed in finally


def test_apply_shares_one_wall_budget_across_download_and_install(monkeypatch):
    """Download + install share ONE _APPLY_TIMEOUT wall budget: the wall time the
    downloads spent is deducted from the uv step's timeout (floor 60s), so apply()
    can't stack two full timeouts past the webui's 320s update.apply RPC ceiling."""
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets",
                        lambda: ("https://x/lunamoth-1.0.0-py3-none-any.whl",
                                 "https://x/SHA256SUMS"))
    blob = b"wheel-bytes"
    digest = hashlib.sha256(blob).hexdigest()
    sums = f"{digest}  lunamoth-1.0.0-py3-none-any.whl\n".encode()

    clock = {"t": 1000.0}

    class _Clock:  # only the attribute apply() reaches (time.monotonic)
        @staticmethod
        def monotonic():
            return clock["t"]

    monkeypatch.setattr(updater, "time", _Clock)
    spent = {"per_download": 110.0}

    def slow_get(url, timeout=0):
        clock["t"] += spent["per_download"]  # each of the two downloads burns wall time
        return blob if url.endswith(".whl") else sums

    monkeypatch.setattr(updater, "_http_get", slow_get)
    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return _completed(cmd, 0, "installed")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    assert updater.apply()["ok"]
    # 220s downloading → the install step gets the REMAINING 80s, not a fresh 300
    assert seen["timeout"] == pytest.approx(updater._APPLY_TIMEOUT - 220.0)

    # budget (over)exhausted: the step still gets the 60s floor to fail with output
    clock["t"] = 1000.0
    spent["per_download"] = 170.0  # 340s total — past the whole budget
    assert updater.apply()["ok"]
    assert seen["timeout"] == 60.0


def test_apply_wheel_checksum_mismatch_never_installs(monkeypatch):
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets",
                        lambda: ("https://x/lunamoth-1.0.0-py3-none-any.whl",
                                 "https://x/SHA256SUMS"))
    sums = ("0" * 64 + "  lunamoth-1.0.0-py3-none-any.whl\n").encode()
    monkeypatch.setattr(updater, "_http_get",
                        lambda url, timeout=0: b"tampered" if url.endswith(".whl") else sums)
    calls = []
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or _completed(cmd))
    res = updater.apply()
    assert res["ok"] is False and res["restart_required"] is False
    assert calls == []                       # uv was NEVER invoked
    assert "MISMATCH" in res["output"]
    assert updater.manual_command() in res["output"]


def test_download_verified_wheel_picks_the_hash_by_basename(monkeypatch):
    blob = b"the-wheel"
    digest = hashlib.sha256(blob).hexdigest()
    manifest = ("1" * 64 + "  other-2.0.0-py3-none-any.whl\n"
                + digest + "  lunamoth-1.0.0-py3-none-any.whl\n").encode()
    monkeypatch.setattr(updater, "_http_get",
                        lambda url, timeout=0: blob if url.endswith(".whl") else manifest)
    path, sha = updater.download_verified_wheel(
        "https://x/lunamoth-1.0.0-py3-none-any.whl", "https://x/SHA256SUMS")
    try:
        assert sha == digest and path.read_bytes() == blob
    finally:
        path.unlink(missing_ok=True)


def test_download_verified_wheel_refuses_a_hashless_manifest(monkeypatch):
    monkeypatch.setattr(updater, "_http_get",
                        lambda url, timeout=0: b"w" if url.endswith(".whl") else b"no hashes here\n")
    with pytest.raises(updater.ChecksumMismatch):
        updater.download_verified_wheel("https://x/w.whl", "https://x/SHA256SUMS")


def test_apply_wheel_no_release_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets", lambda: (None, None))
    res = updater.apply()
    assert res["ok"] is False and res["restart_required"] is False
    assert "release wheel" in res["output"]


def test_apply_without_uv_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(updater, "find_uv", lambda: None)
    res = updater.apply()
    assert res["ok"] is False
    assert "uv not found" in res["output"]


def test_every_failure_hands_back_the_manual_command(monkeypatch):
    # The AstrBot lesson: when the automatic path can't run, always tell the user
    # exactly what to type. Every failure carries the manual command.
    monkeypatch.setattr(updater, "find_uv", lambda: None)
    res = updater.apply()
    assert res["ok"] is False
    assert res["manual_command"] == updater.manual_command()
    assert updater.manual_command() in res["output"]


def test_manual_command_is_channel_aware(monkeypatch):
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    assert "install.sh" in updater.manual_command()  # wheel → re-run the installer
    monkeypatch.setattr(updater, "is_dev", lambda: True)
    cmd = updater.manual_command()
    assert "git pull" in cmd and "uv sync" in cmd  # dev → pull + sync


def test_apply_dev_pulls_then_syncs(monkeypatch):
    monkeypatch.setattr(updater, "is_dev", lambda: True)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater.shutil, "which", lambda n: "/usr/bin/git" if n == "git" else None)
    cmds = []
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **kw: cmds.append(cmd) or _completed(cmd))
    res = updater.apply()
    assert res["ok"] and res["restart_required"]
    assert any("pull" in c for c in cmds) and any("sync" in c for c in cmds)


def test_apply_surfaces_a_failing_step(monkeypatch):
    monkeypatch.setattr(updater, "is_dev", lambda: False)
    monkeypatch.setattr(updater, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(updater, "_latest_wheel_assets", lambda: ("https://x/w.whl", None))
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, **kw: _completed(cmd, 1, "", "boom"))
    res = updater.apply()
    assert res["ok"] is False and res["restart_required"] is False
    assert "boom" in res["output"]


def test_fetch_releases_extracts_the_wheel_asset_url(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda n: None)  # no gh → HTTP path
    payload = json.dumps([{
        "tag_name": "v0.9.0", "name": "0.9.0", "body": "notes",
        "html_url": "https://h", "draft": False, "prerelease": False,
        "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"},
            {"name": "lunamoth-0.9.0-py3-none-any.whl", "browser_download_url": "https://x/w.whl"},
        ],
    }]).encode()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _Resp())

    rels = updater.fetch_releases()
    assert rels[0]["tag"] == "v0.9.0"
    assert rels[0]["wheel_url"] == "https://x/w.whl"  # the .whl asset, not SHA256SUMS
    assert rels[0]["sums_url"] == "https://x/SHA256SUMS"  # the checksum manifest, for apply()


def test_latest_wheel_url_uses_newest_release_only(monkeypatch):
    """Aligns with status(): if the NEWEST release has no wheel, return None (apply fails
    honestly) rather than silently reaching back to an OLDER wheel than was advertised."""
    monkeypatch.setattr(updater, "fetch_releases", lambda timeout=6.0: [
        {"tag": "v0.2.0", "wheel_url": ""},                       # newest, no wheel yet
        {"tag": "v0.1.9", "wheel_url": "https://x/old.whl"},      # older, has one
    ])
    assert updater.latest_wheel_url() is None
    monkeypatch.setattr(updater, "fetch_releases", lambda timeout=6.0: [
        {"tag": "v0.2.0", "wheel_url": "https://x/new.whl"},
    ])
    assert updater.latest_wheel_url() == "https://x/new.whl"


def test_find_uv_falls_back_to_known_location(tmp_path, monkeypatch):
    import shutil as _sh

    from lunamoth.config import find_uv
    monkeypatch.setattr(_sh, "which", lambda n: None)  # not on PATH (the GUI-launch case)
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path))
    (tmp_path / "bin").mkdir(parents=True)
    uv = tmp_path / "bin" / "uv"
    uv.write_text("#!/bin/sh\n")
    assert find_uv() == str(uv)
