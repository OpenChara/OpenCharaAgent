"""The isolation ladder: native jail → Landlock → refuse (never directory trust).

These cover the 2026-06-17 hardening: a chara's `terminal` must not silently run
unconfined when the OS jail is unavailable (the Docker/no-userns case), and when
Landlock IS available it must confine reads to workspace+assets.
"""
import lunamoth.tools.runner as R
from lunamoth.session import landlock
from lunamoth.tools.runner import run_terminal

import pytest


def test_landlock_abi_version_is_nonneg_int():
    v = landlock.abi_version()
    assert isinstance(v, int) and v >= 0
    assert landlock.available() == (v >= 1)


def test_sandbox_refuses_when_no_jail_available(tmp_path, monkeypatch):
    """No bwrap AND no Landlock → REFUSE, never run unconfined (directory trust)."""
    monkeypatch.setattr(R, "os_sandbox_available", lambda: False)
    monkeypatch.setattr(R, "landlock_available", lambda: False)
    ws = tmp_path / "ws"
    out = run_terminal("echo SHOULD_NOT_RUN", ws, isolation="sandbox", timeout=10)
    assert "refused" in out.lower()
    assert "SHOULD_NOT_RUN" not in out


def test_docker_refuses_without_docker_cli(tmp_path, monkeypatch):
    monkeypatch.setattr("lunamoth.tools.runner.shutil.which", lambda _x: None)
    out = run_terminal("echo SHOULD_NOT_RUN", tmp_path / "ws", isolation="docker", timeout=10)
    assert "refused" in out.lower()
    assert "SHOULD_NOT_RUN" not in out


def test_dir_still_runs_unconfined_when_explicit(tmp_path):
    """`dir` is an explicit opt-out — it must still run (not refuse)."""
    out = run_terminal("echo explicit-dir", tmp_path / "ws", isolation="dir", timeout=10)
    assert "explicit-dir" in out


@pytest.mark.skipif(not landlock.available(), reason="no Landlock (kernel <5.13 / not Linux)")
def test_landlock_tier_runs_normal_commands(tmp_path, monkeypatch):
    """When bwrap is absent but Landlock is present, the sandbox tier still runs."""
    monkeypatch.setattr(R, "os_sandbox_available", lambda: False)  # force the Landlock tier
    ws = tmp_path / "sandbox" / "sessions" / "x" / "workspace"
    ws.mkdir(parents=True)
    out = run_terminal("echo hi-from-landlock", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "hi-from-landlock" in out


@pytest.mark.skipif(not landlock.available(), reason="no Landlock (kernel <5.13 / not Linux)")
def test_landlock_blocks_reads_outside_workspace(tmp_path, monkeypatch):
    """The chara can read its workspace but NOT a secret outside it (the key file case)."""
    monkeypatch.setattr(R, "os_sandbox_available", lambda: False)
    secret = tmp_path / "desktop.json"
    secret.write_text("TOPSECRET_KEY")
    ws = tmp_path / "sandbox" / "sessions" / "x" / "workspace"
    ws.mkdir(parents=True)
    (ws / "mine.txt").write_text("MY_OWN_FILE")

    blocked = run_terminal(f"cat {secret}", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "TOPSECRET_KEY" not in blocked  # Landlock denies the out-of-jail read

    allowed = run_terminal("cat mine.txt", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "MY_OWN_FILE" in allowed  # own workspace still readable
