"""The living-in-the-computer layer: speak channel, rest pacing, time sense."""
import json
import re
import time

import pytest

from lunamoth.session.settings import Settings


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


def test_speak_tool_surfaces_say_text(agent):
    a = agent()
    res = a._execute_tool({"function": {"name": "speak", "arguments": json.dumps({"text": "月升了。"})}})
    # The spoken words become a say-channel event; no dim machinery line.
    assert res["ok"] and res["say"] == "月升了。" and res["display"] == ""
    # Empty speech is rejected (anti-noise).
    res = a._execute_tool({"function": {"name": "speak", "arguments": json.dumps({"text": "  "})}})
    assert not res["ok"]


def test_rest_tool_sets_clamped_wake_time(agent):
    a = agent()
    out = a.tools.call("rest", minutes=99999)
    assert out["ok"], out
    until = a.state.load()["rest_until"]
    assert time.time() + 119 * 60 < until <= time.time() + 121 * 60  # clamped to 120min
    # A word from the user always wakes it early.
    s = a.make_session()
    a.handle("早上好", s)
    assert a.state.load()["rest_until"] == 0.0


def test_idle_tick_carries_only_a_timestamp(agent, monkeypatch):
    a = agent(toolpack="")
    a.transcript.reset()
    s = a.make_session()
    seen = {}

    def capture(user_text, *args, **kw):
        seen["text"] = user_text
        seen["in_context"] = kw.get("in_context")

        def gen():
            from lunamoth.protocol import TextDelta
            yield TextDelta("…", "muse")

        return gen()

    monkeypatch.setattr(a, "_reply_stream", capture)
    list(a.stream_think(s))
    # The convention: a timestamp-only user message = no one is speaking.
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]", seen["text"])
    assert seen["in_context"] is False  # ephemeral — zero residue in the context


def test_long_silence_gets_one_gap_note(agent):
    a = agent(toolpack="")
    a.transcript.reset()
    s = a.make_session()
    a._last_turn_wall = time.time() - 3 * 3600  # three hours of real silence
    a.handle("我回来了", s)
    notes = [c for r, c in s.context.pairs() if r == "system" and "3.0" in c]
    assert len(notes) == 1
    # The very next message gets NO note — sparse by construction.
    a.handle("再说一句", s)
    notes = [c for r, c in s.context.pairs() if r == "system"]
    assert len(notes) == 1


def test_quiet_command_persists(agent):
    from lunamoth.core import commands

    a = agent()
    s = a.make_session()
    reply = commands.execute(a, s, "/quiet 120")
    assert reply.ok and reply.data == {"quiet": 120}
    assert a.settings.quiet == 120


def test_say_event_flows_through_stream_agent(agent, monkeypatch):
    # End to end through the REAL tool loop: a fake one-turn model that calls
    # speak, then finishes. The user-facing words must arrive as a say TextDelta.
    from lunamoth.core.llm import LLMClient
    from lunamoth.protocol import MUSE, TextDelta

    a = agent()
    calls = {"n": 0}

    def fake_stream_turn(messages, tools, text_out, reasoning=None, channel="say"):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"id": "c1", "type": "function",
                     "function": {"name": "speak", "arguments": json.dumps({"text": "给你听一段。"})}}], "", "tool_calls"
        text_out.append("(done)")
        return [], "", "stop"

    monkeypatch.setattr(LLMClient, "is_live", lambda self: True)

    def gen_wrapper(self, messages, tools, text_out, reasoning=None, channel="say"):
        return fake_stream_turn(messages, tools, text_out, reasoning, channel)
        yield  # pragma: no cover — keeps this a generator for `yield from`

    monkeypatch.setattr(LLMClient, "_stream_turn", gen_wrapper)
    events = list(a.llm.stream_agent(
        "", [], [], [], a.tools.schemas(), a._execute_tool, channel=MUSE,
    ))
    says = [e for e in events if isinstance(e, TextDelta) and e.channel == "say"]
    assert says and "给你听一段。" in says[0].text
