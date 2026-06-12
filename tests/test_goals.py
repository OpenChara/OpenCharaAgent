"""Goals: charas are goal-driven — operator ⭑ + the chara's own, self-managed."""
import pytest

from lunamoth.tools.goals import GoalStore
from lunamoth.session.settings import Settings


def test_store_roundtrip_and_states(tmp_path):
    g = GoalStore(tmp_path / "goals.json")
    a = g.add("write a nocturne", by="chara")
    b = g.add("answer the operator's letter", by="operator")
    assert [x["id"] for x in g.active()] == [a["id"], b["id"]]
    g.set_status(a["id"], "done")
    assert [x["id"] for x in g.active()] == [b["id"]]
    # A fresh process sees the same goals.
    again = GoalStore(tmp_path / "goals.json")
    assert {x["id"]: x["status"] for x in again.all()} == {a["id"]: "done", b["id"]: "active"}


def test_render_block_marks_operator_goals(tmp_path):
    g = GoalStore(tmp_path / "goals.json")
    assert g.render_block() == ""  # no goals -> no block, no prompt noise
    g.add("compose", by="chara")
    op = g.add("tidy the workspace", by="operator")
    block = g.render_block()
    assert "compose" in block and f"{op['id']}: ⭑ tidy the workspace" in block
    g.set_status(op["id"], "dropped")
    assert "tidy" not in g.render_block()


def test_bad_inputs(tmp_path):
    g = GoalStore(tmp_path / "goals.json")
    with pytest.raises(ValueError):
        g.add("   ")
    with pytest.raises(ValueError):
        g.set_status("g99", "done")
    gid = g.add("x")["id"]
    with pytest.raises(ValueError):
        g.set_status(gid, "finished")  # not a valid status


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "sandbox")
        return LunaMothAgent(Settings(character_path="", **kw))

    return make


def test_chara_manages_goals_through_tools(agent):
    a = agent()
    out = a.tools.call("add_goal", text="learn the operator's favorite key")
    assert out["ok"] and "g" in out["data"]
    gid = a.goals.active()[-1]["id"]
    out = a.tools.call("set_goal_status", goal_id=gid, status="done")
    assert out["ok"] and "done" in out["data"]


def test_active_goals_steer_the_system_prompt(agent):
    a = agent()
    a.goals.add("finish the moonlight study", by="operator")
    blob = "\n".join(a._build_system_messages("hi"))
    assert "finish the moonlight study" in blob and "⭑" in blob
