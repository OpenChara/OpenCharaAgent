"""User/card-facing chara knobs: tempo and embodiment."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lunamoth.content.knobs import parse_tempo
from lunamoth.session.settings import Settings


def _write_card(path: Path, lunamoth: dict | None = None) -> Path:
    payload = {
        "data": {
            "name": "KnobCard",
            "description": "Persona marker.",
            "personality": "Quiet.",
            "scenario": "A test room.",
            "system_prompt": "System marker for {{char}}/{{user}}.",
            "first_mes": "",
            "extensions": {"lunamoth": dict(lunamoth or {})},
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))

    from lunamoth.core import agent as agent_mod
    from lunamoth.tools import skills as skills_mod

    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(skills_mod, "SANDBOX_ROOT", sandbox)

    from lunamoth.core.agent import LunaMothAgent

    def make(*, card: Path | None = None, toolpack: str = "sandbox", **kw):
        settings = Settings(
            provider="mock",
            character_path=str(card or ""),
            world_path="",
            toolpack=toolpack,
            **kw,
        )
        a = LunaMothAgent(settings)
        a.transcript.reset()
        return a

    return make


def _blob(blocks: list[str]) -> str:
    return "\n\n".join(blocks)


def test_tempo_presets_parse_and_bad_values_reject():
    assert parse_tempo("swift") == 2.0
    assert parse_tempo("steady") == 1.0
    assert parse_tempo("slow") == 0.5
    assert parse_tempo("glacial") == 0.25
    assert parse_tempo("2x") == 2.0
    assert parse_tempo(0) is None
    assert parse_tempo("garbage") is None


def test_card_tempo_precedence_and_tempo_command_persists(agent_factory, tmp_path):
    from lunamoth.core import commands

    card = _write_card(tmp_path / "tempo.json", {"toolpack": "sandbox", "tempo": "slow"})
    a = agent_factory(card=card)
    s = a.make_session()
    assert a.effective_tempo() == 0.5

    reply = commands.execute(a, s, "/tempo swift")
    assert reply.ok and reply.data == {"tempo": 2.0}
    assert a.settings.tempo == 2.0
    assert a.effective_tempo() == 2.0  # operator setting > card

    reply = commands.execute(a, s, "/tempo 0")
    assert not reply.ok and "usage: /tempo" in reply.text
    reply = commands.execute(a, s, "/tempo nonsense")
    assert not reply.ok and "usage: /tempo" in reply.text


def test_snapshot_tempo_drives_effective_interval(agent_factory, tmp_path):
    from lunamoth.protocol.api import CharaHandle
    from lunamoth.front.tui.app import LunaMothTUI

    card = _write_card(tmp_path / "tempo.json", {"toolpack": "sandbox", "tempo": 2.0})
    a = agent_factory(card=card)
    handle = CharaHandle(agent=a)
    handle.attach(present=False)
    snap = handle.snapshot(fresh=True)
    assert snap.tempo == 2.0

    app = object.__new__(LunaMothTUI)
    app.handle = handle
    app.base_patience = 8.0
    assert app._cycle_pause() == 4.0
    assert snap.quiet == a.settings.quiet  # tempo does not scale engagement quiet
    until_before = time.time() + 9 * 60
    a.tools.call("rest", minutes=10)
    until = a.state.load()["rest_until"]
    assert until > until_before  # tempo does not shorten the rest tool's explicit decision


def test_embodiment_actor_bridge_order_override_macros_and_tool_gate(agent_factory, tmp_path):
    card = _write_card(tmp_path / "actor.json", {
        "toolpack": "sandbox",
        "embodiment": "actor",
        "embodiment_bridge": "BRIDGE for {{char}} and {{user}}",
    })
    a = agent_factory(card=card)
    blocks = a._stable_prefix()
    blob = _blob(blocks)
    assert "BRIDGE for KnobCard and 操作者" in blob
    assert blob.index("BRIDGE for KnobCard") < blob.index("must be real")

    no_tools = agent_factory(card=card, toolpack="")
    no_tools.tools.set_enabled(None)
    assert "BRIDGE" not in _blob(no_tools._stable_prefix())


def test_default_literal_prefix_is_byte_identical_to_pre_change(agent_factory, tmp_path):
    from lunamoth.content import rules as rules_layer
    from lunamoth.content.worldinfo import apply_macros

    card = _write_card(tmp_path / "plain.json", {"toolpack": "sandbox"})
    a = agent_factory(card=card)
    a.skills = type("NoSkills", (), {"render_block": lambda self: ""})()
    a._skills_snapshot = ""
    expected_pre_change_prefix = [
        a.character.render_system(a.settings.user_name),
        apply_macros(rules_layer.rules(a.lang, ""), a.char_name(), a.settings.user_name),
        (
            "You have tools available via native function calling. Call them directly when "
            "you want to act; never paste code in prose or claim a result before the tool returns."
        ),
        a.toolpack.note.strip(),
    ]
    actual = a._stable_prefix()
    assert actual == expected_pre_change_prefix
    assert "backstage of this embodiment" not in _blob(actual)


def test_bundled_actor_bridge_uses_card_language_and_macros(agent_factory, tmp_path):
    card = _write_card(tmp_path / "actor.zh.json", {"toolpack": "sandbox", "embodiment": "actor"})
    a = agent_factory(card=card)
    blob = _blob(a._stable_prefix())
    assert "你在赋予KnobCard生命" in blob
    assert "这场化身的后台" in blob


def test_embodiment_command_persists_and_explains(agent_factory):
    from lunamoth.core import commands

    a = agent_factory()
    s = a.make_session()
    reply = commands.execute(a, s, "/embodiment actor")
    assert reply.ok
    assert reply.data == {"embodiment": "actor"}
    assert a.settings.embodiment_override == "actor"
    assert "operator > card > literal" in reply.text
    assert "Actor: the model embodies the character" in reply.text

    help_reply = commands.execute(a, s, "/embodiment")
    assert help_reply.ok and help_reply.verbose
    assert "Literal: the character IS a digital being" in help_reply.text
    assert "演员化身" in help_reply.text

    bad = commands.execute(a, s, "/embodiment puppet")
    assert not bad.ok and "usage: /embodiment literal|actor" in bad.text
