"""The engine must be character-agnostic: no SCP / containment / trust framing
leaks into a character's system prompt. (Regression for the 'noble moth says it
is in a containment cell' bug.)"""
import pytest

from chara.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.core.agent import CharaAgent

    def make(**kw):
        return CharaAgent(Settings(character_path="", toolpack="", **kw))

    return make


_FORBIDDEN = ["containment", "收容", "hostility", "敌意", "trust level", "信任度", "scp-079", "scp 079"]


def test_default_character_carries_its_own_world(agent):
    a = agent()
    assert a.character is not None
    assert a.char_name() == a.character.name
    assert a.lang == a.character.language  # derived from the card, not a setting
    book = a.character.character_book  # the card's embedded book IS the world
    assert book is not None and book.entries
    assert "SCP" not in (book.name or "")


def test_no_scp_framing_in_system_prompt(agent):
    a = agent()
    blob = "\n".join(a._build_system_messages("hello")).lower()
    for bad in _FORBIDDEN:
        assert bad not in blob, f"engine leaked {bad!r} into a neutral character's prompt"


def test_card_defaults_drive_toolpack_and_memory(agent):
    a = agent()
    assert a.toolpack is not None and a.toolpack.name == "sandbox"
    # memory_chars is no longer a card field — it's the engine default now.
    assert a.memory.limits.memory_chars == 4000
    # Context window is the model's real window (default for mock/unknown), NOT the card.
    from chara.core.providers import DEFAULT_WINDOW
    assert a.context_limit() == DEFAULT_WINDOW


def test_env_state_is_neutral(agent):
    a = agent()
    state = a.state.load()
    for legacy in ("trust", "hostility", "containment_level", "memory_integrity"):
        assert legacy not in state
    # tool_access was retired: gating is registry ∩ pack (no per-session list).
    assert "tool_access" not in state
    eff = a.tools._effective()
    assert "terminal" in eff and "read_file" in eff
