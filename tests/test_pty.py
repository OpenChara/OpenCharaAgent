"""PTY over WS: the bridge (server/pty.py), the jail shapes
(session/isolation.py), and the supervisor's /chara/<name>/pty endpoint.

Config paths are pinned at import time (CLAUDE.md gotcha), but everything here
goes through session dirs resolved from LUNAMOTH_HOME at call time, so the
env fixtures below are enough.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from lunamoth.server.pty import PtyBridge
from lunamoth.session import isolation as I


# ---- PtyBridge ---------------------------------------------------------------

def _read_until(bridge: PtyBridge, needle: bytes, timeout: float = 8.0) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and needle not in buf:
        chunk = bridge.read(0.2)
        if chunk is None:
            break
        buf += chunk
    return buf


def test_bridge_cat_roundtrip():
    with PtyBridge.spawn(["/bin/cat"]) as bridge:
        assert bridge.pid > 0
        assert bridge.is_alive()
        bridge.write(b"hello pty\n")
        buf = _read_until(bridge, b"hello pty")
        assert b"hello pty" in buf  # cat echoes (plus tty echo)


def test_bridge_eof_after_child_exit():
    bridge = PtyBridge.spawn(["/bin/sh", "-c", "exit 0"])
    try:
        deadline = time.monotonic() + 8.0
        chunk: bytes | None = b""
        while time.monotonic() < deadline:
            chunk = bridge.read(0.2)
            if chunk is None:
                break
        assert chunk is None  # EOF surfaced as None, not an exception
        assert not bridge.is_alive() or bridge.returncode is not None
    finally:
        bridge.close()


def test_bridge_resize_accepts_garbage():
    with PtyBridge.spawn(["/bin/cat"]) as bridge:
        bridge.resize(131072, 1)   # broken probe (WSL2-style)
        bridge.resize(-1, -5)
        bridge.resize("x", "y")    # non-numeric garbage
        bridge.resize(None, None)
        bridge.resize(100, 30)     # sane values still fine
        assert bridge.is_alive()


def test_bridge_close_twice_no_zombie():
    bridge = PtyBridge.spawn(["/bin/cat"])
    pid = bridge.pid
    bridge.close()
    bridge.close()  # idempotent
    assert bridge.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped: no zombie left behind
    assert bridge.read() is None
    bridge.write(b"ignored")  # no-op, no raise


# ---- isolation: interactive_shell_argv ----------------------------------------

def test_interactive_argv_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    argv, cwd, env = I.interactive_shell_argv("admin", tmp_path / "ws")
    assert argv == ["/bin/bash", "-i"]
    assert cwd == str((tmp_path / "ws").resolve())
    # credential blocklist applies to interactive shells too
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["TMPDIR"] == cwd


@pytest.mark.skipif(sys.platform != "darwin" or not I.os_sandbox_available(), reason="needs macOS sandbox-exec")
def test_interactive_argv_macos_sandbox_shape(tmp_path):
    argv, cwd, env = I.interactive_shell_argv("sandbox", tmp_path / "ws", allow_network=False)
    assert argv[0] == "sandbox-exec" and argv[1] == "-p"
    assert argv[-2:] == ["/bin/bash", "-i"]
    profile = argv[2]
    assert "(deny network*)" in profile
    # the interactive profile opens the pty slave device nodes
    assert '(allow file-ioctl (regex #"^/dev/ttys[0-9]+$"))' in profile
    assert '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))' in profile
    # ... and the one-shot profile does NOT (byte-for-byte runner behavior)
    one_shot = I._macos_jail("true", tmp_path / "ws", False, [])
    assert "/dev/ttys" not in one_shot[2]


@pytest.mark.skipif(sys.platform != "linux" or not I.os_sandbox_available(), reason="needs Linux bwrap")
def test_interactive_argv_linux_sandbox_shape(tmp_path):
    argv, cwd, env = I.interactive_shell_argv("sandbox", tmp_path / "ws")
    assert argv[0] == "bwrap"
    assert "--dev" in argv  # provides devpts for the inner tty
    assert argv[-2:] == ["/bin/bash", "-i"]


def test_interactive_argv_legacy_docker_maps_to_admin(tmp_path):
    # Old `docker`/`dir`/`local` values normalize to admin (no jail).
    for legacy in ("docker", "dir", "local"):
        argv, cwd, env = I.interactive_shell_argv(legacy, tmp_path / "ws")
        assert argv == ["/bin/bash", "-i"]


def test_jail_unavailable_raises_no_degrade(tmp_path, monkeypatch):
    monkeypatch.setattr(I.shutil, "which", lambda name: None)
    # Also force Landlock off: on a Linux kernel with Landlock (e.g. CI runners)
    # the sandbox tier would fall to Landlock instead of refusing, so "no jail"
    # must mock BOTH the native jail (via shutil.which) AND Landlock.
    monkeypatch.setattr(I, "landlock_available", lambda: False)
    with pytest.raises(I.JailUnavailableError):
        I.interactive_shell_argv("sandbox", tmp_path / "ws")
    with pytest.raises(I.JailUnavailableError):
        I.interactive_shell_argv("warden", tmp_path / "ws")  # unknown mechanism
    # admin needs no jail and still works
    argv, _, _ = I.interactive_shell_argv("admin", tmp_path / "ws")
    assert argv == ["/bin/bash", "-i"]


def test_runtime_permissions_reads_env_status(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    assert I.runtime_permissions(sandbox) == (False, [])  # missing file = defaults
    (sandbox / "env_status.json").write_text(
        json.dumps({"network_access": True, "writable_paths": ["/tmp/extra"]}), encoding="utf-8"
    )
    assert I.runtime_permissions(sandbox) == (True, ["/tmp/extra"])
    (sandbox / "env_status.json").write_text("not json", encoding="utf-8")
    assert I.runtime_permissions(sandbox) == (False, [])


@pytest.mark.skipif(sys.platform != "darwin" or not I.os_sandbox_available(), reason="needs macOS sandbox-exec")
def test_macos_interactive_jail_real_pty(tmp_path):
    """An interactive command under the extended sandbox-exec profile on a
    real pty: the round-trip proves the /dev/ttys* rules are sufficient."""
    argv, cwd, env = I.interactive_shell_argv("sandbox", tmp_path / "ws")
    with PtyBridge.spawn(argv, cwd=cwd, env=env) as bridge:
        # $((40+2)) so the matched output differs from the echoed input line
        bridge.write(b"echo JAIL_$((40+2))\n")
        buf = _read_until(bridge, b"JAIL_42", timeout=15.0)
        assert b"JAIL_42" in buf


# ---- supervisor routing: /chara/<name>/pty -------------------------------------

websockets = pytest.importorskip("websockets")


@pytest.fixture
def pty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.session import sessions as S

    meta = S.create_session("shellpal", isolation="admin")
    return meta


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60.0))


async def _recv_all_until(ws, needle: bytes, timeout: float = 20.0) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < deadline:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        assert isinstance(msg, bytes), "PTY output must arrive as binary frames"
        buf += msg
    return buf


def test_pty_ws_auth_and_unknown_chara(pty_home):
    from lunamoth.server.supervisor import Supervisor, free_port

    async def scenario():
        port = free_port()
        sup = Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            # bad token -> 4401 (auth happens before routing)
            async with websockets.connect(f"ws://127.0.0.1:{port}/chara/shellpal/pty?token=wrong") as ws:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == 4401
            # unknown chara -> 4404
            async with websockets.connect(f"ws://127.0.0.1:{port}/chara/nobody/pty?token=sesame") as ws:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == 4404
        await sup.shutdown()

    _run(scenario())


def test_pty_ws_shell_roundtrip_resize_and_audit(pty_home):
    from lunamoth.server.supervisor import Supervisor, free_port

    meta = pty_home

    async def scenario():
        port = free_port()
        sup = Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            url = f"ws://127.0.0.1:{port}/chara/shellpal/pty?token=sesame&cols=100&rows=30"
            async with websockets.connect(url) as ws:
                # the resize escape is consumed server-side, never echoed back
                await ws.send(b"\x1b[RESIZE:120;40]")
                await ws.send(b"echo round_$((20+5))trip\n")
                buf = await _recv_all_until(ws, b"round_25trip")
                assert b"round_25trip" in buf
                assert b"RESIZE" not in buf
            # bridge is closed and audited after the client disconnects
            deadline = time.monotonic() + 10.0
            while sup._pty_bridges and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert not sup._pty_bridges
        await sup.shutdown()

    _run(scenario())

    audit_path = meta.sandbox_dir / "logs" / "audit.jsonl"
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    opens = [r for r in records if r["event"] == "pty_open"]
    closes = [r for r in records if r["event"] == "pty_close"]
    assert opens and closes
    assert opens[0]["chara"] == "shellpal" and opens[0]["isolation"] == "admin" and opens[0]["pid"] > 0
    assert closes[0]["chara"] == "shellpal"


def test_pty_ws_jail_unavailable_fails_visibly(pty_home, monkeypatch):
    """sandbox session + no jail on the host = error frame + close 1011,
    never a silent shell with directory trust."""
    from lunamoth.server.supervisor import Supervisor, free_port
    from lunamoth.session import sessions as S

    S.create_session("jailed", isolation="sandbox")
    monkeypatch.setattr(I, "os_sandbox_available", lambda: False)
    # On a Landlock-capable kernel (CI), the sandbox tier would use Landlock
    # instead of refusing; force it off so "no jail available" actually holds.
    monkeypatch.setattr(I, "landlock_available", lambda: False)

    async def scenario():
        port = free_port()
        sup = Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            async with websockets.connect(f"ws://127.0.0.1:{port}/chara/jailed/pty?token=sesame") as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                assert isinstance(msg, str) and "shell unavailable" in msg
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == 1011
        assert not sup._pty_bridges
        await sup.shutdown()

    _run(scenario())
