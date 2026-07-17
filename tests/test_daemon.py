import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chara.session import sessions as S
from chara.front import cli


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    yield


def _configure(meta):
    meta.config_path.write_text(json.dumps({"provider": "mock", "character_path": ""}), encoding="utf-8")


def test_status_progression():
    meta = S.create_session("a")
    assert meta.status() == "new"           # no config yet
    _configure(meta)
    assert S.load_session("a").status() == "idle"


def test_unconfigured_agent_does_not_daemonize():
    meta = S.create_session("b")
    assert cli._start_daemon(meta) is False
    assert meta.daemon_pid() is None


def test_daemon_start_and_stop():
    meta = S.create_session("c")
    _configure(meta)
    assert cli._start_daemon(meta, patience=5) is True
    pid = meta.daemon_pid()
    assert pid and pid > 0
    # the recorded pid is a live process
    os.kill(pid, 0)
    assert S.load_session("c").status() == "running"
    assert cli._stop_daemon(meta) == "stopped"
    time.sleep(0.5)
    assert meta.daemon_pid() is None


def test_start_daemon_claim_prevents_double_spawn(monkeypatch):
    """Two concurrent starts must spawn ONE process: daemon.pid is claimed atomically
    (O_EXCL) BEFORE Popen, so the loser sees the claim and reports already-starting."""
    meta = S.create_session("race")
    _configure(meta)
    spawns = []

    class _Proc:
        pid = 4242

    def racing_popen(*a, **kw):
        spawns.append(a)
        # A second starter races in DURING the spawn window: it must lose the
        # claim (empty daemon.pid) and must NOT spawn a second process.
        assert cli._start_daemon(meta) is True
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", racing_popen)
    assert cli._start_daemon(meta) is True
    assert len(spawns) == 1
    # the winner recorded the real pid after the spawn
    assert meta.daemon_pid_path.read_text(encoding="utf-8").strip() == "4242"


def test_start_daemon_releases_the_claim_on_spawn_failure(monkeypatch):
    meta = S.create_session("boom")
    _configure(meta)

    def failing_popen(*a, **kw):
        raise OSError("spawn failed")

    monkeypatch.setattr(cli.subprocess, "Popen", failing_popen)
    with pytest.raises(OSError):
        cli._start_daemon(meta)
    assert not meta.daemon_pid_path.exists()  # claim released — a retry can claim again

    class _Proc:
        pid = 7

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: _Proc())
    assert cli._start_daemon(meta) is True
    assert meta.daemon_pid_path.read_text(encoding="utf-8").strip() == "7"


def test_start_daemon_argv_carries_the_session_marker(monkeypatch):
    """The daemon argv is otherwise identical across charas; the inert --session
    marker is what lets pid_is_chara tell sibling daemons apart after reboot
    pid-reuse (terminal.py accepts and ignores it)."""
    meta = S.create_session("marked")
    _configure(meta)

    class _Proc:
        pid = 99

    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    assert cli._start_daemon(meta) is True
    i = seen["argv"].index("--session")
    assert seen["argv"][i + 1] == "marked"


def test_stop_racing_a_fresh_start_claim_leaves_it_alone():
    """stop racing start: an EMPTY claim younger than the TTL means a starter is
    mid-Popen — unlinking it would let the daemon come up with no pid file. The
    stop must leave the claim and report 'starting', not silently clear it; only
    an EXPIRED claim (crashed starter) is cleared."""
    meta = S.create_session("racing")
    _configure(meta)
    meta.daemon_pid_path.write_text("", encoding="utf-8")  # a fresh O_EXCL claim
    assert cli._stop_daemon(meta) == "starting — try again shortly"
    assert meta.daemon_pid_path.exists()  # the claim survives for the starter
    old = time.time() - 2 * S._CLAIM_TTL
    os.utime(meta.daemon_pid_path, (old, old))  # now a crashed starter's orphan
    assert cli._stop_daemon(meta) == "not running"
    assert not meta.daemon_pid_path.exists()


def test_start_all_only_configured(capsys):
    S.create_session("cfg"); _configure(S.load_session("cfg"))
    S.create_session("raw")  # unconfigured
    try:
        cli._start_all()
        running = {m.name for m in S.list_sessions() if m.daemon_pid()}
        assert "cfg" in running and "raw" not in running
    finally:
        for m in S.list_sessions():
            cli._stop_daemon(m)


def test_serve_sigterm_clears_running_marker(tmp_path):
    home = tmp_path / "home-serve"
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "CHARA_HOME": str(home)}
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    old_home = os.environ.get("CHARA_HOME")
    os.environ["CHARA_HOME"] = str(home)
    meta = S.create_session("stdio")
    _configure(meta)
    try:
        with subprocess.Popen(
            [sys.executable, "-m", "chara.front.cli", "serve", "stdio", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        ) as proc:
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not meta.pid_path.exists():
                    time.sleep(0.05)
                assert meta.pid_path.exists()
                assert S.load_session("stdio").running_pid() == proc.pid
                proc.terminate()
                proc.wait(timeout=5)
                assert not meta.pid_path.exists()
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=5)
    finally:
        if old_home is None:
            os.environ.pop("CHARA_HOME", None)
        else:
            os.environ["CHARA_HOME"] = old_home
