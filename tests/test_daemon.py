import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lunamoth.session import sessions as S
from lunamoth.front import cli


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
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
    assert cli._stop_daemon(meta) is True
    time.sleep(0.5)
    assert meta.daemon_pid() is None


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
    env = {**os.environ, "LUNAMOTH_HOME": str(home)}
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    old_home = os.environ.get("LUNAMOTH_HOME")
    os.environ["LUNAMOTH_HOME"] = str(home)
    meta = S.create_session("stdio")
    _configure(meta)
    try:
        with subprocess.Popen(
            [sys.executable, "-m", "lunamoth.front.cli", "serve", "stdio", "--stdio"],
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
            os.environ.pop("LUNAMOTH_HOME", None)
        else:
            os.environ["LUNAMOTH_HOME"] = old_home
