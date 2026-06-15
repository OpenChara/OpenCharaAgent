"""`lunamoth setup browser` — the optional browser_* tool driver installer.

The browser tools are check_fn-gated on the agent-browser CLI + a Chromium
build; this command reports status and prints honest install guidance. The
tests never run a real install — they monkeypatch the driver's discovery."""
import argparse

import pytest

from lunamoth.front import cli
from lunamoth.tools.builtin import _browser_driver as drv


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
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


def test_setup_browser_guides_when_node_present_driver_absent(monkeypatch, capsys):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("node", "npm") else None)
    rc = cli.cmd_setup(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "npm install -g agent-browser" in out
    assert "agent-browser install" in out
    # Honest about the isolation caveat.
    assert "dir" in out and "docker" in out


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
