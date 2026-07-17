"""User/card-facing chara knobs: patience and embodiment."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chara.content.knobs import normalize_website, parse_patience
from chara.session.settings import Settings

_WEB_MARK = "personal website in your space"
_WEB_CLOSER_MARK = "keep it current"


def _write_card(path: Path, chara: dict | None = None) -> Path:
    payload = {
        "data": {
            "name": "KnobCard",
            "description": "Persona marker.",
            "personality": "Quiet.",
            "scenario": "A test room.",
            "system_prompt": "System marker for {{char}}/{{user}}.",
            "first_mes": "",
            "extensions": {"chara": dict(chara or {})},
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))

    from chara.core import agent as agent_mod
    from chara.tools import skills as skills_mod

    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(skills_mod, "SANDBOX_ROOT", sandbox)

    # The stable prefix probes the host for ffmpeg (agent._stable_prefix →
    # shutil.which); pin it absent so prefix-sequence asserts don't depend on
    # what happens to be installed on the machine running the tests.
    _real_which = agent_mod.shutil.which
    monkeypatch.setattr(
        agent_mod.shutil,
        "which",
        lambda cmd, *a, **k: None if cmd == "ffmpeg" else _real_which(cmd, *a, **k),
    )

    from chara.core.agent import CharaAgent

    def make(*, card: Path | None = None, toolpack: str = "sandbox", **kw):
        settings = Settings(
            provider="mock",
            character_path=str(card or ""),
            toolpack=toolpack,
            **kw,
        )
        a = CharaAgent(settings)
        a.transcript.reset()
        return a

    return make


def _blob(blocks: list[str]) -> str:
    return "\n\n".join(blocks)


def test_tempo_knob_is_retired_but_old_cards_still_load(agent_factory, tmp_path):
    """tempo was removed entirely (owner decision 2026-06-13): a card that still
    declares `extensions.chara.tempo` loads fine — the key is simply ignored."""
    from chara.core import commands
    from chara.protocol.api import CharaHandle

    card = _write_card(tmp_path / "old-tempo.json", {"toolpack": "sandbox", "tempo": "slow", "patience": 42})
    a = agent_factory(card=card)
    s = a.make_session()
    assert a.char_name() == "KnobCard"           # the card loaded cleanly
    assert a.effective_patience() == 42.0        # other knobs still respected
    assert not hasattr(a, "effective_tempo")

    handle = CharaHandle(agent=a)
    handle.attach(present=False)
    snap = handle.snapshot(fresh=True)
    assert not hasattr(snap, "tempo")            # snapshot no longer carries it

    reply = commands.execute(a, s, "/tempo swift")
    assert not reply.ok and "unknown command" in reply.text
    assert all(c.name != "tempo" for c in commands.infos())


def test_patience_parses_positive_numerics_only():
    assert parse_patience("600") == 600.0
    assert parse_patience("2.5") == 2.5
    assert parse_patience(90) == 90.0
    assert parse_patience(0) is None
    assert parse_patience(-1) is None
    assert parse_patience("garbage") is None


def test_patience_is_explicit_single_sources_the_default_rule():
    from chara.content.knobs import DEFAULT_PATIENCE, patience_is_explicit

    assert DEFAULT_PATIENCE == 3600.0
    # the bare default is NOT explicit (so a card default can still win)…
    assert patience_is_explicit(DEFAULT_PATIENCE) is False
    # …but any other positive value is.
    assert patience_is_explicit(42.0) is True
    assert patience_is_explicit(900.0) is True
    assert patience_is_explicit(0) is False


def test_card_patience_precedence_and_command_persists(agent_factory, tmp_path):
    from chara.core import commands

    card = _write_card(tmp_path / "patience.json", {"toolpack": "sandbox", "patience": "42"})
    a = agent_factory(card=card)
    s = a.make_session()
    assert a.effective_patience() == 42.0

    shown = commands.execute(a, s, "/patience")
    assert shown.ok and shown.data == {"patience": 42.0}

    reply = commands.execute(a, s, "/patience 15.5")
    assert reply.ok and reply.data == {"patience": 15.5}
    assert a.settings.patience == 15.5
    assert a.effective_patience() == 15.5  # operator setting > card

    reply = commands.execute(a, s, "/patience 600")
    assert reply.ok and reply.data == {"patience": 600.0}
    assert a.effective_patience() == 600.0  # explicit operator 600 still beats card

    reply = commands.execute(a, s, "/patience 0")
    assert not reply.ok and "usage: /patience" in reply.text
    reply = commands.execute(a, s, "/patience nonsense")
    assert not reply.ok and "usage: /patience" in reply.text


def test_snapshot_reports_effective_patience(agent_factory, tmp_path):
    from chara.protocol.api import CharaHandle

    card = _write_card(tmp_path / "patience-snap.json", {"toolpack": "sandbox", "patience": 123.0})
    a = agent_factory(card=card)
    handle = CharaHandle(agent=a)
    handle.attach(present=False)
    snap = handle.snapshot(fresh=True)
    assert snap.patience == 123.0


def test_embodiment_actor_bridge_order_override_macros_and_tool_gate(agent_factory, tmp_path):
    card = _write_card(tmp_path / "actor.json", {
        "toolpack": "sandbox",
        "force_roleplay": True,
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


def test_default_literal_prefix_sequence(agent_factory, tmp_path):
    """The literal-stance stable prefix is exactly: card identity, then the three
    neutral English blocks (rules, capabilities, tool-use), then the toolpack note.
    No actor bridge in literal stance; no per-language branching (engine is English)."""
    from chara.content import rules as rules_layer
    from chara.content.worldinfo import apply_macros

    card = _write_card(tmp_path / "plain.json", {"toolpack": "sandbox"})
    a = agent_factory(card=card)
    a.skills = type("NoSkills", (), {"render_block": lambda self: ""})()
    a._skills_snapshot = ""
    expected_prefix = [
        a.character.render_system(a.settings.user_name),
        apply_macros(rules_layer.rules(""), a.char_name(), a.settings.user_name),
        apply_macros(rules_layer.capabilities(""), a.char_name(), a.settings.user_name),
        apply_macros(rules_layer.tool_use(""), a.char_name(), a.settings.user_name),
        a.toolpack.note.strip(),
    ]
    actual = a._stable_prefix()
    assert actual == expected_prefix
    assert "backstage of this embodiment" not in _blob(actual)


def test_bundled_actor_bridge_renders_english_with_macros(agent_factory, tmp_path):
    """The actor bridge is English (engine prompt layer) with {{char}} substituted."""
    card = _write_card(tmp_path / "actor.json", {"toolpack": "sandbox", "force_roleplay": True})
    a = agent_factory(card=card)
    blob = _blob(a._stable_prefix())
    assert "giving KnobCard life" in blob
    assert "stage machinery the audience never sees" in blob


def test_embodiment_is_wake_time_only_no_hot_swap_command(agent_factory, tmp_path):
    """The /embodiment hot swap is gone (owner decision 2026-06-13): identity-layer
    switches rebuild the stable prefix and destroy the prompt cache. The choice
    arrives at wake (embodiment_override in the session config) and stays."""
    from chara.core import commands

    a = agent_factory()
    s = a.make_session()
    reply = commands.execute(a, s, "/embodiment actor")
    assert not reply.ok and "unknown command" in reply.text
    assert all(c.name != "embodiment" for c in commands.infos())

    # The resolution chain itself is untouched: override > card > literal.
    card = _write_card(tmp_path / "card-actor.json", {"toolpack": "sandbox", "force_roleplay": True})
    assert agent_factory(card=card).effective_embodiment() == "actor"
    assert agent_factory(card=card, embodiment_override="literal").effective_embodiment() == "literal"
    assert agent_factory().effective_embodiment() == "literal"


def test_force_roleplay_card_field_and_legacy_embodiment_fallback(agent_factory, tmp_path):
    """The card FIELD is now a boolean `force_roleplay` (True ≡ actor). A legacy
    frozen card still carrying the old `embodiment: "actor"` string must keep
    resolving to "actor" via the back-compat fallback in effective_embodiment()."""
    new = _write_card(tmp_path / "fr-true.json", {"toolpack": "sandbox", "force_roleplay": True})
    assert agent_factory(card=new).effective_embodiment() == "actor"

    new_false = _write_card(tmp_path / "fr-false.json", {"toolpack": "sandbox", "force_roleplay": False})
    assert agent_factory(card=new_false).effective_embodiment() == "literal"

    legacy = _write_card(tmp_path / "legacy-actor.json", {"toolpack": "sandbox", "embodiment": "actor"})
    assert agent_factory(card=legacy).effective_embodiment() == "actor"

    legacy_lit = _write_card(tmp_path / "legacy-literal.json", {"toolpack": "sandbox", "embodiment": "literal"})
    assert agent_factory(card=legacy_lit).effective_embodiment() == "literal"


# ── personal_website module ────────────────────────────────────────────────

def test_normalize_website_values():
    assert normalize_website(True) == "on"
    assert normalize_website(False) == "off"
    assert normalize_website("on") == "on"
    assert normalize_website("OFF") == "off"
    assert normalize_website("yes") == "on"
    assert normalize_website("0") == "off"
    assert normalize_website("") == ""
    assert normalize_website("maybe") == ""
    assert normalize_website(None) == ""


def test_website_module_in_prefix_and_closer_when_on(agent_factory, tmp_path):
    card = _write_card(tmp_path / "web.json", {"toolpack": "sandbox", "website": True})
    a = agent_factory(card=card)
    assert a.website_active() is True
    assert _WEB_MARK in _blob(a._stable_prefix())
    closer = a._post_history_slot()
    assert _WEB_CLOSER_MARK in closer
    # The base closer still rides alongside the module fragment.
    assert "stay fully in character" in closer


def test_website_absent_when_off_by_default(agent_factory, tmp_path):
    card = _write_card(tmp_path / "plain.json", {"toolpack": "sandbox"})
    a = agent_factory(card=card)
    assert a.website_active() is False
    assert _WEB_MARK not in _blob(a._stable_prefix())
    assert _WEB_CLOSER_MARK not in a._post_history_slot()


def test_website_resolution_precedence(agent_factory, tmp_path):
    on_card = _write_card(tmp_path / "on.json", {"toolpack": "sandbox", "website": True})
    # override beats card: card on, override off → off.
    assert agent_factory(card=on_card, website_override="off").website_active() is False
    # override on with no card declaration → on.
    plain = _write_card(tmp_path / "p.json", {"toolpack": "sandbox"})
    assert agent_factory(card=plain, website_override="on").website_active() is True
    # card on, no override → on.
    assert agent_factory(card=on_card).website_active() is True


def test_website_gated_on_tools(agent_factory, tmp_path):
    card = _write_card(tmp_path / "web.json", {"toolpack": "sandbox", "website": True})
    no_tools = agent_factory(card=card, toolpack="")
    no_tools.tools.set_enabled(None)
    assert _WEB_MARK not in _blob(no_tools._stable_prefix())


def test_hub_daemon_launch_does_not_hardcode_tiny_patience(monkeypatch, tmp_path):
    from chara.server import hub as H
    from chara.session import sessions as S

    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home-daemon"))
    meta = S.create_session("no-tiny")
    meta.config_path.write_text(json.dumps({"provider": "mock", "character_path": ""}), encoding="utf-8")
    calls = []

    class Proc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(H.subprocess, "Popen", fake_popen)
    assert H.start_daemon(meta) is True
    assert calls
    assert "--patience" not in calls[0]
    assert "2.0" not in calls[0]


def test_model_command_persists_to_chara_session(agent_factory, tmp_path):
    """/model swaps the model live AND persists it to THIS chara's session config
    so the choice survives a child restart (the LLM client is rebuilt; Reply.data
    carries {model, context_max}). Only the model id changes — the provider/key
    stay put, keeping the route steady within a session."""
    from chara.core import commands

    a = agent_factory(card=_write_card(tmp_path / "m.json"), model="mock/original")
    s = a.make_session()

    shown = commands.execute(a, s, "/model")
    assert shown.ok and shown.data["model"] == "mock/original" and "context_max" in shown.data

    reply = commands.execute(a, s, "/model mock/other")
    assert reply.ok and reply.data["model"] == "mock/other"
    assert a.settings.model == "mock/other"
    assert a.llm.cfg.model == "mock/other" if hasattr(a.llm, "cfg") else True

    # persisted: a fresh load of the chara's session config reflects the swap
    from chara.session.settings import load_settings
    assert load_settings().model == "mock/other"


def test_card_user_name_and_persona_reach_the_prompt(agent_factory, tmp_path):
    """The card may name the operator and describe them (webui-needs #9,
    ST persona convention): user_name fills {{user}}, user_persona becomes an
    'About <user>' block in the cached stable prefix."""
    card = _write_card(tmp_path / "withuser.json", {
        "toolpack": "sandbox",
        "user_name": "Mira",
        "user_persona": "{{user}} is a marine biologist who works odd hours.",
    })
    a = agent_factory(card=card)
    # user_name overrides the default and flows into {{user}} substitution
    assert a.settings.user_name == "Mira"
    prefix = "\n\n".join(a._stable_prefix())
    assert "About Mira:" in prefix
    assert "marine biologist" in prefix
    # the chara's own identity still leads; the user block is separate
    assert prefix.index("KnobCard") < prefix.index("About Mira:")


def test_operator_user_name_wins_over_the_card(agent_factory, tmp_path):
    card = _write_card(tmp_path / "withuser2.json", {"toolpack": "sandbox", "user_name": "Mira"})
    a = agent_factory(card=card, user_name="Captain")  # operator set it explicitly
    assert a.settings.user_name == "Captain"


def test_no_user_persona_means_no_block(agent_factory, tmp_path):
    card = _write_card(tmp_path / "nouser.json", {"toolpack": "sandbox"})
    a = agent_factory(card=card)
    assert "About " not in "\n\n".join(a._stable_prefix())
