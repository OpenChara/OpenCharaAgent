"""Faithful paste-import of a foreign character card → our V3 card shape.

These pin the PURE mapping (`_foreign_to_card`) across the three shapes a pasted card
can take — ST V2/V3 (`data` block), V1 flat, and character-tavern's flat `definition_*`
API card — plus the compatibility tolerances (absent 理想, derived theme, macros &
lorebook preserved, dangling asset pointers dropped). The deck writer is mocked.
"""
from __future__ import annotations

import base64
import json
import struct

import pytest

from lunamoth.server.hub import cards as C
from lunamoth.server.hub._common import HubRpcError
from lunamoth.server.dispatch import RpcError


def _png_card_bytes(card: dict) -> bytes:
    """A minimal PNG carrying a V2 `chara` tEXt chunk (no real raster — enough to test
    embedded-card extraction; the avatar attach is best-effort and no-ops on it)."""
    payload = base64.b64encode(json.dumps(card).encode("utf-8"))
    body = b"chara\x00" + payload
    text_chunk = struct.pack(">I", len(body)) + b"tEXt" + body + b"\x00\x00\x00\x00"
    iend = struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + text_chunk + iend


# ---- the three shapes ----------------------------------------------------------

_V3 = {
    "spec": "chara_card_v3", "spec_version": "3.0",
    "data": {
        "name": "Elspeth",
        "description": "An old witch. {{char}} watches {{user}} closely.",
        "personality": "wry, patient",
        "scenario": "A mossy cabin.",
        "first_mes": "Mind the cat, {{user}}.",
        "mes_example": "<START>\n{{char}}: hm.",
        "system_prompt": "stay in character",
        "post_history_instructions": "never break character",
        "alternate_greetings": ["A second door creaks open.", ""],
        "creator_notes": "for fun",
        "creator": "amy",
        "tags": ["fantasy", "witch"],
        "character_book": {"name": "wood", "entries": [{"keys": ["cabin"], "content": "moss everywhere"}]},
    },
}

_V1 = {
    "name": "Bob", "description": "A knight. {{char}} guards {{user}}.",
    "personality": "stoic", "scenario": "A keep.", "first_mes": "Halt, {{user}}.",
    "mes_example": "", "character_book": None,
}

# character-tavern API: flat definition_* fields, persona at top level
_API = {
    "name": "Witch", "inChatName": "Morgana",
    "definition_character_description": "A sea witch. {{char}} eyes {{user}}.",
    "definition_personality": "cold",
    "definition_first_message": "You again, {{user}}?",
    "definition_post_history_prompt": "never break character",
    "tags": ["ocean"],
    # V2-style lorebook: entries as a DICT keyed by id (must still be kept)
    "character_book": {"name": "tides", "entries": {"0": {"keys": ["reef"], "content": "sharp coral"}}},
}


def test_v3_data_block_maps_and_keeps_macros():
    card = C._foreign_to_card(_V3)
    d = card["data"]
    assert card["version"] == "1.0" and d["name"] == "Elspeth"
    assert d["description"].count("{{char}}") == 1 and d["first_mes"].count("{{user}}") == 1
    # fields the AI-draft path drops are preserved verbatim on a faithful import:
    assert d["mes_example"].startswith("<START>")
    assert d["system_prompt"] == "stay in character"
    assert d["post_history_instructions"] == "never break character"
    assert d["alternate_greetings"] == ["A second door creaks open."]  # blank dropped
    assert d["tags"] == ["fantasy", "witch"] and d["creator"] == "amy"
    assert d["character_book"]["entries"][0]["keys"] == ["cabin"]


def test_v1_flat_card_maps():
    d = C._foreign_to_card(_V1)["data"]
    assert d["name"] == "Bob" and d["scenario"] == "A keep."
    assert d["first_mes"].count("{{user}}") == 1
    assert "character_book" not in d  # None book → omitted


def test_api_definition_shape_and_dict_lorebook():
    d = C._foreign_to_card(_API)["data"]
    assert d["name"] == "Morgana"  # inChatName wins
    assert d["description"].startswith("A sea witch")  # definition_* read
    assert d["post_history_instructions"] == "never break character"
    # the dict-keyed lorebook is preserved as a list (parity with the native loader)
    assert d["character_book"]["entries"][0]["keys"] == ["reef"]


# ---- compatibility tolerances --------------------------------------------------

def test_polaris_never_imported_and_theme_always_present():
    src = {"data": {**_V3["data"],
                    "extensions": {"lunamoth": {"polaris": "become free", "theme": {}}}}}
    ext = C._foreign_to_card(src)["data"]["extensions"]["lunamoth"]
    assert "polaris" not in ext  # 理想 is the user's — never carried by an imported card
    assert ext["theme"]["primary"].startswith("#") and len(ext["theme"]["primary"]) == 7
    assert ext["source"] == "import"


def test_existing_theme_kept_asset_pointers_dropped_svg_kept():
    src = {"data": {**_V3["data"], "extensions": {"lunamoth": {
        "theme": {"primary": "#123456", "secondary": "#abcdef"},
        "avatar_svg": "<svg/>",          # inline — portable, kept (sanitized at save)
        "avatar_file": "Elspeth.avatar.png",  # sidecar pointer — dangling, dropped
        "assets": {"keyvisual": "x.png"},      # sidecar pointers — dropped
        "force_roleplay": True,                # portable behavior — kept
    }}}}
    ext = C._foreign_to_card(src)["data"]["extensions"]["lunamoth"]
    assert ext["theme"]["primary"] == "#123456"  # the card's own theme wins over derived
    assert "avatar_file" not in ext and "assets" not in ext
    assert ext.get("avatar_svg") == "<svg/>" and ext.get("force_roleplay") is True


def test_seeded_theme_is_stable_and_distinct():
    a = C._foreign_to_card({"data": {"name": "Aaa", "description": "x"}})["data"]["extensions"]["lunamoth"]
    b = C._foreign_to_card({"data": {"name": "Bbb", "description": "x"}})["data"]["extensions"]["lunamoth"]
    assert a["theme"]["primary"] != b["theme"]["primary"]  # derived per identity


# ---- import_foreign_card (parse + save) ----------------------------------------

def test_import_parses_and_saves(monkeypatch):
    saved = {}
    monkeypatch.setattr(C, "save_card", lambda card: saved.update(card=card) or {"path": "/deck/elspeth.json"})
    res = C.import_foreign_card(json.dumps(_V3))
    assert res == {"path": "/deck/elspeth.json", "name": "Elspeth"}
    assert saved["card"]["data"]["name"] == "Elspeth"


def test_import_rejects_non_json():
    with pytest.raises(HubRpcError):
        C.import_foreign_card("not json {")


def test_import_rejects_blank():
    with pytest.raises(RpcError):
        C.import_foreign_card("   ")


def test_import_rejects_non_card():
    with pytest.raises(RpcError):
        C.import_foreign_card(json.dumps({"foo": 1}))  # no name/persona
    with pytest.raises(RpcError):
        C.import_foreign_card(json.dumps({"name": "x"}))  # name but no persona


# ---- PNG drag-drop import ------------------------------------------------------

def test_import_png_card_extracts_embedded(monkeypatch):
    saved = {}
    monkeypatch.setattr(C, "save_card", lambda card: saved.update(card=card) or {"path": "/deck/sera.json"})
    png = _png_card_bytes(_V3)  # V2-style chunk carrying our V3 {data:{...}} object
    res = C.import_foreign_card(png_b64=base64.b64encode(png).decode("ascii"))
    assert res == {"path": "/deck/sera.json", "name": "Elspeth"}
    d = saved["card"]["data"]
    assert d["first_mes"].count("{{user}}") == 1  # persona preserved from the PNG
    assert d["character_book"]["entries"][0]["keys"] == ["cabin"]


def test_import_png_rejects_non_image(monkeypatch):
    with pytest.raises(HubRpcError):
        C.import_foreign_card(png_b64="not base64 @@@")


def test_import_png_without_embedded_card(monkeypatch):
    bare = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    with pytest.raises(HubRpcError):
        C.import_foreign_card(png_b64=base64.b64encode(bare).decode("ascii"))


# ---- the source art is the 立绘/sprite, never the avatar -----------------------

def test_card_entry_shows_source_image_as_sprite_not_avatar(tmp_path):
    # A market card whose cover bytes weren't copied locally keeps only the remote URL.
    # It must surface as the sprite (立绘) for display — never as the avatar.
    card = {"version": "1.0", "name": "Web", "data": {
        "name": "Web", "description": "x",
        "extensions": {"lunamoth": {"theme": {"primary": "#112233"},
                                    "source_image": "https://cards.example.com/a/b.png"}}}}
    p = tmp_path / "web.json"
    p.write_text(json.dumps(card), encoding="utf-8")
    entry = C._card_entry(p, False, {})
    assert entry["sprite_url"] == "https://cards.example.com/a/b.png"  # cover → 立绘
    assert not entry["avatar_uri"]  # the source art is NEVER the avatar
