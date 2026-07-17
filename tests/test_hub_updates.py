"""Update/changelog backend (server/hub/updates.py) — version compare, status shape,
caching, and offline fallback. Fully mocked: never touches the network or git in CI."""
import json

import pytest

from chara.server.hub import updates as U


def test_norm_orders_versions():
    assert U._norm("v0.1.10") > U._norm("v0.1.2")  # numeric, not lexical
    assert U._norm("0.1.1") == U._norm("v0.1.1")  # leading v optional
    assert U._norm("v1.0.0") > U._norm("v0.9.9")
    assert U._norm("") == (0,)


# ---- update.restart RPC: relaunch the resident instance into the new code -------

class _StubSupervisor:
    def __init__(self):
        self.scheduled = None

    def schedule_restart(self, delay=1.0):
        self.scheduled = delay
        return True


def test_update_restart_schedules_on_the_supervisor():
    from chara.server import hub as H
    sv = _StubSupervisor()
    d = H.HubDispatcher(lambda f: True, supervisor=sv)
    out = d._update_restart({"delay": 0.5})
    assert out == {"ok": True, "restarting": True}
    assert sv.scheduled == 0.5  # forwarded to the supervisor's scheduler


def test_update_restart_without_supervisor_tells_client_to_do_it_by_hand():
    from chara.server import hub as H
    d = H.HubDispatcher(lambda f: True)  # no resident supervisor (e.g. a foreground tui)
    out = d._update_restart({})
    assert out["ok"] is False and "manual" in out["error"].lower()


def test_relaunch_argv_pins_resolved_ports():
    from chara.server.supervisor.core import Supervisor
    # The launch uses --port 0 / --ws-port 0 (OS-assigned); the re-exec MUST substitute
    # the actually-bound ports, else the new process rebinds random ports and strands clients.
    argv = Supervisor._relaunch_argv(
        8123, 9456,
        ["desktop", "--host", "127.0.0.1", "--port", "0", "--ws-port", "0", "--no-open"],
    )
    assert argv[1:3] == ["-m", "chara.front.cli"]  # re-run the new code
    assert "0" not in argv  # the placeholder ports are gone
    assert argv[-4:] == ["--port", "8123", "--ws-port", "9456"]  # resolved ports pinned


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(U.S, "chara_home", lambda: tmp_path)
    return tmp_path


def _releases(*tags):
    return [{"tag": t, "name": t, "body": f"notes {t}", "published_at": "", "url": "", "prerelease": False}
            for t in tags]


def test_status_wheel_update_available(home, monkeypatch):
    monkeypatch.setattr(U, "_is_dev", lambda: False)
    monkeypatch.setattr(U, "_fetch_releases", lambda: _releases("v0.1.2", "v0.1.1"))
    monkeypatch.setattr(U, "__version__", "0.1.1")
    s = U.status(force=True)
    assert s["channel"] == "wheel"
    assert s["latest"] == "v0.1.2"
    assert s["update_available"] is True
    assert [r["tag"] for r in s["releases"]] == ["v0.1.2", "v0.1.1"]


def test_status_wheel_up_to_date(home, monkeypatch):
    monkeypatch.setattr(U, "_is_dev", lambda: False)
    monkeypatch.setattr(U, "_fetch_releases", lambda: _releases("v0.1.1"))
    monkeypatch.setattr(U, "__version__", "0.1.1")
    assert U.status(force=True)["update_available"] is False


def test_status_dev_uses_commits_behind(home, monkeypatch):
    # On a dev checkout, being behind main signals an update even with no newer tag.
    monkeypatch.setattr(U, "_is_dev", lambda: True)
    monkeypatch.setattr(U, "_fetch_releases", lambda: _releases("v0.1.1"))
    monkeypatch.setattr(U, "_commits_behind", lambda: 3)
    monkeypatch.setattr(U, "__version__", "0.1.1")
    s = U.status(force=True)
    assert s["channel"] == "dev"
    assert s["behind"] == 3
    assert s["update_available"] is True


def test_status_caches_within_ttl(home, monkeypatch):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _releases("v0.1.1")

    monkeypatch.setattr(U, "_is_dev", lambda: False)
    monkeypatch.setattr(U, "_fetch_releases", fetch)
    monkeypatch.setattr(U, "_commits_behind", lambda: None)
    U.status(force=True)
    U.status()  # within TTL → served from the stamp, no second fetch
    assert calls["n"] == 1


def test_status_fetch_failure_keeps_cached(home, monkeypatch):
    monkeypatch.setattr(U, "_is_dev", lambda: False)
    monkeypatch.setattr(U, "_commits_behind", lambda: None)
    monkeypatch.setattr(U, "_fetch_releases", lambda: _releases("v0.2.0"))
    U.status(force=True)  # seeds the cache with a release
    monkeypatch.setattr(U, "_fetch_releases", lambda: (_ for _ in ()).throw(OSError("offline")))
    s = U.status(force=True)  # fetch raises → falls back to cached releases
    assert s["latest"] == "v0.2.0"


def test_stamp_roundtrip(home):
    U._write_stamp({"checked_at": 123, "releases": _releases("v0.1.0")})
    data = json.loads((home / "update_check.json").read_text())
    assert data["checked_at"] == 123
    assert U._read_stamp()["releases"][0]["tag"] == "v0.1.0"
