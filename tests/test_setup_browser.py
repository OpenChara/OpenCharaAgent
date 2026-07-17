"""`chara setup browser` — the optional browser_* tool driver installer.

The browser tools are check_fn-gated on the agent-browser CLI + a Chromium
build; this command reports status and prints honest install guidance. The
tests never run a real install — they monkeypatch the driver's discovery."""
import argparse

import pytest

from chara.front import cli
from chara.tools.builtin import _browser_driver as drv


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    yield


def _args(**kw):
    ns = argparse.Namespace(name="browser", check=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_setup_browser_subcommand_is_registered():
    # `setup` accepts the reserved `browser` name and routes to the driver setup.
    parser = cli.build_parser()
    ns = parser.parse_args(["setup", "browser"])
    assert ns.name == "browser"
    assert ns.func is cli.cmd_setup


def test_setup_browser_reports_ready_when_driver_present(monkeypatch, capsys):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: "/usr/local/bin/agent-browser")
    monkeypatch.setattr(drv, "chromium_installed", lambda: True)
    rc = cli.cmd_setup(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "ready" in out.lower()


def test_setup_browser_attempts_install_when_node_present(monkeypatch, capsys):
    # Node present, driver absent → the command ACTUALLY attempts the install
    # (npm i -g agent-browser, then agent-browser install). subprocess is mocked
    # so no real install runs; the driver stays absent, so it reports not-ready.
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("node", "npm", "agent-browser") else None)
    monkeypatch.setattr(drv, "_reset_caches_for_test", lambda: None)
    calls = []

    class _R:
        returncode = 0

    def _fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _R()

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)
    rc = cli.cmd_setup(_args())
    # It attempted the npm install step (no longer just printed guidance).
    assert any("npm" in str(c[0]) and "agent-browser" in c for c in calls)
    # Driver still absent after (mocked), so it honestly reports not-ready.
    assert rc == 1


def test_setup_browser_honest_when_node_missing(monkeypatch, capsys):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)  # no node/npm
    rc = cli.cmd_setup(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "node" in out.lower()


def test_doctor_runs_with_browser_line(monkeypatch, capsys):
    # doctor must not crash and should mention the optional browser tools.
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    rc = cli.cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "browser tools" in out.lower()
