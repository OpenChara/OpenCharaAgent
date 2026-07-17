"""EnvState.permissions(): the single typed snapshot of (isolation, network,
writable_paths) that every tool runner reads, so the facts can't drift between
the foreground, background, and PTY paths."""
from __future__ import annotations

from chara.core.state import EnvState, Permissions


def test_permissions_reflects_state_and_defaults(tmp_path):
    st = EnvState(tmp_path / "env.json")
    perms = st.permissions()
    assert isinstance(perms, Permissions)
    # Defaults: jailed + network ON (owner 2026-06-15) + no extra writable paths.
    assert perms.isolation == "sandbox"
    assert perms.network_on is True
    assert perms.writable_paths == []


def test_permissions_tracks_mutations(tmp_path):
    st = EnvState(tmp_path / "env.json")
    st.set_network(False)
    st.add_writable_path("/tmp/extra")
    perms = st.permissions()
    assert perms.network_on is False
    assert "/tmp/extra" in perms.writable_paths


def test_permissions_is_an_immutable_snapshot(tmp_path):
    st = EnvState(tmp_path / "env.json")
    perms = st.permissions()
    # frozen dataclass — a consumer can't accidentally mutate shared state.
    import dataclasses
    try:
        perms.network_on = False  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised


def test_missing_network_key_defaults_on_not_off(tmp_path):
    """REGRESSION: an env_status.json from a build predating network-on-by-default
    (owner 2026-06-15) carries no network_access key; it must default to
    DEFAULT_STATUS's True — the old False default silently flipped a chara offline."""
    (tmp_path / "env.json").write_text(
        '{"writable_paths": [], "rest_until": 0.0}', encoding="utf-8"
    )
    st = EnvState(tmp_path / "env.json")
    assert st.load()["network_access"] is True   # backfilled from DEFAULT_STATUS
    assert st.permissions().network_on is True
    # An EXPLICIT off is still respected — the default never overrides a choice.
    st.set_network(False)
    assert st.permissions().network_on is False


def test_isolation_follows_the_backend_authority_not_a_stored_copy(tmp_path, monkeypatch):
    """REGRESSION: isolation must come from the ONE authority (CHARA_PY_BACKEND),
    never a per-sandbox env_status.json copy. An `admin` chara whose env file was
    never seeded (the default for network-on charas) used to be silently sandboxed."""
    # A stale on-disk copy says "sandbox" — it must NOT win.
    (tmp_path / "env.json").write_text('{"isolation": "sandbox", "network_access": true}', encoding="utf-8")
    monkeypatch.setenv("CHARA_PY_BACKEND", "admin")
    st = EnvState(tmp_path / "env.json")
    assert st.permissions().isolation == "admin"   # authority wins over the stale copy
    # And the legacy key is dropped from the dict on load (no misleading reader).
    assert "isolation" not in st.load()


def test_isolation_defaults_to_sandbox_when_backend_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CHARA_PY_BACKEND", raising=False)
    st = EnvState(tmp_path / "env.json")
    assert st.permissions().isolation == "sandbox"
