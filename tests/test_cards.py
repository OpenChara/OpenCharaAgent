import json
from pathlib import Path

from chara.content.cards import CharacterCard, detect_language

CARDS_DIR = Path(__file__).resolve().parents[1] / "cards"


def test_language_from_filename():
    assert detect_language("archive/cards-zh/LunaMoth.card.zh.json") == "zh"
    assert detect_language("cards/LunaMoth/card.json") == "en"
    assert detect_language("archive/cards-zh/Quinn.card.zh.json") == "zh"


def test_language_from_content_when_no_hint():
    assert detect_language("card.json", "你好，我是一个清冷的数字灵魂") == "zh"
    assert detect_language("card.json", "Hello, I am a serene digital soul") == "en"


def test_bundled_cards_declare_defaults_and_language():
    moth = CharacterCard.load("archive/cards-zh/LunaMoth.card.zh.json")
    assert moth.language == "zh"
    d = moth.defaults()
    assert d["toolpack"] == "sandbox"
    assert d["memory_chars"] == 8000
    # The world is the embedded character_book, never a path pointer.
    assert "world" not in d
    # The context window is NOT card-declared — it's the model's real window.
    assert "context_tokens" not in d

    quinn = CharacterCard.load("cards/Quinn/card.json")
    assert quinn.language == "en"
    assert "world" not in quinn.defaults()
    # memory_chars / toolpack are no longer card fields — they were removed.
    assert "memory_chars" not in quinn.defaults()
    assert "toolpack" not in quinn.defaults()


def test_every_bundled_card_carries_its_world_inside():
    """The card is the ONE external file: each bundled card embeds its book."""
    cards = sorted(CARDS_DIR.glob("*.json")) + sorted(CARDS_DIR.glob("*/card*.json"))
    assert cards, "no bundled cards found"
    for path in cards:
        card = CharacterCard.load(path)
        assert card.character_book is not None, f"{path.name} has no embedded character_book"
        assert card.character_book.entries, f"{path.name} embeds an empty book"
        # Every bundled book has at least one constant (always-on) entry.
        assert card.character_book.constant_blocks(card.name, "User"), path.name


def test_plain_card_without_bundle_gets_empty_defaults(tmp_path):
    p = tmp_path / "plain.json"
    p.write_text(json.dumps({"name": "Plain", "description": "hi", "first_mes": "hello"}))
    card = CharacterCard.load(str(p))
    assert card.defaults() == {}  # -> agent falls back to safe global defaults


# ---- presentation: dual theme + avatar sidecar (not the soul) ----------------

def _card(ext):
    return CharacterCard.from_card_dict({"data": {"name": "X", "extensions": {"chara": ext}}})


def test_dual_theme_parsed():
    card = _card({"theme": {"primary": "#000000", "secondary": "#ff0000"}})
    assert card.theme_colors() == {"primary": "#000000", "secondary": "#FF0000"}


def test_dual_theme_back_compat_from_single_theme_color():
    # A card carrying only the legacy single color reads as primary, blank secondary.
    card = _card({"theme_color": "#5b9fd4"})
    assert card.theme_colors() == {"primary": "#5B9FD4", "secondary": ""}


def test_dual_theme_new_field_wins_over_legacy():
    card = _card({"theme": {"primary": "#112233", "secondary": "#445566"}, "theme_color": "#999999"})
    assert card.theme_colors()["primary"] == "#112233"


def test_dual_theme_malformed_is_blank_never_raises():
    card = _card({"theme": {"primary": "nope", "secondary": 5}})
    assert card.theme_colors() == {"primary": "", "secondary": ""}
    assert _card({}).theme_colors() == {"primary": "", "secondary": ""}


def test_avatar_file_reference_and_traversal_guard(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"data": {"name": "X",
        "extensions": {"chara": {"avatar_file": "c.avatar.png"}}}}))
    (tmp_path / "c.avatar.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = CharacterCard.load(str(p))
    assert card.avatar_file() == "c.avatar.png"
    assert card.avatar_path() == tmp_path / "c.avatar.png"
    # A traversal-bearing reference is refused (the sidecar must sit beside the card).
    bad = _card({"avatar_file": "../secret.png"})
    assert bad.avatar_file() == ""


def test_avatar_path_none_when_sidecar_missing(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"data": {"name": "X",
        "extensions": {"chara": {"avatar_file": "c.avatar.png"}}}}))
    assert CharacterCard.load(str(p)).avatar_path() is None  # referenced but not on disk


def test_asset_resolution_is_confined_and_rejects_traversal(tmp_path):
    """sprite/background/keyvisual/sticker sidecars resolve inside the card folder;
    traversal / absolute paths are refused (the /asset route depends on this)."""
    folder = tmp_path / "Zed"
    (folder / "stickers").mkdir(parents=True)
    (folder / "sprite.png").write_bytes(b"img")
    (folder / "stickers" / "00.png").write_bytes(b"s")
    (tmp_path / "secret.png").write_bytes(b"nope")
    card = {"name": "Zed", "data": {"name": "Zed", "first_mes": "hi", "extensions": {"chara": {
        "assets": {"sprite": "sprite.png",
                   "stickers": ["stickers/00.png", "../secret.png", "/etc/passwd.png"]}}}}}
    p = folder / "card.json"
    p.write_text(json.dumps(card), encoding="utf-8")
    c = CharacterCard.load(str(p))
    assert c.asset_path("sprite") == (folder / "sprite.png").resolve()
    assert c.asset_path("background") is None          # not declared
    assert c.sticker_paths() == [(folder / "stickers" / "00.png").resolve()]  # traversal+abs dropped
    assert c.has_art()
