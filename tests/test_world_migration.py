"""Legacy `world_path` configs: load cleanly + one-time merge into the card.

The standalone world channel is retired; the card's embedded character_book is
the ONE world source. Old configs migrate exactly once and never lose a world.
"""
import json

import pytest

from lunamoth.session import settings as settings_mod


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(settings_mod, "CONFIG_DIR", cfg)
    monkeypatch.setattr(settings_mod, "CONFIG_PATH", cfg / "config.json")
    return cfg


def _world(path, entries):
    path.write_text(json.dumps({"name": "old-world", "entries": {str(i): e for i, e in enumerate(entries)}},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _card(path, book=None):
    data = {"name": "Mig", "description": "d", "extensions": {}}
    if book is not None:
        data["character_book"] = book
    path.write_text(json.dumps({"data": data}, ensure_ascii=False), encoding="utf-8")
    return path


def test_old_config_with_empty_world_path_loads_cleanly_and_strips_key(cfg_dir):
    settings_mod.CONFIG_PATH.write_text(json.dumps({"provider": "mock", "world_path": ""}), encoding="utf-8")
    s = settings_mod.load_settings()
    assert s.provider == "mock"
    assert not hasattr(s, "world_path")
    assert "world_path" not in json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))


def test_old_config_with_missing_world_file_loads_and_strips_key(cfg_dir, tmp_path):
    settings_mod.CONFIG_PATH.write_text(
        json.dumps({"provider": "mock", "world_path": str(tmp_path / "gone.json")}), encoding="utf-8")
    s = settings_mod.load_settings()
    assert s.provider == "mock"
    assert "world_path" not in json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))


def test_session_card_gets_world_merged_in_place_once(cfg_dir, tmp_path):
    card = _card(cfg_dir / "card.json")  # the session's own frozen copy
    world = _world(tmp_path / "w.json", [
        {"key": ["alpha"], "content": "ALPHA LORE", "constant": True, "order": 3},
        {"key": ["beta"], "content": "BETA LORE", "order": 7},
    ])
    settings_mod.CONFIG_PATH.write_text(json.dumps({
        "provider": "mock", "character_path": str(card), "world_path": str(world),
    }), encoding="utf-8")

    s = settings_mod.load_settings()
    assert s.character_path == str(card)  # in-place: no repoint

    merged = json.loads(card.read_text(encoding="utf-8"))
    book = merged["data"]["character_book"]
    assert book["name"] == "old-world"
    assert [(e["keys"], e["content"], e["constant"], e["insertion_order"]) for e in book["entries"]] == [
        (["alpha"], "ALPHA LORE", True, 3),
        (["beta"], "BETA LORE", False, 7),
    ]
    cfg = json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert "world_path" not in cfg

    # One-time: a second load changes nothing.
    settings_mod.load_settings()
    again = json.loads(card.read_text(encoding="utf-8"))
    assert len(again["data"]["character_book"]["entries"]) == 2


def test_shared_card_gets_a_merged_copy_and_repointed_config(cfg_dir, tmp_path):
    shared = _card(tmp_path / "shared.json", book={"name": "had", "entries": [
        {"keys": ["alpha"], "content": "ALPHA LORE"},
    ]})
    world = _world(tmp_path / "w.json", [
        {"key": ["alpha"], "content": "ALPHA LORE"},  # identical keys+content -> skipped
        {"key": ["gamma"], "content": "GAMMA LORE"},
    ])
    settings_mod.CONFIG_PATH.write_text(json.dumps({
        "provider": "mock", "character_path": str(shared), "world_path": str(world),
    }), encoding="utf-8")

    s = settings_mod.load_settings()
    cfg = json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert "world_path" not in cfg
    assert cfg["character_path"] != str(shared)
    assert s.character_path == cfg["character_path"]
    copy = json.loads(open(cfg["character_path"], encoding="utf-8").read())
    contents = [e["content"] for e in copy["data"]["character_book"]["entries"]]
    assert contents == ["ALPHA LORE", "GAMMA LORE"]  # deduped append

    # The shared original is untouched.
    original = json.loads(shared.read_text(encoding="utf-8"))
    assert len(original["data"]["character_book"]["entries"]) == 1
