"""Unit tests for the `chara connect ssh://…` SSH-tunnel orchestration.

Pure functions are tested directly; the subprocess seams are monkeypatched so
no live SSH host is required.
"""
from __future__ import annotations

import json

import pytest

from chara.server import sshconnect as SC


# ── ssh:// URL parse ──────────────────────────────────────────────────────────


def test_parse_ssh_url_full():
    t = SC.parse_ssh_url("ssh://alice@example.com:2222")
    assert t.user == "alice"
    assert t.host == "example.com"
    assert t.port == 2222
    assert t.target() == "alice@example.com"


def test_parse_ssh_url_no_user():
    t = SC.parse_ssh_url("ssh://example.com:2200")
    assert t.user is None
    assert t.host == "example.com"
    assert t.port == 2200
    assert t.target() == "example.com"


def test_parse_ssh_url_no_port_defaults_22():
    t = SC.parse_ssh_url("ssh://bob@host")
    assert t.user == "bob"
    assert t.host == "host"
    assert t.port == 22


def test_parse_ssh_url_bare_host_no_scheme():
    t = SC.parse_ssh_url("user@10.0.0.5:2022")
    assert t.user == "user"
    assert t.host == "10.0.0.5"
    assert t.port == 2022


@pytest.mark.parametrize("bad", ["", "   ", "http://host", "ssh://", "ssh://user@host:notaport"])
def test_parse_ssh_url_rejects_bad(bad):
    with pytest.raises(SC.ConnectError):
        SC.parse_ssh_url(bad)


# ── daemon.json parse ─────────────────────────────────────────────────────────


def test_parse_daemon_json_ok():
    text = json.dumps({"pid": 42, "http_port": 8800, "ws_port": 8801, "token": "secret-tok"})
    d = SC.parse_daemon_json(text)
    assert d.token == "secret-tok"
    assert d.http_port == 8800
    assert d.ws_port == 8801


@pytest.mark.parametrize("text", ["", "   ", "not json", "[]"])
def test_parse_daemon_json_rejects_garbage(text):
    with pytest.raises(SC.ConnectError):
        SC.parse_daemon_json(text)


@pytest.mark.parametrize(
    "obj",
    [
        {"http_port": 1, "ws_port": 2},  # no token
        {"token": "x", "ws_port": 2},  # no http_port
        {"token": "x", "http_port": 1},  # no ws_port
        {"token": "x", "http_port": 0, "ws_port": 0},  # zero ports
    ],
)
def test_parse_daemon_json_requires_token_and_ports(obj):
    with pytest.raises(SC.ConnectError):
        SC.parse_daemon_json(json.dumps(obj))


# ── ssh -L argv shape ─────────────────────────────────────────────────────────


def test_build_ssh_argv_two_forwards_and_target():
    t = SC.SshTarget(host="example.com", user="alice", port=2222)
    argv = SC.build_ssh_argv(t, local_http=5000, remote_http=8800, local_ws=5001, remote_ws=8801)
    assert argv[0] == "ssh"
    assert "-N" in argv
    # exactly two -L forwards
    assert argv.count("-L") == 2
    assert "5000:127.0.0.1:8800" in argv
    assert "5001:127.0.0.1:8801" in argv
    # custom port threaded
    assert "-p" in argv and "2222" in argv
    # target is last
    assert argv[-1] == "alice@example.com"
    # fail-fast on forward error so we never hang silently
    assert "ExitOnForwardFailure=yes" in argv


def test_build_ssh_argv_default_port_omits_p():
    t = SC.SshTarget(host="h", user=None, port=22)
    argv = SC.build_ssh_argv(t, 1, 2, 3, 4)
    assert "-p" not in argv
    assert argv[-1] == "h"


def test_build_remote_exec_argv():
    t = SC.SshTarget(host="h", user="u", port=2200)
    argv = SC.build_remote_exec_argv(t, "cat ~/.chara/daemon.json")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "-p" in argv and "2200" in argv
    assert argv[-2] == "u@h"
    assert argv[-1] == "cat ~/.chara/daemon.json"


# ── local URL build ───────────────────────────────────────────────────────────


def test_build_local_url():
    d = SC.RemoteDaemon(token="tok en/+", http_port=8800, ws_port=8801)
    url = SC.build_local_url(d, local_http=5000, local_ws=5001)
    assert url.startswith("http://127.0.0.1:5000/#token=")
    assert "&ws=5001" in url
    # token is URL-quoted (the slash/plus/space escaped)
    assert "tok en/+" not in url
    assert "tok%20en%2F%2B" in url


# ── ensure_remote_daemon: read vs "no daemon.json → start" branch ─────────────


def test_read_remote_daemon_present(monkeypatch):
    blob = json.dumps({"http_port": 8800, "ws_port": 8801, "token": "t"})
    monkeypatch.setattr(SC, "_ssh_exec", lambda target, cmd, timeout=20.0: (0, blob, ""))
    d = SC.read_remote_daemon(SC.SshTarget(host="h"))
    assert d is not None and d.token == "t"


def test_read_remote_daemon_absent_returns_none(monkeypatch):
    monkeypatch.setattr(
        SC, "_ssh_exec",
        lambda target, cmd, timeout=20.0: (1, "", "cat: ~/.chara/daemon.json: No such file or directory"),
    )
    assert SC.read_remote_daemon(SC.SshTarget(host="h")) is None


def test_read_remote_daemon_auth_failure_raises(monkeypatch):
    monkeypatch.setattr(
        SC, "_ssh_exec",
        lambda target, cmd, timeout=20.0: (255, "", "Permission denied (publickey)."),
    )
    with pytest.raises(SC.ConnectError) as exc:
        SC.read_remote_daemon(SC.SshTarget(host="h"))
    assert "Permission denied" in str(exc.value)


def test_ensure_remote_daemon_starts_when_absent(monkeypatch):
    """The 'no daemon.json → start it' branch: first read absent, start invoked,
    second read present."""
    calls: list[str] = []

    def fake_exec(target, cmd, timeout=20.0):
        calls.append(cmd)
        if cmd.startswith("cat"):
            if "chara desktop --daemon" in " ".join(calls):
                # after start, the file exists
                return (0, json.dumps({"http_port": 9, "ws_port": 10, "token": "z"}), "")
            return (1, "", "No such file or directory")
        if cmd.startswith("chara desktop --daemon"):
            return (0, "started", "")
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(SC, "_ssh_exec", fake_exec)
    monkeypatch.setattr(SC.time, "sleep", lambda *_a: None)

    d = SC.ensure_remote_daemon(SC.SshTarget(host="h"), log=lambda *_a: None)
    assert d.token == "z"
    # the start command was issued
    assert any(c.startswith("chara desktop --daemon") for c in calls)


def test_start_remote_daemon_failure_raises(monkeypatch):
    monkeypatch.setattr(
        SC, "_ssh_exec",
        lambda target, cmd, timeout=60.0: (127, "", "chara: command not found"),
    )
    with pytest.raises(SC.ConnectError) as exc:
        SC.start_remote_daemon(SC.SshTarget(host="h"))
    assert "command not found" in str(exc.value)


# ── connect(): full flow with mocked seams (no live ssh) ──────────────────────


def test_connect_flow_mocked(monkeypatch):
    blob = json.dumps({"http_port": 8800, "ws_port": 8801, "token": "TKN"})
    monkeypatch.setattr(SC, "_ssh_exec", lambda target, cmd, timeout=20.0: (0, blob, ""))

    ports = iter([5000, 5001])
    monkeypatch.setattr(SC, "_free_local_port", lambda: next(ports))
    monkeypatch.setattr(SC.time, "sleep", lambda *_a: None)

    spawned: dict[str, list[str]] = {}

    class FakeProc:
        returncode = 0

        def poll(self):
            return None  # alive at the immediate-death check

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    def fake_spawn(argv):
        spawned["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(SC, "_spawn_tunnel", fake_spawn)

    opened: list[str] = []
    monkeypatch.setattr(SC, "_open_browser", lambda url: opened.append(url))

    rc = SC.connect("ssh://alice@host:2222", open_browser=True, log=lambda *_a: None)
    assert rc == 0
    # the spawned tunnel forwarded both local ports to the remote loopback
    assert "5000:127.0.0.1:8800" in spawned["argv"]
    assert "5001:127.0.0.1:8801" in spawned["argv"]
    # the browser got the tunneled localhost URL with the remote token + local ws
    assert opened == ["http://127.0.0.1:5000/#token=TKN&ws=5001"]


def test_connect_tunnel_dies_immediately_raises(monkeypatch):
    blob = json.dumps({"http_port": 8800, "ws_port": 8801, "token": "TKN"})
    monkeypatch.setattr(SC, "_ssh_exec", lambda target, cmd, timeout=20.0: (0, blob, ""))
    monkeypatch.setattr(SC, "_free_local_port", lambda: 5000)
    monkeypatch.setattr(SC.time, "sleep", lambda *_a: None)

    class DeadProc:
        returncode = 255

        def poll(self):
            return 255  # already exited

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 255

    monkeypatch.setattr(SC, "_spawn_tunnel", lambda argv: DeadProc())
    monkeypatch.setattr(SC, "_open_browser", lambda url: None)

    with pytest.raises(SC.ConnectError) as exc:
        SC.connect("ssh://host", open_browser=False, log=lambda *_a: None)
    assert "exited immediately" in str(exc.value)
