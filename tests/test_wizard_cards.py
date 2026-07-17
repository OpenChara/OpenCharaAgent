"""front/wizard._discover_characters — the first-run character menu.

The menu once scanned ROOT/"cards", which exists only in a dev checkout — on the
wheel channel (ROOT points into site-packages) the menu came up EMPTY. It now
resolves through config.content_dir("cards"), like content/persona.py does, so a
wheel install sees the packaged chara/_bundled/cards.
"""
from __future__ import annotations

import json


def test_discover_characters_reads_content_dir(tmp_path, monkeypatch):
    from chara.front import wizard

    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "hoshi.json").write_text(json.dumps({"name": "Hoshi"}), encoding="utf-8")
    (cards / "quinn.zh.json").write_text(json.dumps({"name": "Quinn"}), encoding="utf-8")
    (cards / ".hidden.json").write_text("{}", encoding="utf-8")  # dotfiles skipped
    (cards / "notes.txt").write_text("x", encoding="utf-8")      # non-cards skipped
    monkeypatch.setattr(wizard, "content_dir",
                        lambda name: cards if name == "cards" else tmp_path / name)

    found = wizard._discover_characters()
    paths = [p for _, p in found]
    assert str(cards / "hoshi.json") in paths
    assert str(cards / "quinn.zh.json") in paths
    assert len(found) == 2  # the dotfile and the .txt were filtered out
    zh_label = next(lbl for lbl, p in found if p.endswith("quinn.zh.json"))
    assert "(zh)" in zh_label  # language inferred from the stem, as before


def test_discover_characters_empty_without_a_cards_dir(tmp_path, monkeypatch):
    from chara.front import wizard

    monkeypatch.setattr(wizard, "content_dir", lambda name: tmp_path / "missing" / name)
    assert wizard._discover_characters() == []
