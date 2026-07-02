"""The isolation ladder: native jail → Landlock → refuse (never directory trust).

These cover the 2026-06-17 hardening: a chara's `terminal` must not silently run
unconfined when the OS jail is unavailable (the no-userns case), and when
Landlock IS available it must confine reads to workspace+assets.
"""
import lunamoth.session.isolation as ISO
from lunamoth.session import landlock
from lunamoth.tools.runner import run_terminal

import pytest


def test_landlock_abi_version_is_nonneg_int():
    v = landlock.abi_version()
    assert isinstance(v, int) and v >= 0
    assert landlock.available() == (v >= 1)


def test_sandbox_refuses_when_no_jail_available(tmp_path, monkeypatch):
    """No bwrap AND no Landlock → REFUSE, never run unconfined (directory trust).

    The ladder decision lives in ``session/isolation.build_jail_command`` (the
    one copy shared by the foreground runner and the background process path), so
    that is where availability is mocked away.
    """
    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)
    monkeypatch.setattr(ISO, "landlock_available", lambda: False)
    ws = tmp_path / "ws"
    out = run_terminal("echo SHOULD_NOT_RUN", ws, isolation="sandbox", timeout=10)
    assert "refused" in out.lower()
    assert "SHOULD_NOT_RUN" not in out


def test_background_sandbox_refuses_when_no_jail_available(tmp_path, monkeypatch):
    """The BACKGROUND process path goes native→Landlock→refuse just like the
    foreground — a bg `sandbox` spawn on a no-jail host must NOT start unconfined.

    This mirrors ``test_sandbox_refuses_when_no_jail_available`` for the path that
    previously silently degraded to directory trust (the security inconsistency).
    """
    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)
    monkeypatch.setattr(ISO, "landlock_available", lambda: False)
    from lunamoth.session.isolation import JailUnavailableError
    from lunamoth.tools.builtin._process_registry import ProcessRegistry

    ws = tmp_path / "ws"
    reg = ProcessRegistry()
    with pytest.raises(JailUnavailableError):
        reg.spawn("echo SHOULD_NOT_RUN", ws, isolation="sandbox")
    # Nothing was tracked — the process never started.
    assert reg.count_running() == 0
    assert reg.list_sessions() == []


def test_background_sandbox_refusal_surfaces_as_tool_error(tmp_path, monkeypatch):
    """End-to-end: the `terminal(background=true)` tool turns the refusal into a
    visible error to the model, never a silent unconfined run."""
    import json

    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)
    monkeypatch.setattr(ISO, "landlock_available", lambda: False)
    from lunamoth.tools.builtin.terminal import terminal

    from lunamoth.core.state import Permissions

    class _State:
        def load(self):
            return {"isolation": "sandbox", "network_access": False, "writable_paths": []}

        def permissions(self):
            return Permissions(isolation="sandbox", network_on=False, writable_paths=[])

    class _Ctx:
        def __init__(self, ws):
            self._ws = ws
            self.state = _State()
            self.processes = None

        @property
        def workspace(self):
            return self._ws

        def permissions(self):
            return self.state.permissions()

    ws = tmp_path / "ws"
    ws.mkdir()
    res = json.loads(terminal({"command": "echo SHOULD_NOT_RUN", "background": True}, _Ctx(ws)))
    # tool_error: an `error` field, no session_id/pid (the process never started).
    assert "error" in res
    assert "session_id" not in res
    assert "SHOULD_NOT_RUN" not in res.get("output", "")
    assert "no jail is available" in res["error"].lower()


def test_admin_still_runs_unconfined_when_explicit(tmp_path):
    """`admin` is an explicit opt-out — it must still run (not refuse)."""
    out = run_terminal("echo explicit-admin", tmp_path / "ws", isolation="admin", timeout=10)
    assert "explicit-admin" in out


def test_legacy_docker_value_maps_to_admin_and_runs(tmp_path):
    """Old `docker` isolation value normalizes to admin (no jail) and runs."""
    out = run_terminal("echo legacy-docker", tmp_path / "ws", isolation="docker", timeout=10)
    assert "legacy-docker" in out


def test_landlock_tier_note_mentions_proc_and_network(tmp_path, monkeypatch):
    """Every Landlock-tier run carries the honest notes: /proc unavailable by
    policy (so a ps/top EACCES reads as the jail, not a mystery) and — with
    net off — network not gated (ABI v1). Shape-only: the argv is not run."""
    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)
    monkeypatch.setattr(ISO, "landlock_available", lambda: True)
    ws = tmp_path / "ws"
    _, _, note = ISO.build_jail_command("ps aux", ws, "sandbox", allow_network=False)
    assert "/proc" in note and "EACCES" in note
    assert "network not gated (ABI v1)" in note
    # net ON: the network note drops, the /proc note stays
    _, _, note_on = ISO.build_jail_command("ps aux", ws, "sandbox", allow_network=True)
    assert "/proc" in note_on
    assert "network not gated" not in note_on
    # browser jail: /proc is re-added (--rw /proc), so no /proc note there
    _, _, note_br = ISO.build_jail_command("chromium", ws, "sandbox",
                                           allow_network=True, browser=True)
    assert "/proc" not in note_br


def test_native_tiers_carry_no_landlock_note(tmp_path, monkeypatch):
    """The /proc note must not spam the other tiers (admin / native jail)."""
    ws = tmp_path / "ws"
    _, _, admin_note = ISO.build_jail_command("ps aux", ws, "admin")
    assert admin_note == ""
    if ISO.os_sandbox_available():  # native jail on this host (bwrap / sandbox-exec)
        _, _, native_note = ISO.build_jail_command("ps aux", ws, "sandbox", allow_network=False)
        assert native_note == ""


@pytest.mark.skipif(not landlock.available(), reason="no Landlock (kernel <5.13 / not Linux)")
def test_landlock_tier_runs_normal_commands(tmp_path, monkeypatch):
    """When bwrap is absent but Landlock is present, the sandbox tier still runs."""
    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)  # force the Landlock tier
    ws = tmp_path / "sandbox" / "sessions" / "x" / "workspace"
    ws.mkdir(parents=True)
    out = run_terminal("echo hi-from-landlock", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "hi-from-landlock" in out


@pytest.mark.skipif(not landlock.available(), reason="no Landlock (kernel <5.13 / not Linux)")
def test_landlock_blocks_reads_outside_workspace(tmp_path, monkeypatch):
    """The chara can read its workspace but NOT a secret outside it (the key file case)."""
    monkeypatch.setattr(ISO, "os_sandbox_available", lambda: False)
    secret = tmp_path / "desktop.json"
    secret.write_text("TOPSECRET_KEY")
    ws = tmp_path / "sandbox" / "sessions" / "x" / "workspace"
    ws.mkdir(parents=True)
    (ws / "mine.txt").write_text("MY_OWN_FILE")

    blocked = run_terminal(f"cat {secret}", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "TOPSECRET_KEY" not in blocked  # Landlock denies the out-of-jail read

    allowed = run_terminal("cat mine.txt", ws, isolation="sandbox", allow_network=True, timeout=20)
    assert "MY_OWN_FILE" in allowed  # own workspace still readable
