"""R9 — the in-app visuals pipeline (card → brief → image → optional matte).

No network: the brief LLM and the Ark image client are injected/mocked. Confirms
the brief uses the INJECTED model (not a hard-coded gemini), the prompt craft, and
the honest optional-matte behavior.
"""
from __future__ import annotations

import json

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
def key_present(monkeypatch, tmp_path):
    # unified: an image key is "present" when the selected provider has a keyring
    # entry (no ARK_API_KEY env path any more).
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(json.dumps({
        "image_provider": "volcano", "image_model": "doubao-seedream-x",
        "keys": {"火山": {"provider": "volcano", "api_key": "sk-img-test"}},
    }), encoding="utf-8")
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))


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
    # A brief WITHOUT an LM-chosen style falls back to the anime house defaults
    # (back-compat: older briefs / a model that skipped the field).
    brief = {"appearance": "AAA", "palette": "PPP", "world": "WWW", "theme": "#123"}
    sp = pipeline.prompt_for("sprite", brief)
    assert "AAA" in sp and "PPP" in sp and pipeline.STYLE_REAL in sp
    av = pipeline.prompt_for("avatar", brief)
    assert "AAA" in av and "#123" in av and pipeline.CHIBI in av
    bg = pipeline.prompt_for("background", brief)
    assert "WWW" in bg
    with pytest.raises(ValueError):
        pipeline.prompt_for("nope", brief)


def test_prompt_for_honors_brief_style_over_anime_default():
    # The whole point of the un-lock: when the brief carries a style, it drives the
    # prompt and the anime house default is NOT injected — so a realistic/other
    # card is no longer forced into 二次元.
    brief = {"appearance": "AAA", "palette": "PPP", "world": "WWW", "theme": "#123",
             "style": "cinematic photorealistic rendering, soft key light"}
    # sprite + background follow the chosen style; avatar/stickers stay chibi by design.
    for kind in ("sprite", "background"):
        assert "photorealistic" in pipeline.prompt_for(kind, brief)
    sp = pipeline.prompt_for("sprite", brief)
    assert pipeline.STYLE_REAL not in sp  # anime default suppressed when a style is set
    # avatar is intentionally ALWAYS chibi (an app icon), regardless of the chosen style
    av = pipeline.prompt_for("avatar", brief)
    assert pipeline.CHIBI in av and "photorealistic" not in av


def test_build_brief_normalizes_style_key():
    brief = pipeline.build_brief(
        CARD, lambda s, u: '{"appearance":"a","palette":"p","world":"w","theme":"#000"}')
    assert "style" in brief  # always present (empty when the model omits it)


# --- generate -----------------------------------------------------------------

def _fixed_brief():
    return {"appearance": "a", "palette": "p", "world": "w", "theme": "#111"}


def test_generate_avatar_no_matte_by_default():
    out = pipeline.generate(
        CARD, "avatar",
        llm_call=lambda s, u: "{}",
        brief=_fixed_brief(),
        ark_generate=lambda prompt, size, refs=None: ["http://x/a.png"],
        download_bytes=lambda url: _FAKE_PNG,
    )
    assert out["data"] == _FAKE_PNG
    assert out["mime"] == "image/png"
    assert out["ext"] == "png"
    assert out["matted"] is False
    assert out["kind"] == "avatar"


def test_generate_reports_true_ext_for_jpeg():
    # an un-matted JPEG result must report ext=jpg (so asset_save's magic check
    # accepts it) — not a hardcoded png.
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIFblah"
    out = pipeline.generate(
        CARD, "background", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
        ark_generate=lambda prompt, size, refs=None: ["http://x/b.jpg"],
        download_bytes=lambda url: jpeg,
    )
    assert out["ext"] == "jpg" and out["mime"] == "image/jpeg" and out["matted"] is False


def test_generate_sprite_matte_skipped_when_no_model(monkeypatch):
    # sprite defaults to matte=True, but with no matte deps/model it is skipped
    # honestly (image still returned, matted=False, a note explains).
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: False)
    out = pipeline.generate(
        CARD, "sprite",
        llm_call=lambda s, u: "{}",
        brief=_fixed_brief(),
        ark_generate=lambda prompt, size, refs=None: ["http://x/s.png"],
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
        ark_generate=lambda prompt, size, refs=None: ["http://x/s.png"],
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
            ark_generate=lambda prompt, size, refs=None: [],
        )


def test_generate_non_image_body_is_error():
    with pytest.raises(RuntimeError, match="image"):
        pipeline.generate(
            CARD, "avatar", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
            ark_generate=lambda prompt, size, refs=None: ["http://x/err.html"],
            download_bytes=lambda url: b"<html>error</html>",
        )


def test_generate_unknown_kind_is_error():
    with pytest.raises(ValueError):
        pipeline.generate(CARD, "nope", llm_call=lambda s, u: "{}")


def test_generate_passes_refs_to_image_client():
    seen = {}

    def fake_ark(prompt, size, refs=None):
        seen["refs"] = refs
        return ["http://x/a.png"]

    pipeline.generate(
        CARD, "avatar", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
        refs=["data:image/png;base64,AAAA"],
        ark_generate=fake_ark, download_bytes=lambda url: _FAKE_PNG,
    )
    assert seen["refs"] == ["data:image/png;base64,AAAA"]


# --- restored kinds: keyvisual (anchor) + stickers ----------------------------

def _white_sheet(rows: int = 3, cols: int = 3, cell: int = 120) -> bytes:
    """A flat WHITE sheet with a red blob centered in each cell — so the white-bg
    removal keeps the blob and drops the white (a realistic slice fixture)."""
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (cols * cell, rows * cell), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for i in range(rows):
        for j in range(cols):
            cx, cy = j * cell + cell // 2, i * cell + cell // 2
            d.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_brief_system_recommends_anime_default_not_forbids():
    sysmsg = pipeline.BRIEF_SYSTEM.lower()
    # the old hard ban is gone; anime/gacha is now the recommended DEFAULT...
    assert "never force an anime" not in sysmsg
    assert "default" in sysmsg and "anime" in sysmsg and "gacha" in sysmsg
    # ...but the model is still free to depart for a fitting medium
    assert "depart" in sysmsg or "photoreal" in sysmsg


def test_prompt_for_keyvisual_is_identity_settei_sheet():
    brief = {"appearance": "AAA", "palette": "PPP", "world": "WWW", "theme": "#123"}
    kv = pipeline.prompt_for("keyvisual", brief)
    assert "AAA" in kv and "turnaround" in kv.lower()
    assert pipeline.STYLE_REAL in kv          # anime default when brief has no style
    assert "reference" in kv.lower()          # it's the identity anchor


def test_prompt_for_stickers_has_nine_expressions_on_white():
    brief = {"appearance": "AAA", "palette": "PPP", "theme": "#123"}
    st = pipeline.prompt_for("stickers", brief)
    assert "3x3" in st and pipeline.CHIBI in st and "#FFFFFF" in st  # white bg, not green
    assert "do NOT draw" in st  # the no-label guard
    for e in pipeline.EXPR9:
        assert e in st


def test_generate_stickers_slices_nine_cells(monkeypatch):
    # no matte model → white-bg fallback; the sheet is sliced into 9 PNG cells.
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: False)
    out = pipeline.generate(
        CARD, "stickers", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
        ark_generate=lambda prompt, size, refs=None: ["http://x/sheet.png"],
        download_bytes=lambda url: _white_sheet(),
    )
    assert out["kind"] == "stickers" and "data" not in out
    assert len(out["stickers"]) == 9
    assert out["matted"] is False and "white-background" in out["note"]
    for c in out["stickers"]:
        assert c[:8] == b"\x89PNG\r\n\x1a\n"


def test_generate_stickers_mattes_each_cell_when_available(monkeypatch):
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: True)
    monkeypatch.setattr(pipeline._matte, "is_installed", lambda mid: True)
    monkeypatch.setattr(pipeline._matte, "selected_model", lambda: "birefnet-general")
    cuts = {"n": 0}

    def fake_cut(data, model_id=None):
        cuts["n"] += 1
        return b"\x89PNG\r\n\x1a\nCUT"

    monkeypatch.setattr(pipeline._matte, "cut", fake_cut)
    out = pipeline.generate(
        CARD, "stickers", llm_call=lambda s, u: "{}", brief=_fixed_brief(),
        ark_generate=lambda prompt, size, refs=None: ["http://x/sheet.png"],
        download_bytes=lambda url: _white_sheet(),
    )
    assert cuts["n"] == 9 and out["matted"] is True and len(out["stickers"]) == 9


def test_slice_grid_cuts_equal_cell_count():
    from lunamoth.content import imaging
    cells = imaging.slice_grid(_white_sheet(rows=3, cols=3, cell=99), 3, 3)
    assert len(cells) == 9
    assert all(c[:8] == b"\x89PNG\r\n\x1a\n" for c in cells)


def test_cut_white_bg_removes_white_keeps_subject():
    import io

    from PIL import Image

    from lunamoth.visuals import matte

    img = Image.new("RGB", (60, 60), (255, 255, 255))
    for x in range(20, 40):
        for y in range(20, 40):
            img.putpixel((x, y), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    res = Image.open(io.BytesIO(matte.cut_white_bg(buf.getvalue()))).convert("RGBA")
    assert res.size[0] <= 24 and res.size[1] <= 24          # autocropped to the blob
    assert res.getpixel((res.size[0] // 2, res.size[1] // 2))[3] == 255  # subject opaque
