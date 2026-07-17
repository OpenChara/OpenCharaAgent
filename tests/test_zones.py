"""Three-zone prompt assembly: stable prefix, history, volatile tail."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from chara.session.settings import Settings


def _write_card(path: Path, *, phi: str = "", rules_closer: str = "", goals: list[str] | None = None,
                wishes: list[str] | None = None, polaris: str = "", book: list[dict] | None = None) -> Path:
    chara: dict[str, object] = {"toolpack": "sandbox"}
    if rules_closer:
        chara["rules_closer"] = rules_closer
    if goals is not None:
        chara["goals"] = goals
    if wishes is not None:
        chara["wishes"] = wishes
    if polaris:
        chara["polaris"] = polaris
    data: dict[str, object] = {
        "name": "TestCard",
        "description": "Persona marker.",
        "personality": "Curious.",
        "scenario": "A quiet test room.",
        "first_mes": "",
        "mes_example": "",
        "system_prompt": "System marker for {{char}} and {{user}}.",
        "post_history_instructions": phi,
        "extensions": {"chara": chara},
    }
    if book is not None:
        # The card's embedded character_book is the ONE world source.
        data["character_book"] = {"name": "test-world", "entries": book}
    payload = {"data": data}
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

    from chara.core.agent import CharaAgent

    def make(*, card: Path | None = None, toolpack: str = "sandbox",
             reset: bool = True, **kw):
        settings = Settings(
            provider="mock",
            character_path=str(card or ""),
            toolpack=toolpack,
            **kw,
        )
        a = CharaAgent(settings)
        if reset:
            a.transcript.reset()
        a.state.set_network(False)
        return a

    return make


def _hash(blocks: list[str]) -> str:
    raw = json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _blob(blocks: list[str]) -> str:
    return "\n\n".join(blocks)


def test_stable_prefix_hash_is_identical_across_turns(agent_factory):
    a = agent_factory()
    s = a.make_session()
    stable = a._stable_prefix()
    h1 = _hash(stable)
    s.context.add("user", "first turn")
    a._volatile_tail(a._scan_text(s, "first turn"), s)
    s.context.add("assistant", "first reply")
    s.context.add("user", "second turn")
    a._volatile_tail(a._scan_text(s, "second turn"), s)
    assert a._stable_prefix() is stable
    assert _hash(a._stable_prefix()) == h1


def test_runtime_flips_and_worldinfo_change_only_volatile_tail(agent_factory, tmp_path):
    card = _write_card(tmp_path / "card.json", book=[
        {"keys": ["trigger"], "content": "KEY-VOLATILE", "insertion_order": 1},
    ])
    a = agent_factory(card=card)
    s = a.make_session()
    stable_hash = _hash(a._stable_prefix())
    before = _blob(a._volatile_tail(a._scan_text(s, "plain"), s))

    a.state.set_network(True)
    after = _blob(a._volatile_tail(a._scan_text(s, "trigger"), s))

    assert _hash(a._stable_prefix()) == stable_hash
    assert before != after
    assert "network=on" in after
    assert "operator=" not in after  # presence retired: no operator token in env facts
    assert "KEY-VOLATILE" in after
    assert "KEY-VOLATILE" not in _blob(a._stable_prefix())


def test_env_facts_line_is_dynamic_only(agent_factory, tmp_path):
    """The volatile env line carries only the dynamic facts (isolation/network/
    date). The static workspace/works/assets geography is taught once in the
    CACHED stable prefix (rules.py) — re-shipping it every turn burned tokens."""
    card = _write_card(tmp_path / "card.json")
    a = agent_factory(card=card)
    s = a.make_session()
    tail = _blob(a._volatile_tail(a._scan_text(s, "hi"), s))
    assert "Environment: isolation=" in tail and "network=" in tail and "date=" in tail
    assert "workspace is your private" not in tail
    assert "reference shelf" not in tail
    # ...and the geography still lives in the stable prefix, once.
    assert "works/" in _blob(a._stable_prefix())


def test_card_phi_is_last_and_absent_from_persona_block(agent_factory, tmp_path):
    card = _write_card(tmp_path / "card.json", phi="PHI-LAST for {{char}}/{{user}}")
    a = agent_factory(card=card)
    a.tools.set_enabled(None)
    s = a.make_session()
    assert a.character is not None
    persona = a.character.render_system(a.settings.user_name)
    assert "PHI-LAST" not in persona

    s.context.add("user", "hello")
    stable = a._stable_prefix()
    volatile = a._volatile_tail(a._scan_text(s, "hello"), s)
    messages = a.llm._messages("hello", s.context.render(), stable, volatile, in_context=True)
    assert messages[-1] == {"role": "system", "content": "PHI-LAST for TestCard/操作者"}


def test_worldinfo_constants_prefix_keywords_recalled_no_sticky(agent_factory, tmp_path):
    card = _write_card(tmp_path / "card.json", book=[
        {"keys": ["always"], "content": "CONST-PREFIX", "constant": True, "insertion_order": 1},
        {"keys": ["spark"], "content": "KEY-TAIL", "insertion_order": 2},
    ])
    a = agent_factory(card=card)
    s = a.make_session()

    # Constants are the fixed overview and ride the CACHED prefix; keyword
    # entries never do.
    assert "CONST-PREFIX" in _blob(a._stable_prefix())
    assert "KEY-TAIL" not in _blob(a._stable_prefix())

    # Constants do not double into the volatile tail.
    assert "CONST-PREFIX" not in _blob(a._volatile_tail(a._scan_text(s, "anything"), s))

    # A keyword entry recalls exactly while its key is in the scan window —
    # no sticky tail-off, the shallow window itself smooths recall.
    s.context.add("user", "spark is too old")
    for i in range(5):
        s.context.add("assistant", f"neutral {i}")
    assert "KEY-TAIL" not in _blob(a._volatile_tail(a._scan_text(s, "neutral now"), s))
    assert "KEY-TAIL" in _blob(a._volatile_tail(a._scan_text(s, "spark now"), s))
    assert "KEY-TAIL" not in _blob(a._volatile_tail(a._scan_text(s, "neutral again"), s))


def test_worldinfo_budget_cap_truncates_by_order(agent_factory, tmp_path, monkeypatch):
    card = _write_card(tmp_path / "card.json", book=[
        {"keys": ["cap"], "content": "tiny", "insertion_order": 1},
        {"keys": ["cap"], "content": "B" * 80, "insertion_order": 2},
    ])
    a = agent_factory(card=card)
    s = a.make_session()
    monkeypatch.setattr(a, "context_limit", lambda: 50)  # 10% cap ~= 5 rough tokens
    tail = _blob(a._volatile_tail(a._scan_text(s, "cap"), s))
    assert "tiny" in tail
    assert "BBBB" not in tail


def test_compaction_summary_persists_and_restore_avoids_resummarizing(agent_factory, monkeypatch):
    from chara.core import compaction
    # This fast test uses a 4000-token window; drop the 64K trigger floor so the
    # tail budget (keyed to the threshold) fits the tiny window. The floor itself
    # is covered in tests/test_compaction.py.
    monkeypatch.setattr(compaction, "MINIMUM_CONTEXT_LENGTH", 0)

    a = agent_factory(toolpack="")
    s = a.make_session()
    for i in range(50):
        s.context.add("user" if i % 2 == 0 else "assistant", f"message {i} " + "x" * 500)
    s.context.max_tokens = 4000
    s.context.trim_buffer_tokens = 0

    calls = {"n": 0}

    def fake_raw_complete(self, messages, max_tokens=1024, timeout=60.0):
        calls["n"] += 1
        return "PERSISTED SUMMARY"

    from chara.core.llm import LLMClient

    monkeypatch.setattr(LLMClient, "is_live", lambda self: True)
    monkeypatch.setattr(LLMClient, "raw_complete", fake_raw_complete)
    assert a._maybe_compact(s, force=True)
    assert calls["n"] == 1

    b = agent_factory(toolpack="", reset=False)
    s2 = b.make_session()
    assert s2.context.messages[0]["kind"] == "summary"
    assert "PERSISTED SUMMARY" in s2.context.messages[0]["content"]
    assert calls["n"] == 1


def test_volatile_tail_never_enters_transcript(agent_factory, tmp_path, monkeypatch):
    card = _write_card(tmp_path / "card.json", phi="PHI-NEVER-PERSIST", book=[
        {"keys": ["spark"], "content": "WI-NEVER-PERSIST", "insertion_order": 1},
    ])
    a = agent_factory(card=card)
    s = a.make_session()

    def canned(*_args, **_kwargs):
        def gen():
            from chara.protocol import TextDelta
            yield TextDelta("reply", "say")
        return gen()

    monkeypatch.setattr(a, "_reply_stream", canned)
    list(a.stream_handle("spark", s))

    with sqlite3.connect(a.transcript.path) as conn:
        rows = conn.execute("SELECT content FROM messages WHERE epoch=? ORDER BY id", (a.transcript.epoch(),)).fetchall()
    text = "\n".join(row[0] for row in rows)
    assert "spark" in text and "reply" in text
    assert "WI-NEVER-PERSIST" not in text
    assert "PHI-NEVER-PERSIST" not in text
    assert "Environment:" not in text


def test_card_polaris_seeds_and_survives_reconfigure(agent_factory, tmp_path):
    # The card's `polaris` string seeds the store; a user edit on a live chara
    # survives a later reconfigure (seed_once never clobbers an existing value).
    card = _write_card(tmp_path / "card.json", polaris="touch the moon")
    a = agent_factory(card=card)
    assert a.polaris.get() == "touch the moon"
    a.polaris.set("my own star")
    again = agent_factory(card=card)
    assert again.polaris.get() == "my own star"


def test_card_legacy_goals_list_is_ignored(agent_factory, tmp_path):
    # No backward compat: a legacy `goals`/`wishes` LIST seeds NOTHING — only the
    # `polaris` string is read.
    card = _write_card(tmp_path / "card.json", wishes=["a wish"], goals=["legacy"])
    a = agent_factory(card=card)
    assert a.polaris.get() == ""
