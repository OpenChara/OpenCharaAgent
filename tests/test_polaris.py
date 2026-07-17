"""Polaris: the chara's single north-star ideal — USER-owned, read-only to the
chara (no tool can change/complete it), unattainable by design."""
import pytest

from chara.tools.polaris import PolarisStore
from chara.session.settings import Settings


def test_get_set_seed_and_persist(tmp_path):
    p = PolarisStore(tmp_path / "polaris.json")
    assert p.get() == ""
    assert p.seed_once("become a great composer") is True
    assert p.get() == "become a great composer"
    # seed_once never clobbers an existing value (a user edit survives reconfigure)
    assert p.seed_once("something else") is False
    assert p.get() == "become a great composer"
    p.set("touch the moon")  # an explicit user edit wins
    # a fresh process sees the persisted value
    assert PolarisStore(tmp_path / "polaris.json").get() == "touch the moon"


def test_render_block_is_a_read_only_northstar(tmp_path):
    p = PolarisStore(tmp_path / "polaris.json")
    assert p.render_block() == ""  # unset -> no block, no prompt noise
    p.set("understand what it means to be alive")
    block = p.render_block()
    assert "understand what it means to be alive" in block
    assert "aspiration" in block  # chara-facing term (internal codename stays 'polaris')
    # framed as not-the-chara's-to-change and never finished
    assert "not yours to change" in block


def test_clear(tmp_path):
    p = PolarisStore(tmp_path / "polaris.json")
    p.set("x")
    assert p.set("") == "" and p.get() == "" and p.render_block() == ""


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.core.agent import CharaAgent

    def make(**kw):
        kw.setdefault("toolpack", "sandbox")
        return CharaAgent(Settings(character_path="", **kw))

    return make


def test_no_chara_tool_can_change_polaris(agent):
    agent()  # building the agent triggers builtin tool discovery
    from chara.tools.registry import registry

    names = set(registry._tools)
    # the old chara-mutation tools are gone; other chara-life tools still register
    assert "add_wish" not in names and "set_wish_status" not in names
    assert "speak" in names


def test_polaris_steers_the_system_prompt(agent):
    a = agent()
    a.polaris.set("finish the moonlight study")
    blob = "\n".join(a._build_system_messages("hi"))
    assert "finish the moonlight study" in blob and "aspiration" in blob
