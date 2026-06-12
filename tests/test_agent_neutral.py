"""The engine must be character-agnostic: no SCP / containment / trust framing
leaks into a character's system prompt. (Regression for the 'noble moth says it
is in a containment cell' bug.)"""
import pytest

from lunamoth.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        return LunaMothAgent(Settings(character_path="", toolpack="", **kw))

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
    assert a.memory.limits.memory_chars == 8000  # moth's card-declared memory size
    # Context window is the model's real window (default for mock/unknown), NOT the card.
    from lunamoth.core.providers import DEFAULT_WINDOW
    assert a.context_limit() == DEFAULT_WINDOW


def test_env_state_is_neutral(agent):
    a = agent()
    state = a.state.load()
    for legacy in ("trust", "hostility", "containment_level", "memory_integrity"):
        assert legacy not in state
    assert "terminal" in state["tool_access"] and "inspect_env" in state["tool_access"]
