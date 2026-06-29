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
            {"path": "amy/witch", "name": "Witch", "inChatName": "Elspeth", "tagline": "spooky",
             "author": "amy", "tags": ["fantasy", "magic"], "isNSFW": False, "hasLorebook": True,
             "isOC": True, "downloads": 5473, "likes": 128,
             "characterFirstMessage": "Hello there, traveller."},
            {"path": "", "name": "broken"},  # no path → dropped
            "not-a-dict",                      # junk → dropped
        ], "totalHits": 99, "totalPages": 5}

    monkeypatch.setattr(M, "_get_json", fake_get)
    out = M.search("witch", limit=10)
    assert out["totalHits"] == 99 and out["totalPages"] == 5 and out["page"] == 1
    assert [c["path"] for c in out["candidates"]] == ["amy/witch"]
    c = out["candidates"][0]
    assert c["name"] == "Elspeth"  # inChatName wins
    # cover is the STORAGE CDN (resized thumb), never the hotlink-403 cards.* host
    assert "ct-cards.storage.character-tavern.com" in c["imageUrl"] and "amy/witch.png" in c["imageUrl"]
    assert "width=" in c["imageUrl"]
    assert c["hasLorebook"] is True and c["oc"] is True
    assert c["downloads"] == 5473 and c["likes"] == 128  # ranking signals surfaced
    assert "exclude_tags=" in captured["url"] and "sort=most_popular" in captured["url"]


def test_search_browses_with_empty_query_and_sort(monkeypatch):
    # an empty query is the DEFAULT browse (trending / popular), not an error — the
    # market opens to content. The query param is omitted; sort + page drive it.
    captured = {}
    monkeypatch.setattr(M, "_get_json", lambda url: captured.update(url=url) or {"hits": [], "totalPages": 7})
    out = M.search("", sort="trending", page=3)
    assert "query=" not in captured["url"] and "sort=trending" in captured["url"]
    assert "page=3" in captured["url"] and out["sort"] == "trending" and out["page"] == 3


def test_search_sort_is_validated(monkeypatch):
    captured = {}
    monkeypatch.setattr(M, "_get_json", lambda url: captured.update(url=url) or {"hits": []})
    M.search("x", sort="bogus")  # unknown sort → most_popular
    assert "sort=most_popular" in captured["url"]


def test_search_filters_tags_oc_lorebook(monkeypatch):
    captured = {}
    monkeypatch.setattr(M, "_get_json", lambda url: captured.update(url=url) or {"hits": []})
    M.search("", tags=["anime", "fantasy"], oc=True, lorebook=True)
    # multiple tags AND via one comma-joined param (urlencoded comma = %2C)
    assert "tags=anime%2Cfantasy" in captured["url"]
    assert "isOC=true" in captured["url"] and "hasLorebook=true" in captured["url"]


def test_search_nsfw_flag_drops_the_exclude(monkeypatch):
    captured = {}
    monkeypatch.setattr(M, "_get_json", lambda url: captured.update(url=url) or {"hits": []})
    M.search("witch", nsfw=True)
    assert "exclude_tags=" not in captured["url"]


def test_paths_with_spaces_are_url_encoded(monkeypatch):
    # card paths can carry spaces/unicode (e.g. "bmboster/Yae Miko") — the <img> and
    # detail URLs must be encoded, but the stored source_path stays raw (the identity).
    monkeypatch.setattr(M, "_get_json", lambda url: {"hits": [
        {"path": "bmboster/Yae Miko", "name": "Yae"}]})
    hit = M.search("yae")["candidates"][0]
    assert hit["imageUrl"].startswith("https://ct-cards.storage.character-tavern.com/bmboster/Yae%20Miko.png")
    assert hit["pageUrl"].endswith("/character/bmboster/Yae%20Miko")
    assert hit["path"] == "bmboster/Yae Miko"  # raw identity preserved

    seen = {}
    monkeypatch.setattr(M, "_get_json", lambda url: seen.update(url=url) or {
        "card": {"path": "bmboster/Yae Miko", "name": "Yae", "definition_character_description": "x"}})
    monkeypatch.setattr(M._cards, "save_card", lambda card: {"path": "/deck/yae.json"})
    M.import_card("bmboster/Yae Miko")
    assert seen["url"].endswith("/api/character/bmboster/Yae%20Miko")  # detail fetch encoded


# ---- detail (preview before importing) -----------------------------------------

def test_detail_normalizes_persona_and_stats(monkeypatch):
    monkeypatch.setattr(M, "_get_json", lambda url: {"card": {
        "path": "amy/witch", "name": "Witch", "inChatName": "Elspeth", "author": "amy",
        "tagline": "spooky", "definition_character_description": "An old witch. {{char}} watches {{user}}.",
        "definition_personality": "wry", "definition_first_message": "Mind the cat, {{user}}.",
        "tags": ["fantasy"], "isNSFW": False, "lorebookId": "lb1", "isOC": True,
        "analytics_downloads": 5473, "analytics_views": 9001}})
    d = M.detail("amy/witch")
    assert d["name"] == "Elspeth" and d["author"] == "amy"
    assert d["description"].count("{{char}}") == 1  # macros intact for preview
    assert d["hasLorebook"] is True and d["oc"] is True
    assert d["downloads"] == 5473 and d["views"] == 9001
    assert "ct-cards.storage.character-tavern.com" in d["imageUrl"]


def test_detail_blank_path_errors():
    with pytest.raises(HubRpcError):
        M.detail("   ")


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


def _import(monkeypatch, detail=_DETAIL):
    saved = {}
    monkeypatch.setattr(M, "_get_json", lambda url: detail)
    monkeypatch.setattr(M._cards, "save_card", lambda card: saved.update(card=card) or {"path": "/deck/witch.json"})
    res = M.import_card("amy/witch")
    return res, saved.get("card")


def test_import_maps_fields_and_keeps_macros(monkeypatch):
    res, card = _import(monkeypatch)
    assert res["path"] == "/deck/witch.json" and res["name"] == "Elspeth"
    # the full-res cover URL (storage CDN) is surfaced so the client can bring the image over.
    assert res["image_url"] == "https://ct-cards.storage.character-tavern.com/amy/witch.png"
    d = card["data"]
    assert d["name"] == "Elspeth"  # inChatName wins
    assert d["description"].count("{{char}}") == 1 and d["first_mes"].count("{{user}}") == 1  # macros intact
    assert d["post_history_instructions"] == "never break character"
    assert d["character_book"]["entries"][0]["keys"] == ["cabin"]  # lorebook passes through
    assert d["alternate_greetings"] == ["A second door creaks open."]


def test_import_omits_polaris_sets_theme_and_keeps_cover_url(monkeypatch):
    _, card = _import(monkeypatch)
    ext = card["data"]["extensions"]["lunamoth"]
    assert "polaris" not in ext  # 理想 is the user's — never imported, and absent is safe
    assert ext["theme"]["primary"].startswith("#") and len(ext["theme"]["primary"]) == 7  # always present
    assert ext["source"] == "character_tavern" and ext["source_path"] == "amy/witch"
    # the full-res cover URL is preserved on the card (sprite/立绘 display fallback)
    assert ext["source_image"] == "https://ct-cards.storage.character-tavern.com/amy/witch.png"


def test_import_nsfw_gate(monkeypatch):
    nsfw_detail = {"card": {**_DETAIL["card"], "isNSFW": True}}
    monkeypatch.setattr(M, "_get_json", lambda url: nsfw_detail)
    with pytest.raises(HubRpcError):
        M.import_card("amy/witch")  # default nsfw=False → refused
    # explicit opt-in imports it
    monkeypatch.setattr(M._cards, "save_card", lambda card: {"path": "/deck/x.json"})
    assert M.import_card("amy/witch", nsfw=True)["path"] == "/deck/x.json"


def test_import_blank_path_errors():
    with pytest.raises(HubRpcError):
        M.import_card("   ")


def test_traversal_path_is_rejected(monkeypatch):
    # a card path never legitimately contains `.`/`..`; reject it before any fetch
    # (host is hard-coded so this is hygiene, but the upstream must never be hit with it).
    called = {"n": 0}
    monkeypatch.setattr(M, "_get_json", lambda url: called.update(n=called["n"] + 1) or {})
    with pytest.raises(HubRpcError):
        M.import_card("../../etc/passwd")
    assert called["n"] == 0  # rejected before reaching the network


def test_import_nsfw_gate_by_tag(monkeypatch):
    # the detail payload doesn't always echo `isNSFW`; a tag match is the backstop.
    tagged = {"card": {**_DETAIL["card"], "isNSFW": None, "tags": ["nsfw", "fantasy"]}}
    monkeypatch.setattr(M, "_get_json", lambda url: tagged)
    with pytest.raises(HubRpcError):
        M.import_card("amy/witch")  # nsfw tag → refused even without the isNSFW flag
    monkeypatch.setattr(M._cards, "save_card", lambda card: {"path": "/deck/x.json"})
    assert M.import_card("amy/witch", nsfw=True)["path"] == "/deck/x.json"


def test_import_keeps_dict_form_lorebook(monkeypatch):
    # some ST V2 cards encode character_book.entries as a dict keyed by id; the mapper
    # must keep it (normalized to a list) just like the native loader, not drop it.
    detail = {"card": {**_DETAIL["card"],
                       "character_book": {"name": "w", "entries": {"0": {"keys": ["k"], "content": "c"}}}}}
    _, card = _import(monkeypatch, detail)
    assert card["data"]["character_book"]["entries"][0]["keys"] == ["k"]


def test_seeded_theme_is_deterministic_and_distinct():
    a1 = M._seeded_theme("amy/witch")
    a2 = M._seeded_theme("amy/witch")
    b = M._seeded_theme("bob/knight")
    assert a1 == a2  # stable per card
    assert a1["primary"] != b["primary"]  # distinct across cards
    assert M._seeded_theme("")["primary"] == M._DEFAULT_THEME_PRIMARY  # empty → fallback blue
