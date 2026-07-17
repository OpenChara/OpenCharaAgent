"""The terminal key-entry flows (setup wizard / TUI welcome) must land the API key
in the GLOBAL keyring, NOT a config.json copy.

save_settings() no longer persists api_key (the keyring is the ONE key store), so
every terminal path that collects a key must route it through save_global_key().
These pin that: after the flow global_api_key() returns the entered key, and the
written config.json carries NO api_key.
"""
from __future__ import annotations

import json

import pytest

from chara.session import settings as S


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    """Hermetic global home + pinned config dir under tmp, no inherited env key."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("CHARA_HOME", str(home))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # CONFIG_DIR/CONFIG_PATH are pinned at import — repoint them at the temp dir.
    monkeypatch.setattr(S, "CONFIG_DIR", cfg.resolve())
    monkeypatch.setattr(S, "CONFIG_PATH", (cfg / "config.json").resolve())
    return home, cfg


def _drive_wizard(monkeypatch, answers, choices, *, getpass_value=""):
    """Run run_wizard() non-interactively: stub stdin/getpass/_test and feed answers.

    ``answers`` maps a substring of a free-text prompt to its reply; unmatched text
    prompts return "" (accept the [default]). ``choices`` is the ordered list of
    replies to the numbered menus (provider, character, embodiment) — each menu's
    actual input() prompt is just "choice", so they're consumed in order.
    """
    from chara.front import wizard as W

    monkeypatch.setattr(W.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(W, "_test", lambda settings: True)  # never touch the network

    pending = list(choices)

    def fake_input(prompt: str) -> str:
        if "choice" in prompt:
            return pending.pop(0) if pending else ""
        for needle, reply in answers.items():
            if needle in prompt:
                return reply
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(W.getpass, "getpass", lambda *_a, **_k: getpass_value)
    return W.run_wizard()


def test_wizard_typed_key_lands_in_keyring(wizard_env, monkeypatch):
    home, cfg = wizard_env
    answers = {
        "base_url": "https://relay.example/v1",
        "model": "some-model",
    }
    # Provider menu → the last option (Custom OpenAI-compatible endpoint).
    choices = [str(len(S.PRESETS) + 1)]
    settings = _drive_wizard(monkeypatch, answers, choices, getpass_value="sk-TYPED")

    # The key is resolvable from the global keyring on its explicit route...
    assert S.global_api_key(settings.provider, settings.base_url) == "sk-TYPED"
    # ...and on the active default route too.
    assert S.global_api_key() == "sk-TYPED"
    # ...but the config.json carries NO secret.
    raw = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert not raw.get("api_key")


def test_wizard_preset_provided_key_lands_in_keyring(wizard_env, monkeypatch):
    """A preset that ships its own key (e.g. Ollama's "ollama") must persist too,
    even when the operator types no key at the getpass prompt."""
    home, cfg = wizard_env
    preset_names = list(S.PRESETS.keys())
    if "Ollama (local)" in preset_names:
        idx = preset_names.index("Ollama (local)")
    else:  # find any preset that ships a non-empty api_key
        idx = next(i for i, n in enumerate(preset_names)
                   if (S.PRESETS[n].get("api_key") or ""))
    preset = S.PRESETS[preset_names[idx]]

    settings = _drive_wizard(monkeypatch, {}, [str(idx + 1)], getpass_value="")  # accept preset key

    assert settings.api_key == preset["api_key"]
    assert S.global_api_key(settings.provider, settings.base_url) == preset["api_key"]
    raw = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert not raw.get("api_key")


def test_wizard_empty_key_is_a_noop(wizard_env, monkeypatch):
    """No key typed on a keyless custom endpoint → nothing written to the keyring."""
    home, cfg = wizard_env
    answers = {
        "base_url": "https://noauth.example/v1",
        "model": "m",
    }
    _drive_wizard(monkeypatch, answers, [str(len(S.PRESETS) + 1)], getpass_value="")

    assert S.global_api_key("openai_compatible", "https://noauth.example/v1") == ""
    assert not (home / "desktop.json").exists() or not json.loads(
        (home / "desktop.json").read_text(encoding="utf-8")
    ).get("keys")
