"""SEC-2: the provider api_key is GLOBAL, never copied into a per-session config.

A living chara's session config holds only non-secret overrides; the key is
resolved at load from the global keyring (~/.lunamoth/desktop.json). These pin
that contract: global resolution, env override, save never persists the secret
into a session, and a legacy embedded key is stripped on read.
"""
from __future__ import annotations

import json

import pytest

from lunamoth.session import settings as S


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    """Point the (import-pinned) CONFIG_DIR/CONFIG_PATH at a per-session dir under
    a temp LUNAMOTH_HOME, so _is_session_config() and global_api_key() resolve there."""
    home = tmp_path / "home"
    (home / "sessions" / "probe").mkdir(parents=True)
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sess = (home / "sessions" / "probe").resolve()
    monkeypatch.setattr(S, "CONFIG_DIR", sess)
    monkeypatch.setattr(S, "CONFIG_PATH", sess / "config.json")
    return home, sess


def _write_global_key(home, key="sk-GLOBAL"):
    (home / "desktop.json").write_text(
        json.dumps({"provider": "openrouter", "api_key": key}), encoding="utf-8")


def _write_session(sess, data):
    (sess / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_session_resolves_key_from_global_keyring(session_env):
    home, sess = session_env
    _write_global_key(home, "sk-GLOBAL")
    _write_session(sess, {"provider": "openrouter", "model": "m"})  # NO api_key
    assert S.load_settings().api_key == "sk-GLOBAL"


def test_env_overrides_global(session_env, monkeypatch):
    home, sess = session_env
    _write_global_key(home, "sk-GLOBAL")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ENV")
    _write_session(sess, {"provider": "openrouter"})
    assert S.load_settings().api_key == "sk-ENV"


def test_save_settings_never_persists_key_into_session(session_env):
    home, sess = session_env
    S.save_settings(S.Settings(provider="openrouter", api_key="sk-LEAK", model="m"))
    raw = json.loads((sess / "config.json").read_text(encoding="utf-8"))
    assert not raw.get("api_key")            # secret not written
    assert raw.get("provider") == "openrouter"  # non-secret overrides kept


def test_load_strips_legacy_embedded_session_key(session_env):
    home, sess = session_env
    _write_global_key(home, "sk-GLOBAL")
    _write_session(sess, {"provider": "openrouter", "api_key": "sk-STALE"})
    st = S.load_settings()
    assert st.api_key == "sk-GLOBAL"         # global wins over the stale copy
    raw = json.loads((sess / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw              # and the stale copy is stripped from disk


def test_load_folds_orphan_session_key_instead_of_destroying_it(session_env):
    """A session config carrying a legacy api_key with an EMPTY keyring must NOT have its
    only copy destroyed: it's folded into the keyring (preserved), then the disk copy is
    stripped. Regression — the strip used to run unconditionally for session configs,
    orphaning the sole key."""
    home, sess = session_env
    # no global key written → the keyring is empty; the session config is the only copy
    _write_session(sess, {"provider": "openrouter", "api_key": "sk-ONLYCOPY"})
    st = S.load_settings()
    assert st.api_key == "sk-ONLYCOPY"                            # resolved (folded into keyring)
    assert S.global_api_key("openrouter", "") == "sk-ONLYCOPY"   # preserved in the keyring
    raw = json.loads((sess / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw                                  # stripped from disk (now safe)


def test_bulk_key_update_is_noop_after_sec2():
    from lunamoth.server import hub as H
    assert H.key_update_candidates() == []
    assert H.apply_default_key(["anything"]) == {"updated": [], "skipped": [], "candidates": []}


def test_named_key_resolved_by_route(session_env):
    home, sess = session_env
    (home / "desktop.json").write_text(json.dumps({
        "provider": "openrouter", "api_key": "sk-DEFAULT",
        "keys": {"alt": {"provider": "openrouter",
                         "base_url": "https://alt.example/v1", "api_key": "sk-ALT"}},
    }), encoding="utf-8")
    # a chara on the alt route resolves the alt key (multi-key preserved)
    _write_session(sess, {"provider": "openrouter", "base_url": "https://alt.example/v1"})
    assert S.load_settings().api_key == "sk-ALT"
    # a chara on the default route resolves the default key
    _write_session(sess, {"provider": "openrouter", "base_url": ""})
    assert S.load_settings().api_key == "sk-DEFAULT"


def test_save_global_key_writes_keyring_and_resolves(session_env):
    """The terminal setup path writes its key into the keyring (the ONE store) and
    makes it the active default route — never a config.json copy."""
    home, _ = session_env
    label = S.save_global_key("openrouter", "https://openrouter.ai/api/v1", "sk-NEW", model="m/x")
    raw = json.loads((home / "desktop.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw                       # never a top-level secret
    assert raw["keys"][label]["api_key"] == "sk-NEW"
    assert raw["active_key_label"] == label
    assert S.global_api_key("openrouter", "https://openrouter.ai/api/v1") == "sk-NEW"  # explicit route
    assert S.global_api_key("openrouter", "") == "sk-NEW"                              # default route
    # Re-run on the same route reuses the entry — no duplicate label, secret updated.
    S.save_global_key("openrouter", "https://openrouter.ai/api/v1", "sk-NEW2")
    raw2 = json.loads((home / "desktop.json").read_text(encoding="utf-8"))
    assert list(raw2["keys"]) == [label]
    assert raw2["keys"][label]["api_key"] == "sk-NEW2"
    assert S.save_global_key("openrouter", "x", "") == ""  # empty key → no-op


def test_load_folds_legacy_global_config_key_into_keyring(tmp_path, monkeypatch):
    """The OLD terminal store: a key embedded in the GLOBAL config.json. On load it is
    folded into the keyring (not orphaned) and stripped from config.json — config.json
    is no longer a key store, so the second-copy drift can't happen."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(S, "CONFIG_DIR", home.resolve())              # global config dir (NOT under sessions/)
    monkeypatch.setattr(S, "CONFIG_PATH", (home / "config.json").resolve())
    (home / "config.json").write_text(json.dumps({
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-OLDCLI", "model": "m"}), encoding="utf-8")
    st = S.load_settings()
    assert st.api_key == "sk-OLDCLI"                                  # resolved via the keyring, not orphaned
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in cfg                                       # stripped from config.json
    desk = json.loads((home / "desktop.json").read_text(encoding="utf-8"))
    assert any(v.get("api_key") == "sk-OLDCLI" for v in desk["keys"].values())  # now in the keyring


def test_migrate_legacy_default_key_folds_into_keyring(session_env):
    """A legacy top-level api_key is folded into the keyring (the one store): an entry
    on the top-level route, active_key_label set, and the top-level secret dropped."""
    home, _ = session_env
    (home / "desktop.json").write_text(json.dumps({
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-LEGACY", "model": "m/x",
    }), encoding="utf-8")
    S.migrate_legacy_default_key()
    raw = json.loads((home / "desktop.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw                      # top-level secret dropped
    assert raw["active_key_label"] == "OpenRouter"   # active pointer set to the new entry
    entry = raw["keys"]["OpenRouter"]
    assert entry["api_key"] == "sk-LEGACY"
    assert entry["base_url"] == "https://openrouter.ai/api/v1" and entry["model"] == "m/x"
    # idempotent: a second run is a byte-identical no-op
    before = (home / "desktop.json").read_text(encoding="utf-8")
    S.migrate_legacy_default_key()
    assert (home / "desktop.json").read_text(encoding="utf-8") == before


def test_migrate_reuses_existing_route_entry_without_clobber(session_env):
    """When the keyring already has an entry on the top-level route, migration REUSES it
    (no synthetic duplicate, the existing secret untouched) and just drops the top-level."""
    home, _ = session_env
    (home / "desktop.json").write_text(json.dumps({
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-TOP",
        "keys": {"mine": {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                          "api_key": "sk-MINE", "model": "m"}},
    }), encoding="utf-8")
    S.migrate_legacy_default_key()
    raw = json.loads((home / "desktop.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw
    assert list(raw["keys"]) == ["mine"]                # reused — no synthetic entry
    assert raw["keys"]["mine"]["api_key"] == "sk-MINE"  # existing secret untouched
    assert raw["active_key_label"] == "mine"


def test_resolve_named_key_reads_keyring(session_env):
    """resolve_named_key returns a keyring entry's provider/base_url/api_key/model
    (used by /provider to switch a chara's provider); empty for unknown/keyless."""
    home, _ = session_env
    (home / "desktop.json").write_text(json.dumps({
        "provider": "openrouter", "api_key": "sk-TOP",
        "keys": {
            "alt": {"provider": "openai", "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-ALT", "model": "gpt-x"},
            "keyless": {"provider": "x", "base_url": "y"},  # no api_key → not usable
        },
    }), encoding="utf-8")
    got = S.resolve_named_key("alt")
    assert got == {"provider": "openai", "base_url": "https://api.openai.com/v1",
                   "api_key": "sk-ALT", "model": "gpt-x"}
    assert S.resolve_named_key("keyless") == {}   # keyless entry is unusable
    assert S.resolve_named_key("nope") == {}      # unknown label
    assert S.resolve_named_key("") == {}


def test_global_vision_route_uses_its_own_provider(session_env):
    """读图 rides its OWN provider (vision_provider = a keyring label) + vision_model,
    independent of the main text default — so OpenRouter text + a DashScope vision
    model works. Falls back to the main default when vision_provider is unset."""
    home, _ = session_env
    (home / "desktop.json").write_text(json.dumps({
        # main text default = OpenRouter
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-OR",
        "vision_model": "qwen-vl-max",
        "vision_provider": "dashscope",
        "keys": {"dashscope": {"provider": "openai_compatible",
                               "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                               "api_key": "sk-DASH", "model": "qwen-vl-max"}},
    }), encoding="utf-8")
    route = S.global_vision_route()
    assert route["model"] == "qwen-vl-max"
    assert route["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"  # NOT openrouter
    assert route["api_key"] == "sk-DASH"

    # vision_provider unset → falls back to the main text default route
    (home / "desktop.json").write_text(json.dumps({
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-OR",
        "vision_model": "some/vlm",
    }), encoding="utf-8")
    fb = S.global_vision_route()
    assert fb["base_url"] == "https://openrouter.ai/api/v1" and fb["api_key"] == "sk-OR"

    # no vision_model → no aux vision at all
    (home / "desktop.json").write_text(json.dumps({"provider": "openrouter"}), encoding="utf-8")
    assert S.global_vision_route() == {}


def test_task_defaults_overlays_per_task_provider(session_env):
    """task_defaults swaps in a saved provider's route for an aux task (card /
    image-prompt / vision …), leaving the model id to the caller; empty label or a
    keyless entry → defaults unchanged (falls back to the main default)."""
    from lunamoth.server.hub import config as C
    home, _ = session_env
    (home / "desktop.json").write_text(json.dumps({
        "keys": {"dash": {"provider": "openai_compatible",
                          "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "api_key": "sk-DASH"}},
    }), encoding="utf-8")
    base = {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-OR"}
    routed = C.task_defaults(base, "dash")
    assert routed["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert routed["api_key"] == "sk-DASH" and routed["provider"] == "openai_compatible"
    assert base["base_url"] == "https://openrouter.ai/api/v1"  # original untouched
    # no label / unknown label → unchanged (main default)
    assert C.task_defaults(base, "") is base
    assert C.task_defaults(base, "ghost") == base
