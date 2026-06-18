"""Wake must never freeze a persona-less / greeting-less chara.

Root-fix for the recurring "avatar is Q + first message disappeared" bug: the
web wake editor round-trips the whole card through UI fields and re-submits it,
but it renders no field for mes_example/system_prompt/post_history_instructions
and a field value() can come back blank — so an empty submission would overwrite
the source's persona, first_mes, and avatar declaration with "". wake() now
MERGES the edit onto the freshly-loaded SOURCE card: an empty edited field keeps
the source value (_merge_preserving), so the frozen chara always carries the full
content while a genuine (non-empty) edit still wins.
"""
import json

import pytest


def test_merge_preserving_blank_never_overwrites():
    from lunamoth.server.hub import _merge_preserving

    src = {
        "description": "the real persona", "first_mes": "hello there",
        "system_prompt": "sys", "mes_example": "ex",
        "extensions": {"lunamoth": {"avatar_file": "a.png", "wishes": ["w"]}},
    }
    edit = {  # the blanked submission the wake editor can send
        "description": "", "first_mes": "", "system_prompt": "", "mes_example": "",
        "extensions": {"lunamoth": {}},
    }
    out = _merge_preserving(src, edit)
    assert out["description"] == "the real persona"   # blank kept source
    assert out["first_mes"] == "hello there"
    assert out["system_prompt"] == "sys"
    assert out["mes_example"] == "ex"
    assert out["extensions"]["lunamoth"]["avatar_file"] == "a.png"  # deep-merged
    assert out["extensions"]["lunamoth"]["wishes"] == ["w"]

    # a genuine (non-empty) edit still wins
    assert _merge_preserving(src, {"description": "new"})["description"] == "new"
    # a new key the source lacks is added
    assert _merge_preserving(src, {"tagline": "hi"})["tagline"] == "hi"


@pytest.fixture
def clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sb"))
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(
        json.dumps({"provider": "mock", "base_url": "", "api_key": "", "model": "mock"})
    )


def test_wake_blank_submission_keeps_source_persona_and_avatar(clean_home):
    """A blanked wake submission must still freeze the full source persona +
    first_mes + avatar declaration (the actual reported regression)."""
    from lunamoth.server import hub
    import lunamoth.session.sessions as S

    quinn = next(c["path"] for c in hub.list_cards()
                 if c["name"] == "Quinn" and "sessions" not in c["path"])
    blanked = {"name": "Quinn", "data": {
        "name": "Quinn", "description": "", "personality": "", "scenario": "",
        "first_mes": "", "mes_example": "", "system_prompt": "",
        "post_history_instructions": "",
    }}
    entry = hub.wake(quinn, name="QuinnTest", card_data=blanked)
    meta = S.load_session(entry["name"])
    d = json.loads((meta.root / "card.json").read_text(encoding="utf-8"))["data"]

    assert len(d["description"]) > 100, "source persona must survive a blank wake"
    assert len(d["first_mes"]) > 10, "source first_mes must survive a blank wake"
    ext = (d.get("extensions") or {}).get("lunamoth") or {}
    assert ext.get("avatar_file") == "avatar.png", "avatar declaration must survive"
