"""The Soul layer: an always-on autonomy frame, plus capability-gated reality
grounding (a chara with tools must make real artifacts; a tool-less chara may
narrate fiction, like a SillyTavern import)."""
import pytest

from lunamoth import soul
from lunamoth.settings import Settings


def test_soul_is_autonomy_first_not_assistant():
    s = soul.soul("en")
    assert "character, not an assistant" in s
    s_zh = soul.soul("zh")
    assert "不是助手" in s_zh


def test_card_override_wins_over_default():
    assert soul.soul("en", card_soul="I am a custom soul.") == "I am a custom soul."


def test_global_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("global override soul", encoding="utf-8")
    assert soul.soul("en") == "global override soul"
    # a card override still beats the global file
    assert soul.soul("en", card_soul="card wins") == "card wins"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sb"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.agent import LunaMothAgent

    def make(toolpack):
        return LunaMothAgent(Settings(character_path="", world_path="", toolpack=toolpack))

    return make


def _has(msgs, *needles):
    blob = "\n".join(msgs)
    return all(n in blob for n in needles)


def test_with_tools_grounding_on_and_closer_last(agent):
    a = agent("sandbox")
    msgs = a._build_system_messages("art")
    # soul first
    assert "角色" in msgs[0] or "character" in msgs[0]
    # reality grounding present
    assert _has(msgs, "作品必须是真实") or _has(msgs, "works must be real")
    # closer is the very last block
    assert "记住" in msgs[-1] or "Remember" in msgs[-1]


def test_without_tools_no_grounding_no_closer(agent):
    a = agent("")
    a.tools.set_enabled(None)  # truly no tools
    msgs = a._build_system_messages("art")
    assert "角色" in msgs[0] or "character" in msgs[0]          # soul still present
    blob = "\n".join(msgs)
    assert "works must be real" not in blob and "作品必须是真实" not in blob
    assert "Remember" not in blob and "记住" not in blob
