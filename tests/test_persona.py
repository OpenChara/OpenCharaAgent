"""Default-card selection: the "default" tag convention (no names in src/)."""
import json

from lunamoth.content import persona
from lunamoth.content.cards import CharacterCard


def test_bundled_default_is_the_tagged_card():
    # The bundled default is whichever card carries the "default" tag — today
    # that is Quinn. The roster is English-only (the zh cards were archived as
    # easter eggs), so a zh request falls back to the same tagged English card.
    for lang in ("zh", "en"):
        path = persona.default_character_path(lang)
        assert path is not None
        card = CharacterCard.load(path)
        assert persona.DEFAULT_TAG in [t.lower() for t in card.tags]
        assert card.name == "Quinn"


def _write_card(path, name, lang, tags):
    payload = {"data": {"name": name, "description": f"{name} description.", "tags": tags}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_default_tag_wins_over_sorted_order_and_falls_back_without_it(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    cards.mkdir()
    _write_card(cards / "Aardvark.en.json", "Aardvark", "en", [])
    _write_card(cards / "Zebra.en.json", "Zebra", "en", ["default"])
    monkeypatch.setattr(persona, "ROOT", tmp_path)

    assert persona.default_character_path("en").name == "Zebra.en.json"

    # Remove the tag -> legacy sorted-order-first behavior.
    _write_card(cards / "Zebra.en.json", "Zebra", "en", [])
    assert persona.default_character_path("en").name == "Aardvark.en.json"


def test_default_tag_reading_tolerates_broken_tags(tmp_path, monkeypatch):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "Broken.en.json").write_text("{not json", encoding="utf-8")
    payload = {"data": {"name": "Odd", "tags": "default"}}  # non-list tags
    (cards / "Odd.en.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(persona, "ROOT", tmp_path)

    assert persona.default_character_path("en").name == "Broken.en.json"  # sorted fallback
