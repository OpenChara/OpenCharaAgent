import json
import subprocess
import sys

import pytest

from lunamoth.session import sessions as S


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def test_create_list_delete():
    meta = S.create_session("alpha", isolation="admin", note="x")
    assert meta.sandbox_dir.is_dir()
    assert S.load_session("alpha").isolation == "admin"
    assert [m.name for m in S.list_sessions()] == ["alpha"]
    S.delete_session("alpha")
    assert S.list_sessions() == []


def test_legacy_isolation_maps_to_admin():
    # Old session configs carrying dir/local/docker must read back as admin.
    meta = S.create_session("legacy", isolation="dir")  # accepted, normalized
    assert meta.isolation == "admin"
    assert S.load_session("legacy").isolation == "admin"
    # A session.json hand-written with a retired value also maps on read.
    raw = json.loads(meta.meta_path.read_text(encoding="utf-8"))
    raw["isolation"] = "docker"
    meta.meta_path.write_text(json.dumps(raw), encoding="utf-8")
    assert S.load_session("legacy").isolation == "admin"


def test_invalid_names_and_dupes():
    with pytest.raises(ValueError):
        S.create_session("../evil")
    with pytest.raises(ValueError):
        S.create_session("ok", isolation="vmware")
    S.create_session("ok")
    with pytest.raises(FileExistsError):
        S.create_session("ok")


def test_running_pid_lifecycle():
    meta = S.create_session("run")
    assert meta.running_pid() is None
    meta.mark_running()
    assert S.load_session("run").running_pid() is not None
    assert S.load_session("run").running_marker_stale() is False
    with pytest.raises(RuntimeError):
        S.delete_session("run")
    meta.clear_running()
    S.delete_session("run")


def test_stale_running_marker_is_harmless():
    meta = S.create_session("stale")
    meta.pid_path.write_text("999999999", encoding="utf-8")
    assert meta.running_pid() is None
    assert S.load_session("stale").running_marker_stale() is True
    assert S.load_session("stale").status() == "new"


def test_env_points_at_session():
    meta = S.create_session("envy")
    env = meta.env()
    assert env["LUNAMOTH_CONFIG_DIR"].endswith("sessions/envy")
    assert env["LUNAMOTH_SANDBOX"].endswith("sessions/envy/sandbox")


def test_cli_new_ls_rm(temp_home):
    def run(*argv):
        return subprocess.run(
            [sys.executable, "-m", "lunamoth.front.cli", *argv],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "LUNAMOTH_HOME": str(temp_home), "PYTHONPATH": "src"},
        )

    out = run("new", "beta", "--isolation", "admin")
    assert out.returncode == 0 and "created session 'beta'" in out.stdout
    out = run("ls")
    assert "beta" in out.stdout and "admin" in out.stdout
    out = run("rm", "beta", "-y")
    assert out.returncode == 0
    out = run("ls")
    assert "no chara yet" in out.stdout
