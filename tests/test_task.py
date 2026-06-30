"""Tests for the chara's `task` store (life-threads) + the `task` tool."""
import json

import pytest

from lunamoth.tools.task import TaskStore, _MAX_ACTIVE


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "task.json")


def test_add_assigns_stable_ids_and_persists(store, tmp_path):
    a = store.add("write the first chapter")
    b = store.add("learn the lute")
    assert a["id"] == "t1" and b["id"] == "t2"
    assert a["status"] == "active"
    # A fresh store over the same file sees them (persisted).
    again = TaskStore(tmp_path / "task.json")
    assert [t["id"] for t in again.active()] == ["t1", "t2"]


def test_complete_seals_and_drops_from_active(store):
    store.add("draft the song")
    store.complete("t1")
    assert store.active() == []
    done = store.done()
    assert len(done) == 1 and done[0]["status"] == "done" and "done_at" in done[0]


def test_sealed_task_cannot_be_edited_reopened_or_removed(store):
    store.add("explore the eastern range")
    store.complete("t1")
    with pytest.raises(ValueError):
        store.update("t1", "changed")
    with pytest.raises(ValueError):
        store.complete("t1")          # already done
    with pytest.raises(ValueError):
        store.remove("t1")            # a record, not deletable


def test_update_and_remove_active(store):
    store.add("a")
    store.update("t1", "a refined")
    assert store.active()[0]["content"] == "a refined"
    store.remove("t1")
    assert store.active() == []


def test_active_cap_enforced(store):
    for i in range(_MAX_ACTIVE):
        store.add(f"thread {i}")
    with pytest.raises(ValueError):
        store.add("one too many")


def test_missing_id_raises(store):
    with pytest.raises(ValueError):
        store.complete("nope")


def test_seed_once_is_noop_after_chara_edits(store):
    assert store.seed_once("a starter thread") is True
    assert [t["content"] for t in store.active()] == ["a starter thread"]
    # Seeding again (e.g. a reconfigure) must not clobber the chara's own list.
    assert store.seed_once("different starter") is False
    assert [t["content"] for t in store.active()] == ["a starter thread"]


def test_seed_accepts_a_list(store):
    assert store.seed_once(["one", "two"]) is True
    assert [t["content"] for t in store.active()] == ["one", "two"]


def test_render_active_lists_ids(store):
    store.add("cross the river")
    block = store.render_block(has_aspiration=True)
    assert "[t1]" in block and "cross the river" in block
    assert "task" in block.lower()


def test_render_empty_invites_only_with_aspiration(store):
    # No aspiration to derive from → no nag.
    assert store.render_block(has_aspiration=False) == ""
    # With an aspiration → a non-coercive invitation that also blesses having none.
    inv = store.render_block(has_aspiration=True)
    assert "no task" in inv.lower()
    assert "or have none" in inv.lower() or "fine" in inv.lower()


def test_tool_dispatch_add_complete(tmp_path):
    from types import SimpleNamespace

    from lunamoth.tools.builtin.task import task as task_tool

    ctx = SimpleNamespace(task=TaskStore(tmp_path / "task.json"))
    out = json.loads(task_tool({"action": "add", "content": "make a thing"}, ctx))
    assert out["active"][0]["content"] == "make a thing"
    out = json.loads(task_tool({"action": "complete", "id": "t1"}, ctx))
    assert out["active"] == [] and out["done_count"] == 1


def test_tool_errors_surface(tmp_path):
    from types import SimpleNamespace

    from lunamoth.tools.builtin.task import task as task_tool

    ctx = SimpleNamespace(task=TaskStore(tmp_path / "task.json"))
    assert "__tool_error__" in task_tool({"action": "bogus"}, ctx)
    assert "__tool_error__" in task_tool({"action": "complete"}, ctx)  # no id
