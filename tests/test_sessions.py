import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chara.session import sessions as S


def test_config_isolation_has_a_single_mutating_writer():
    """Guard the one-authority invariant (the isolation/py_backend two-store drift that
    once silently sandboxed an admin chara): config.json's `isolation` mirror may be
    MUTATED only by SessionMeta.set_isolation. The wake builder CREATES it as a dict
    literal (the legitimate initial write); the derived `py_backend` copy is fully
    retired — no config write may reintroduce it. If this fails, a new site is editing
    the mirror behind the single writer's back — route it through set_isolation."""
    src = Path(__file__).resolve().parent.parent / "src" / "chara"
    mutation = re.compile(r'\["isolation"\]\s*=')
    py_backend_write = re.compile(r'\["py_backend"\]\s*=|"py_backend"\s*:')
    iso, pyb = [], []
    for p in sorted(src.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        rel = str(p.relative_to(src))
        if mutation.search(text):
            iso.append(rel)
        if py_backend_write.search(text):
            pyb.append(rel)
    assert iso == ["session/sessions.py"], f"config.json `isolation` mutated outside set_isolation: {iso}"
    assert pyb == [], f"a config write reintroduced the retired py_backend field: {pyb}"


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def test_create_list_delete():
    meta = S.create_session("alpha", isolation="admin", note="x")
    assert meta.sandbox_dir.is_dir()
    assert S.load_session("alpha").isolation == "admin"
    assert [m.name for m in S.list_sessions()] == ["alpha"]
    S.delete_session("alpha")
    assert S.list_sessions() == []


def test_soft_delete_moves_to_trash_frees_name_keeps_data():
    meta = S.create_session("quinn", isolation="sandbox")
    (meta.root / "card.json").write_text('{"v":1}', encoding="utf-8")  # a kept artifact
    res = S.soft_delete_session("quinn")
    assert res["ok"] and res["trash_id"]
    # gone from the roster (→ and from the deck's locked-card listing)
    assert S.list_sessions() == []
    assert not meta.root.exists()  # moved out of the sessions dir
    # data preserved in the trash (recoverable), with an origin manifest
    trashed = S.chara_home() / ".trash" / "sessions" / res["trash_id"]
    assert (trashed / "card.json").read_text(encoding="utf-8") == '{"v":1}'
    assert "quinn" in (trashed / "origin.json").read_text(encoding="utf-8")
    # the name is freed → re-waking the template reuses it cleanly (no -2 drift)
    S.create_session("quinn", isolation="sandbox")
    assert [m.name for m in S.list_sessions()] == ["quinn"]


def test_soft_delete_refuses_a_running_session(monkeypatch):
    S.create_session("busy", isolation="sandbox")
    monkeypatch.setattr(S.SessionMeta, "running_pid", lambda self: 4321)
    with pytest.raises(RuntimeError):
        S.soft_delete_session("busy")
    assert [m.name for m in S.list_sessions()] == ["busy"]  # untouched


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


def _backdate(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_daemon_pid_reused_by_a_stranger_is_stale(monkeypatch):
    """Reboot pid-reuse: a LIVE pid that fails the identity check must read as stale —
    the file is dropped, so start proceeds and stop never signals the stranger."""
    meta = S.create_session("reused")
    meta.daemon_pid_path.write_text(str(os.getpid()), encoding="utf-8")  # alive, but pytest
    _backdate(meta.daemon_pid_path, 3600)  # past the fresh-spawn identity grace
    monkeypatch.setattr(S, "pid_is_chara", lambda pid, session=None: False)  # not one of ours
    assert meta.daemon_pid() is None
    assert not meta.daemon_pid_path.exists()  # stale file removed


def test_daemon_pid_keeps_a_verified_chara_process(monkeypatch):
    meta = S.create_session("ours")
    meta.daemon_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    _backdate(meta.daemon_pid_path, 3600)
    monkeypatch.setattr(S, "pid_is_chara", lambda pid, session=None: True)  # identity passes
    assert meta.daemon_pid() == os.getpid()
    assert meta.daemon_pid_path.exists()


def test_daemon_pid_trusts_a_fresh_record_during_spawn(monkeypatch):
    """A record written moments ago may point at a child still mid fork→exec (its
    cmdline briefly reads as the parent's) — identity must not condemn it yet."""
    meta = S.create_session("fresh")
    meta.daemon_pid_path.write_text(str(os.getpid()), encoding="utf-8")  # just written
    monkeypatch.setattr(S, "pid_is_chara", lambda pid, session=None: False)
    assert meta.daemon_pid() == os.getpid()  # grace window: trusted, not dropped
    assert meta.daemon_pid_path.exists()


def test_dead_daemon_pid_file_is_dropped():
    meta = S.create_session("deadpid")
    meta.daemon_pid_path.write_text("999999999", encoding="utf-8")
    assert meta.daemon_pid() is None
    assert not meta.daemon_pid_path.exists()  # removed so a new start can claim


def test_empty_daemon_pid_is_an_in_flight_claim():
    """cli._start_daemon claims daemon.pid (O_EXCL, empty) BEFORE spawning: a fresh
    empty file is not stale; an orphaned claim expires after the TTL."""
    meta = S.create_session("claiming")
    meta.daemon_pid_path.write_text("", encoding="utf-8")
    assert meta.daemon_pid() is None
    assert meta.daemon_pid_path.exists()  # fresh claim: left for the starter
    _backdate(meta.daemon_pid_path, 2 * S._CLAIM_TTL)
    assert meta.daemon_pid() is None
    assert not meta.daemon_pid_path.exists()  # a crashed starter's claim self-heals


def test_pid_is_chara_reads_the_command_line():
    # argv carrying the package name passes; a plain sleeper does not; nor a dead pid.
    probe = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                              "chara-identity-probe"])
    sleeper = subprocess.Popen(["/bin/sleep", "30"])
    try:
        deadline = time.time() + 5  # give the children a beat to finish exec
        while time.time() < deadline and not S.pid_is_chara(probe.pid):
            time.sleep(0.05)
        assert S.pid_is_chara(probe.pid) is True
        assert S.pid_is_chara(sleeper.pid) is False
    finally:
        for p in (probe, sleeper):
            p.kill()
            p.wait()
    assert S.pid_is_chara(999999999) is False


def test_pid_is_chara_tells_sibling_sessions_apart():
    """Every daemon argv is otherwise identically `-m chara.front.terminal` (the
    session rides only env), so cli._start_daemon stamps an inert `--session NAME`
    marker. With a session given, a DIFFERENT marker is a sibling's daemon (reboot
    pid-reuse across charas once made start-all skip A and A.stop() kill B)."""
    code = "import time; time.sleep(30)"
    alice = subprocess.Popen([sys.executable, "-c", code, "chara", "--session", "alice"])
    bob = subprocess.Popen([sys.executable, "-c", code, "chara", "--session", "bob"])
    old = subprocess.Popen([sys.executable, "-c", code, "chara-pre-marker-daemon"])
    try:
        deadline = time.time() + 5  # give the children a beat to finish exec
        while time.time() < deadline and not (
            S.pid_is_chara(alice.pid, session="alice")
            and S.pid_is_chara(bob.pid, session="bob")
            and S.pid_is_chara(old.pid)
        ):
            time.sleep(0.05)
        assert S.pid_is_chara(alice.pid, session="alice") is True
        assert S.pid_is_chara(bob.pid, session="bob") is True
        # the sibling collision: bob's daemon must NOT pass as alice's (and vice versa)
        assert S.pid_is_chara(bob.pid, session="alice") is False
        assert S.pid_is_chara(alice.pid, session="bob") is False
        # back-compat: a pre-upgrade daemon carries NO marker — chara, just
        # unlabeled: it must not be treated as foreign.
        assert S.pid_is_chara(old.pid, session="alice") is True
        # the bare match (no session asked) is unchanged
        assert S.pid_is_chara(bob.pid) is True
    finally:
        for p in (alice, bob, old):
            p.kill()
            p.wait()


def test_daemon_pid_hands_its_session_name_to_the_identity_check(monkeypatch):
    """daemon_pid() must ask for THIS chara's daemon, not any chara process —
    that session= is what stops a sibling's reused pid from passing."""
    meta = S.create_session("named")
    meta.daemon_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    _backdate(meta.daemon_pid_path, 3600)
    seen = {}

    def fake_identity(pid, session=None):
        seen["session"] = session
        return True

    monkeypatch.setattr(S, "pid_is_chara", fake_identity)
    assert meta.daemon_pid() == os.getpid()
    assert seen["session"] == "named"


def test_stale_running_marker_is_harmless():
    meta = S.create_session("stale")
    meta.pid_path.write_text("999999999", encoding="utf-8")
    assert meta.running_pid() is None
    assert S.load_session("stale").running_marker_stale() is True
    assert S.load_session("stale").status() == "new"


def test_env_points_at_session():
    meta = S.create_session("envy")
    env = meta.env()
    assert env["CHARA_CONFIG_DIR"].endswith("sessions/envy")
    assert env["CHARA_SANDBOX"].endswith("sessions/envy/sandbox")


def test_env_carries_py_backend_so_callers_never_rederive_the_jail():
    """The isolation→backend map has ONE owner now (sessions); env() is the
    complete activation interface and emits CHARA_PY_BACKEND itself, so a
    caller can never drift by forgetting to set the jail."""
    assert S.isolation_to_backend("sandbox") == "sandbox"
    assert S.isolation_to_backend("admin") == "admin"
    assert S.isolation_to_backend("anything-unknown") == "sandbox"  # safe default = jailed
    assert S.create_session("jailed", isolation="sandbox").env()["CHARA_PY_BACKEND"] == "sandbox"
    assert S.create_session("trusted", isolation="admin").env()["CHARA_PY_BACKEND"] == "admin"


def test_set_isolation_writes_both_stores_in_lockstep():
    """SessionMeta.set_isolation is the ONE writer of both stores — session.json (the
    jail authority via env()) AND the config.json mirror (``isolation``) — so they can
    never drift. A config-only write once left the toggle a no-op on the jail."""
    meta = S.create_session("iso", isolation="admin")
    meta.config_path.write_text(
        json.dumps({"model": "m", "isolation": "admin", "py_backend": "admin"}), encoding="utf-8")
    assert meta.set_isolation("sandbox") == "sandbox"
    # session.json (authority): reloads as sandbox, env() jails the next child
    reloaded = S.load_session("iso")
    assert reloaded.isolation == "sandbox"
    assert reloaded.env()["CHARA_PY_BACKEND"] == "sandbox"
    # config.json mirror: isolation updated, the legacy py_backend copy dropped, model kept
    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    assert cfg["isolation"] == "sandbox" and "py_backend" not in cfg and cfg["model"] == "m"
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
            [sys.executable, "-m", "chara.front.cli", *argv],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "CHARA_HOME": str(temp_home), "PYTHONPATH": "src"},
        )

    out = run("new", "beta", "--isolation", "admin")
    assert out.returncode == 0 and "created session 'beta'" in out.stdout
    out = run("ls")
    assert "beta" in out.stdout and "admin" in out.stdout
    out = run("rm", "beta", "-y")
    assert out.returncode == 0
    out = run("ls")
    assert "no chara yet" in out.stdout
