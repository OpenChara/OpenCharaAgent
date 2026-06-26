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


def test_env_carries_py_backend_so_callers_never_rederive_the_jail():
    """The isolation→backend map has ONE owner now (sessions); env() is the
    complete activation interface and emits LUNAMOTH_PY_BACKEND itself, so a
    caller can never drift by forgetting to set the jail."""
    assert S.isolation_to_backend("sandbox") == "sandbox"
    assert S.isolation_to_backend("admin") == "admin"
    assert S.isolation_to_backend("anything-unknown") == "sandbox"  # safe default = jailed
    assert S.create_session("jailed", isolation="sandbox").env()["LUNAMOTH_PY_BACKEND"] == "sandbox"
    assert S.create_session("trusted", isolation="admin").env()["LUNAMOTH_PY_BACKEND"] == "admin"


def test_set_isolation_writes_both_stores_in_lockstep():
    """SessionMeta.set_isolation is the ONE writer of both stores — session.json (the
    jail authority via env()) AND the config.json mirror (isolation + py_backend) — so
    they can never drift. A config-only write once left the toggle a no-op on the jail."""
    meta = S.create_session("iso", isolation="admin")
    meta.config_path.write_text(
        json.dumps({"model": "m", "isolation": "admin", "py_backend": "admin"}), encoding="utf-8")
    assert meta.set_isolation("sandbox") == "sandbox"
    # session.json (authority): reloads as sandbox, env() jails the next child
    reloaded = S.load_session("iso")
    assert reloaded.isolation == "sandbox"
    assert reloaded.env()["LUNAMOTH_PY_BACKEND"] == "sandbox"
    # config.json mirror: isolation + py_backend updated in lockstep, model preserved
    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    assert cfg["isolation"] == "sandbox" and cfg["py_backend"] == "sandbox" and cfg["model"] == "m"
    with pytest.raises(ValueError):
        meta.set_isolation("vmware")


def test_set_isolation_leaves_missing_config_untouched():
    """The config mirror is best-effort: a missing config.json is NOT rewritten from
    scratch (that would wipe the chara's model/etc) — the authority still updates."""
    meta = S.create_session("nocfg", isolation="admin")
    if meta.config_path.exists():
        meta.config_path.unlink()
    meta.set_isolation("sandbox")
    assert S.load_session("nocfg").isolation == "sandbox"  # authority updated
    assert not meta.config_path.exists()  # no fresh config written


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
