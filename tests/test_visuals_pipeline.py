"""R9 — the in-app visuals pipeline (card → brief → image → optional matte).

No network: the brief LLM and the Ark image client are injected/mocked. Confirms
the brief uses the INJECTED model (not a hard-coded gemini), the prompt craft, and
the honest optional-matte behavior.
"""
from __future__ import annotations

import pytest

from lunamoth.visuals import pipeline


_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"

CARD = {
    "data": {
        "name": "Test Chara",
        "description": "A tidy archivist.",
        "personality": "meticulous, warm",
        "extensions": {"lunamoth": {"tagline": "keeper of records",
                                    "theme": {"primary": "#3A7"}}},
        "character_book": {"entries": [{"content": "Lives in a brass library."}]},
    }
}


@pytest.fixture(autouse=True)
def key_present(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "sk-img-test")


# --- brief --------------------------------------------------------------------

def test_card_text_gathers_identity_and_world():
    txt = pipeline.card_text(CARD)
    assert "NAME: Test Chara" in txt
    assert "PERSONALITY: meticulous, warm" in txt
    assert "TAGLINE: keeper of records" in txt
    assert "WORLD-NOTE: Lives in a brass library." in txt


def test_build_brief_uses_injected_llm_and_card_theme():
    calls = {}

    def fake_llm(system, user):
        calls["system"], calls["user"] = system, user
        return '{"appearance":"tall", "palette":"green", "world":"a library", "theme":"#000"}'

    brief = pipeline.build_brief(CARD, fake_llm)
    # the injected model was used (no hard-coded gemini/openrouter anywhere)
    assert "character designer" in calls["system"].lower()
    assert "Test Chara" in calls["user"]
    # the card's own theme color wins over the model's
    assert brief["theme"] == "#3A7"
    assert brief["appearance"] == "tall"


def test_parse_brief_tolerates_markdown_fences():
    out = pipeline.parse_brief('```json\n{"appearance":"x"}\n```')
    assert out["appearance"] == "x"


def test_parse_brief_empty_is_error():
    with pytest.raises(RuntimeError):
        pipeline.parse_brief("   ")


# --- prompt craft -------------------------------------------------------------

def test_prompt_for_each_kind_embeds_brief():
    brief = {"appearance": "AAA", "palette": "PPP", "world": "WWW", "theme": "#123"}
    sp = pipeline.prompt_for("sprite", brief)
    assert "AAA" in sp and "PPP" in sp and pipeline.STYLE_REAL in sp
    av = pipeline.prompt_for("avatar", brief)
    assert "AAA" in av and "#123" in av and pipeline.CHIBI in av
    bg = pipeline.prompt_for("background", brief)
    assert "WWW" in bg
    with pytest.raises(ValueError):
        pipeline.prompt_for("nope", brief)


# --- generate -----------------------------------------------------------------

def _fixed_brief():
    return {"appearance": "a", "palette": "p", "world": "w", "theme": "#111"}


def test_generate_avatar_no_matte_by_default():
    out = pipeline.generate(
        CARD, "avatar",
        llm_call=lambda s, u: "{}",
        brief=_fixed_brief(),
        ark_generate=lambda prompt, size: ["http://x/a.png"],
        download_bytes=lambda url: _FAKE_PNG,
    )
    assert out["data"] == _FAKE_PNG
    assert out["mime"] == "image/png"
    assert out["matted"] is False
    assert out["kind"] == "avatar"


def test_generate_sprite_matte_skipped_when_no_model(monkeypatch):
    # sprite defaults to matte=True, but with no matte deps/model it is skipped
    # honestly (image still returned, matted=False, a note explains).
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: False)
    out = pipeline.generate(
        CARD, "sprite",
        llm_call=lambda s, u: "{}",
        brief=_fixed_brief(),
        ark_generate=lambda prompt, size: ["http://x/s.png"],
        download_bytes=lambda url: _FAKE_PNG,
    )
    assert out["data"] == _FAKE_PNG
    assert out["matted"] is False
    assert "matte skipped" in out["note"]


def test_generate_sprite_mattes_when_available(monkeypatch):
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: True)
    monkeypatch.setattr(pipeline._matte, "is_installed", lambda mid: True)
    monkeypatch.setattr(pipeline._matte, "selected_model", lambda: "birefnet-general")
    monkeypatch.setattr(pipeline._matte, "cut", lambda data, model_id=None: b"CUT-RGBA")
    out = pipeline.generate(
        CARD, "sprite",
        llm_call=lambda s, u: "{}",
        brief=_fixed_brief(),
        ark_generate=lambda prompt, size: ["http://x/s.png"],
        download_bytes=lambda url: _FAKE_PNG,
    )
    assert out["data"] == b"CUT-RGBA"
    assert out["matted"] is True


def test_generate_no_image_key_is_error(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("LUNAMOTH_HOME", "/nonexistent-empty-home-xyz")
    with pytest.raises(RuntimeError, match="image key"):
        pipeline.generate(CARD, "avatar", llm_call=lambda s, u: "{}", brief=_fixed_brief())


def test_generate_empty_result_is_error():
    with pytest.raises(RuntimeError, match="no result"):
        pipeline.generate(
            CARD, "avatar", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
            ark_generate=lambda prompt, size: [],
        )


def test_generate_non_image_body_is_error():
    with pytest.raises(RuntimeError, match="image"):
        pipeline.generate(
            CARD, "avatar", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
            ark_generate=lambda prompt, size: ["http://x/err.html"],
            download_bytes=lambda url: b"<html>error</html>",
        )


def test_generate_unknown_kind_is_error():
    with pytest.raises(ValueError):
        pipeline.generate(CARD, "nope", llm_call=lambda s, u: "{}")
