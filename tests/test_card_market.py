"""card_market: search normalization + ST-card import mapping + compatibility.

HTTP and the deck/avatar writers are mocked, so these pin the pure logic: the
foreign-card → our-card mapping, and that import tolerates what a foreign card lacks
(no 理想/polaris, no theme color) without crashing or fabricating.
"""
from __future__ import annotations

import pytest

from lunamoth.server.hub import card_market as M
from lunamoth.server.hub._common import HubRpcError


# ---- search --------------------------------------------------------------------

def test_search_normalizes_hits(monkeypatch):
    captured = {}

    def fake_get(url):
        captured["url"] = url
        return {"hits": [
            {"path": "amy/witch", "name": "Witch", "tagline": "spooky", "author": "amy",
             "tags": ["fantasy", "magic"], "isNSFW": False, "hasLorebook": True,
             "characterFirstMessage": "Hello there, traveller."},
            {"path": "", "name": "broken"},  # no path → dropped
            "not-a-dict",                      # junk → dropped
        ], "totalHits": 99}

    monkeypatch.setattr(M, "_get_json", fake_get)
    out = M.search("witch", limit=10)
    assert out["totalHits"] == 99
    assert [c["path"] for c in out["candidates"]] == ["amy/witch"]
    c = out["candidates"][0]
    assert c["imageUrl"].endswith("amy/witch.png") and c["pageUrl"].endswith("/character/amy/witch")
    assert c["hasLorebook"] is True and c["excerpt"].startswith("Hello there")
    assert "exclude_tags=" in captured["url"]  # NSFW excluded by default


def test_search_nsfw_flag_drops_the_exclude(monkeypatch):
    captured = {}
    monkeypatch.setattr(M, "_get_json", lambda url: captured.update(url=url) or {"hits": []})
    M.search("witch", nsfw=True)
    assert "exclude_tags=" not in captured["url"]


def test_search_blank_query_errors():
    with pytest.raises(HubRpcError):
        M.search("   ")


# ---- import mapping (the compatibility core) -----------------------------------

# The REAL /api/character shape: persona rides flat `definition_*` fields at the TOP
# level (no nested `data` block), like character-tavern actually returns.
_DETAIL = {
    "card": {
        "path": "amy/witch", "name": "Witch", "inChatName": "Elspeth",
        "tagline": "a spooky forest witch", "author": "amy", "isNSFW": False,
        "definition_character_description": "An old witch. {{char}} watches {{user}} closely.",
        "definition_personality": "wry, patient",
        "definition_scenario": "A mossy cabin.",
        "definition_first_message": "Mind the cat, {{user}}.",
        "definition_example_messages": "<START>\n{{char}}: hm.",
        "definition_system_prompt": "stay in character",
        "definition_post_history_prompt": "never break character",
        "alternate_greetings": ["A second door creaks open."],
        "creator_notes": "for fun",
        "tags": ["fantasy", "witch"],
        "character_book": {"name": "wood", "entries": [{"keys": ["cabin"], "content": "moss everywhere"}]},
    }
}


def _import(monkeypatch, detail=_DETAIL, cover=b"\x89PNG\r\n\x1a\nrest"):
    saved = {}
    monkeypatch.setattr(M, "_get_json", lambda url: detail)
    monkeypatch.setattr(M, "_request", lambda url: cover)
    monkeypatch.setattr(M._cards, "save_card", lambda card: saved.update(card=card) or {"path": "/deck/witch.json"})
    calls = []
    monkeypatch.setattr(M._avatars, "asset_save", lambda *a, **k: calls.append(("asset_save", a)))
    monkeypatch.setattr(M._avatars, "avatar_upload", lambda *a, **k: calls.append(("avatar_upload", a)))
    res = M.import_card("amy/witch")
    return res, saved.get("card"), calls


def test_import_maps_fields_and_keeps_macros(monkeypatch):
    res, card, _ = _import(monkeypatch)
    assert res["path"] == "/deck/witch.json" and res["name"] == "Elspeth"
    d = card["data"]
    assert d["name"] == "Elspeth"  # inChatName wins
    assert d["description"].count("{{char}}") == 1 and d["first_mes"].count("{{user}}") == 1  # macros intact
    assert d["post_history_instructions"] == "never break character"
    assert d["character_book"]["entries"][0]["keys"] == ["cabin"]  # lorebook passes through
    assert d["alternate_greetings"] == ["A second door creaks open."]


def test_import_omits_polaris_and_always_sets_theme(monkeypatch):
    _, card, _ = _import(monkeypatch)
    ext = card["data"]["extensions"]["lunamoth"]
    assert "polaris" not in ext  # 理想 is the user's — never imported, and absent is safe
    assert ext["theme"]["primary"].startswith("#") and len(ext["theme"]["primary"]) == 7  # always present
    assert ext["source"] == "character_tavern" and ext["source_path"] == "amy/witch"


def test_import_attaches_cover_as_keyvisual_and_avatar(monkeypatch):
    res, _, calls = _import(monkeypatch)
    kinds = [c[0] for c in calls]
    assert "asset_save" in kinds and "avatar_upload" in kinds and res["cover"] is True
    # keyvisual kind passed through
    assert any(c[0] == "asset_save" and c[1][1] == "keyvisual" for c in calls)


def test_import_survives_a_missing_cover(monkeypatch):
    # a non-PNG / failed image must NOT abort the import — the card still lands
    res, card, calls = _import(monkeypatch, cover=b"<html>not an image</html>")
    assert res["path"] == "/deck/witch.json" and res["cover"] is False
    assert calls == []  # nothing attached, but no raise


def test_import_nsfw_gate(monkeypatch):
    nsfw_detail = {"card": {**_DETAIL["card"], "isNSFW": True}}
    monkeypatch.setattr(M, "_get_json", lambda url: nsfw_detail)
    with pytest.raises(HubRpcError):
        M.import_card("amy/witch")  # default nsfw=False → refused
    # explicit opt-in imports it
    monkeypatch.setattr(M._cards, "save_card", lambda card: {"path": "/deck/x.json"})
    monkeypatch.setattr(M, "_request", lambda url: b"")
    assert M.import_card("amy/witch", nsfw=True)["path"] == "/deck/x.json"


def test_import_blank_path_errors():
    with pytest.raises(HubRpcError):
        M.import_card("   ")


def test_seeded_theme_is_deterministic_and_distinct():
    a1 = M._seeded_theme("amy/witch")
    a2 = M._seeded_theme("amy/witch")
    b = M._seeded_theme("bob/knight")
    assert a1 == a2  # stable per card
    assert a1["primary"] != b["primary"]  # distinct across cards
    assert M._seeded_theme("")["primary"] == M._DEFAULT_THEME_PRIMARY  # empty → fallback blue
