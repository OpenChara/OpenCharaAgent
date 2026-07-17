"""config.content_dir — the dev/wheel content-dir resolver (2026-06-17 deploy P0).

A wheel install has no repo root, so cards/ + toolpacks/ must resolve to the
packaged _bundled/ copy. In a dev checkout they resolve to the repo root. If this
breaks, a wheel deploy silently ships zero toolpacks and every chara loses its
tools — so it's worth a guard.
"""
from pathlib import Path

import chara.config as config
from chara.config import ROOT, content_dir


def test_content_dir_prefers_repo_root_when_present():
    # In this dev checkout the repo-root dirs exist and must win.
    assert (ROOT / "toolpacks").exists(), "dev checkout should have repo-root toolpacks/"
    assert content_dir("toolpacks") == ROOT / "toolpacks"
    assert content_dir("cards") == ROOT / "cards"


def test_content_dir_falls_back_to_packaged_bundled(monkeypatch, tmp_path):
    # Simulate a wheel install: ROOT points somewhere with no toolpacks/cards.
    monkeypatch.setattr(config, "ROOT", tmp_path)
    got = content_dir("toolpacks")
    expected = Path(config.__file__).resolve().parent / "_bundled" / "toolpacks"
    assert got == expected, f"wheel fallback should be {expected}, got {got}"


def test_sandbox_toolpack_loads_in_dev():
    """The default 'sandbox' pack must resolve to a non-empty tool list (the thing
    a wheel deploy was silently missing)."""
    from chara.tools.toolpacks import load_toolpack

    pack = load_toolpack("sandbox")
    assert pack is not None and len(pack.tools) > 0
