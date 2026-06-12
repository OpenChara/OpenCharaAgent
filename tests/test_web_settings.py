from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / "src" / "lunamoth" / "front" / "web"


def test_settings_key_rotation_prompt_uses_safe_rpc_and_i18n_strings():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    assert "promptKeyUpdate(saved.key_update_candidates)" in app
    assert 'hub.call("defaults.apply_key", { names }' in app
    assert "key-update-title" in i18n
    assert "key-update-sub" in i18n
    # The prompt lists chara identity/model metadata only; it must never try to
    # render old/new key values from RPC payloads.
    prompt_src = app[app.index("function promptKeyUpdate"):app.index("/* settings interactions */")]
    assert "api_key" not in prompt_src
    assert "has_key" not in prompt_src


def test_board_chip_has_specific_invalid_key_copy():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    assert 'kind === "auth"' in app
    assert 't("board-key-invalid")' in app
    assert "board-key-tip" in i18n
