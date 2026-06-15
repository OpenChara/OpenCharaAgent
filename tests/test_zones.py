"""Three-zone prompt assembly: stable prefix, history, volatile tail."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from lunamoth.session.settings import Settings


def _write_card(path: Path, *, phi: str = "", rules_closer: str = "", goals: list[str] | None = None,
                wishes: list[str] | None = None, book: list[dict] | None = None) -> Path:
    lunamoth: dict[str, object] = {"toolpack": "sandbox"}
    if rules_closer:
        lunamoth["rules_closer"] = rules_closer
    if goals is not None:
        lunamoth["goals"] = goals
    if wishes is not None:
        lunamoth["wishes"] = wishes
    data: dict[str, object] = {
        "name": "TestCard",
        "description": "Persona marker.",
        "personality": "Curious.",
        "scenario": "A quiet test room.",
        "first_mes": "",
        "mes_example": "",
        "system_prompt": "System marker for {{char}} and {{user}}.",
        "post_history_instructions": phi,
        "extensions": {"lunamoth": lunamoth},
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
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))

    from lunamoth.core import agent as agent_mod
    from lunamoth.tools import skills as skills_mod

    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(skills_mod, "SANDBOX_ROOT", sandbox)

    from lunamoth.core.agent import LunaMothAgent

    def make(*, card: Path | None = None, toolpack: str = "sandbox",
             reset: bool = True, **kw):
        settings = Settings(
            provider="mock",
            character_path=str(card or ""),
            toolpack=toolpack,
            **kw,
        )
        a = LunaMothAgent(settings)
        if reset:
            a.transcript.reset()
        a.state.set_network(False)
        a.state.set_present(False)
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
    a.state.set_present(True)
    after = _blob(a._volatile_tail(a._scan_text(s, "trigger"), s))

    assert _hash(a._stable_prefix()) == stable_hash
    assert before != after
    assert "network=on" in after and "operator=present" in after
    assert "KEY-VOLATILE" in after
    assert "KEY-VOLATILE" not in _blob(a._stable_prefix())


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


def test_worldinfo_constant_stable_keyword_shallow_scan_and_sticky(agent_factory, tmp_path):
    card = _write_card(tmp_path / "card.json", book=[
        {"keys": ["always"], "content": "CONST-STABLE", "constant": True, "insertion_order": 1},
        {"keys": ["spark"], "content": "KEY-TAIL", "insertion_order": 2},
    ])
    a = agent_factory(card=card)
    s = a.make_session()

    assert "CONST-STABLE" in _blob(a._stable_prefix())
    assert "KEY-TAIL" not in _blob(a._stable_prefix())

    s.context.add("user", "spark is too old")
    for i in range(5):
        s.context.add("assistant", f"neutral {i}")
    assert "KEY-TAIL" not in _blob(a._volatile_tail(a._scan_text(s, "neutral now"), s))

    assert "KEY-TAIL" in _blob(a._volatile_tail(a._scan_text(s, "spark now"), s))
    for _ in range(4):
        assert "KEY-TAIL" in _blob(a._volatile_tail(a._scan_text(s, "neutral"), s))
    assert "KEY-TAIL" not in _blob(a._volatile_tail(a._scan_text(s, "neutral"), s))


def test_worldinfo_budget_cap_truncates_by_order(agent_factory, tmp_path, monkeypatch):
    card = _write_card(tmp_path / "card.json", book=[
        {"keys": ["cap"], "content": "tiny", "insertion_order": 1},
        {"keys": ["cap"], "content": "B" * 80, "insertion_order": 2},
    ])
    a = agent_factory(card=card)
    s = a.make_session()
    monkeypatch.setattr(a, "context_limit", lambda: 20)  # 25% cap ~= 5 rough tokens
    tail = _blob(a._volatile_tail(a._scan_text(s, "cap"), s))
    assert "tiny" in tail
    assert "BBBB" not in tail


def test_compaction_summary_persists_and_restore_avoids_resummarizing(agent_factory, monkeypatch):
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

    from lunamoth.core.llm import LLMClient

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
            from lunamoth.protocol import TextDelta
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


def test_card_legacy_goals_seed_once(agent_factory, tmp_path):
    # Legacy `extensions.lunamoth.goals` still seeds (one-load migration).
    card = _write_card(tmp_path / "card.json", goals=["seed one", "seed two"])
    a = agent_factory(card=card)
    assert [g["text"] for g in a.wishes.all()] == ["seed one", "seed two"]
    a.wishes.add("operator-added", by="operator")

    again = agent_factory(card=card)
    assert [g["text"] for g in again.wishes.all()] == ["seed one", "seed two", "operator-added"]


def test_card_wishes_seed_and_take_precedence(agent_factory, tmp_path):
    # The new `wishes` key seeds; when both exist, wishes wins.
    card = _write_card(tmp_path / "card.json", wishes=["a wish"], goals=["legacy goal"])
    a = agent_factory(card=card)
    assert [g["text"] for g in a.wishes.all()] == ["a wish"]
