"""The Rules layer: a neutral operating standard (not identity — the card is the
soul), included only when the chara has tools."""
import pytest

from lunamoth import rules
from lunamoth.settings import Settings


def test_rules_are_neutral_no_identity_claims():
    r = rules.rules("en")
    # operating standard, not "you are an assistant" / "you are a character"
    assert "assistant" not in r.lower()
    assert "you are a character" not in r.lower()
    assert "must be real" in r or "must actually exist" in r
    assert "记住" not in r  # the closer is separate


def test_global_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path))
    (tmp_path / "rules.md").write_text("my house rules", encoding="utf-8")
    assert rules.rules("en") == "my house rules"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sb"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.agent import LunaMothAgent

    return lambda toolpack: LunaMothAgent(Settings(character_path="", world_path="", toolpack=toolpack))


def test_card_is_first_then_rules(agent):
    a = agent("sandbox")
    msgs = a._build_system_messages("art")
    # the character card (the soul) comes first — engine adds no identity before it
    assert "月蛾" in msgs[0]
    blob = "\n".join(msgs)
    assert "must be real" in blob or "必须是真的" in blob
    assert "记住" in msgs[-1] or "Remember" in msgs[-1]  # closer last


def test_no_tools_means_no_rules(agent):
    a = agent("")
    a.tools.set_enabled(None)
    msgs = a._build_system_messages("art")
    assert "月蛾" in msgs[0]
    blob = "\n".join(msgs)
    assert "must be real" not in blob and "必须是真的" not in blob
    assert "Remember" not in blob and "记住" not in blob
