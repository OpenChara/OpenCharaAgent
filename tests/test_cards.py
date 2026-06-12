import json
from pathlib import Path

from lunamoth.content.cards import CharacterCard, detect_language

CARDS_DIR = Path(__file__).resolve().parents[1] / "cards"


def test_language_from_filename():
    assert detect_language("cards/LunaMoth.zh.json") == "zh"
    assert detect_language("cards/LunaMoth.en.json") == "en"
    assert detect_language("cards/SCP-079.zh.json") == "zh"


def test_language_from_content_when_no_hint():
    assert detect_language("card.json", "你好，我是一个清冷的数字灵魂") == "zh"
    assert detect_language("card.json", "Hello, I am a serene digital soul") == "en"


def test_bundled_cards_declare_defaults_and_language():
    moth = CharacterCard.load("cards/LunaMoth.zh.json")
    assert moth.language == "zh"
    d = moth.defaults()
    assert d["toolpack"] == "sandbox"
    assert d["memory_chars"] == 8000
    # The world is the embedded character_book, never a path pointer.
    assert "world" not in d
    # The context window is NOT card-declared — it's the model's real window.
    assert "context_tokens" not in d

    scp = CharacterCard.load("cards/SCP-079.en.json")
    assert scp.language == "en"
    assert "world" not in scp.defaults()
    assert scp.defaults()["memory_chars"] == 1500  # 079's tiny memory is characterful


def test_every_bundled_card_carries_its_world_inside():
    """The card is the ONE external file: each bundled card embeds its book."""
    cards = sorted(CARDS_DIR.glob("*.json"))
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
