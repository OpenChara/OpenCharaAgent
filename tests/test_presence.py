"""Presence awareness: card-driven attach/detach prompts, the cross-process
handoff file, and the presence-gated request_permission tool."""
import pytest

from lunamoth.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return LunaMothAgent(Settings(character_path="", **kw))

    return make


def test_default_card_declares_presence_prompts(agent):
    a = agent()
    assert a.settings.user_name in a.attach_event_text()
    assert a.detach_event_text().strip()  # the card declares one; wording is its own


def test_card_without_prompts_means_no_events():
    from lunamoth.content.cards import CharacterCard
    from lunamoth import presence

    bare = CharacterCard(name="Visitor")
    assert presence.attach_text(bare, "Visitor", "op") == ""
    assert presence.detach_text(bare, "Visitor", "op") == ""


def test_presence_state_roundtrip(tmp_path):
    from lunamoth.presence import PresenceState

    p = PresenceState(tmp_path)
    assert p.first_meeting()
    p.mark_met()
    assert not p.first_meeting()
    assert p.pop_event() == ""
    p.queue_event("the operator left")
    assert p.pop_event() == "the operator left"
    assert p.pop_event() == ""  # consumed


def test_detach_queues_handoff_and_logs(agent):
    a = agent(toolpack="sandbox")
    session = a.make_session()
    a.note_detach(session)
    assert any(role == "system" for role, _ in session.context.pairs())
    assert a.presence.pop_event() != ""


def test_request_denied_when_operator_away(agent):
    a = agent(toolpack="sandbox")
    a.state.set_network(False)  # SANDBOX_ROOT is import-time global; reset shared state
    a.state.set_present(False)
    out = a.tools.call("request_permission", kind="network", reason="need pip")
    assert out["ok"] and "denied" in out["data"]
    assert a.state.load()["network_access"] is False


def test_request_granted_via_hook_when_present(agent):
    a = agent(toolpack="sandbox")
    a.state.set_present(True)
    asked = {}

    def approve(kind, reason, detail, wait_seconds):
        asked.update(kind=kind, reason=reason, wait=wait_seconds)
        return True

    a.tools.permission_hook = approve
    out = a.tools.call("request_permission", kind="network", reason="need pip", wait_seconds=30)
    assert out["ok"] and "granted" in out["data"]
    assert asked == {"kind": "network", "reason": "need pip", "wait": 30}
    assert a.state.load()["network_access"] is True


def test_request_denied_without_hook_even_when_present(agent):
    a = agent(toolpack="sandbox")
    a.state.set_network(False)  # SANDBOX_ROOT is import-time global; reset shared state
    a.state.set_present(True)
    a.tools.permission_hook = None
    out = a.tools.call("request_permission", kind="network", reason="x")
    assert out["ok"] and "denied" in out["data"]
    assert a.state.load()["network_access"] is False


def test_memory_grant_raises_budget(agent):
    a = agent(toolpack="sandbox")
    a.state.set_present(True)
    a.tools.permission_hook = lambda *args: True
    before = a.memory.limits.memory_chars
    out = a.tools.call("request_permission", kind="memory", reason="more room")
    assert out["ok"] and "granted" in out["data"]
    assert a.memory.limits.memory_chars > before


def test_mode_normalization():
    from lunamoth.presence import normalize_mode

    assert normalize_mode("live") == "live"
    assert normalize_mode("CHAT ") == "chat"
    # Pre-rename spellings map onto the two modes.
    assert normalize_mode("auto") == "live"
    assert normalize_mode("always") == "live"
    assert normalize_mode("off") == "chat"
    assert normalize_mode("banana") == "live"
    assert normalize_mode("") == "live"


def test_attach_never_wakes_a_resting_chara(agent):
    """Entering the room is presence bookkeeping only while the chara rests:
    no greeting, no arrival turn — a user MESSAGE is what wakes it."""
    import time as _time

    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(_time.time() + 600)
    handle = CharaHandle(agent=a)
    info = handle.attach(present=True)
    assert info.opening == "none" and info.opening_text == ""
    assert a.state.load()["user_present"] is True


# ---- entering never forces a turn; speak inserts entered; leave only if spoke ----

def test_entering_a_return_visit_forces_nothing(agent):
    """Entering a chara you've met before opens silently — no arrival turn,
    you just watch it do its own thing (owner decision 2026-06-13)."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()           # isolate from the shared repo sandbox transcript
    a.presence.mark_met()  # already met -> not the first_mes path
    handle = CharaHandle(agent=a)
    info = handle.attach(present=True)
    assert info.opening == "none" and not info.opening_text


def test_first_meeting_still_shows_the_card_greeting(agent):
    """A brand-new chara introduces itself once (first_mes)."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()                     # isolate from the shared repo sandbox transcript
    a.presence.path.unlink(missing_ok=True)  # shared SANDBOX_ROOT: ensure first meeting
    handle = CharaHandle(agent=a)
    info = handle.attach(present=True)
    assert info.opening == "greeting" and info.opening_text


def test_wordless_visit_leaves_no_trace(agent):
    """Enter, watch, leave without a word: nothing was added on entry and no
    departure marker is written — entering and leaving are not conversation."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.presence.mark_met()
    handle = CharaHandle(agent=a)
    handle.attach(present=True)
    before = len(handle._session.context.messages)
    handle.detach()
    assert len(handle._session.context.messages) == before
    assert a.presence.pop_event() == ""  # no departure handoff


def test_speaking_inserts_an_entered_marker_once_then_leaving_marks_departure(agent):
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.presence.mark_met()
    handle = CharaHandle(agent=a)
    handle.attach(present=True)
    list(handle.stream_user("在吗？"))
    systems = [m["content"] for m in handle._session.context.messages if m.get("role") == "system"]
    entered = [s for s in systems if "进入" in s or "joined" in s]
    assert len(entered) == 1  # the entered marker, exactly once
    # a second message does NOT add another entered marker
    list(handle.stream_user("还在吗"))
    systems = [m["content"] for m in handle._session.context.messages if m.get("role") == "system"]
    assert len([s for s in systems if "进入" in s or "joined" in s]) == 1
    # leaving after speaking writes one departure marker
    handle.detach()
    systems = [m["content"] for m in handle._session.context.messages if m.get("role") == "system"]
    assert len([s for s in systems if "离开" in s or "left" in s]) == 1
    assert a.presence.pop_event() != ""


def test_visit_to_a_resting_chara_leaves_no_departure_note(agent):
    import time as _time

    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(_time.time() + 600)
    handle = CharaHandle(agent=a)
    handle.attach(present=True)
    before = len(handle._session.context.messages)
    handle.detach()
    assert len(handle._session.context.messages) == before
    assert a.presence.pop_event() == ""


def test_reattach_does_not_replay_the_opening(agent):
    """A resident greets once per life, not once per page-load."""
    from lunamoth.protocol.api import CharaHandle
    from lunamoth.server.dispatch import JsonRpcDispatcher

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()                     # isolate from the shared repo sandbox transcript
    a.presence.path.unlink(missing_ok=True)  # shared SANDBOX_ROOT: ensure first meeting
    out = []
    d = JsonRpcDispatcher(out.append, handle=CharaHandle(agent=a))

    def opening(resp):
        res = resp["result"]
        return res["opening"] if isinstance(res, dict) else res.opening

    r1 = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert opening(r1) == "greeting"  # first meeting greets
    r2 = d.dispatch({"jsonrpc": "2.0", "id": 2, "method": "attach", "params": {}})
    assert opening(r2) == "none"      # reconnect does not


def test_background_adopt_then_human_attach_still_greets(agent):
    """The supervisor pre-attaches a resident with present=False (idle driving)
    BEFORE the human connects. That background adopt must NOT eat the human's
    opener — the first present=True attach still greets (regression: the
    'greet once per life' change had neutered every human greeting)."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.presence.path.unlink(missing_ok=True)    # shared SANDBOX_ROOT: ensure first meeting
    handle = CharaHandle(agent=a)
    bg = handle.attach(present=False)          # daemon adopts first
    assert bg.opening == "none"
    human = handle.attach(present=True)        # the human arrives
    assert human.opening in ("greeting", "arrival", "probe")
    assert human.opening_text
    # a reconnect (second human attach) is presence-only, no re-greet
    again = handle.attach(present=True)
    assert again.opening == "none"


def test_reconnect_shows_the_conversation_so_far(agent):
    """A reconnect must restore the conversation that happened since the child
    started (regression: the dispatch cached the empty background-attach
    snapshot and replayed it forever)."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    handle.attach(present=False)
    handle.attach(present=True)
    list(handle.stream_user("记住：项目代号 Moth"))   # a real exchange lands in context
    reattached = handle.attach(present=True)          # navigate away and back
    joined = " ".join(c for _, c in [(m.get("role"), m.get("content") or "")
                                     for m in [dict(x) for x in reattached.restored]])
    assert "项目代号 Moth" in joined


def test_display_restore_shows_tools_without_changing_model_context(agent):
    """The FRONTEND restore gains tool calls / results / reasoning, while the
    MODEL's replayed context (context.render()) is unchanged — tool results
    stay forensic for the model on purpose."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    # Persist a realistic tool round-trip straight into the transcript.
    a.transcript.append_message({"role": "user", "content": "list files"})
    a.transcript.append_message({"role": "assistant", "content": "",
                                 "reasoning_content": "I'll run terminal",
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "terminal", "arguments": "{}"}}]})
    a.transcript.append_message({"role": "tool", "tool_call_id": "c1", "content": "a.txt\nb.txt"})

    handle = CharaHandle(agent=a)
    info = handle.attach(present=True)
    restored = [dict(m) for m in info.restored]

    # The DISPLAY view carries reasoning, the tool call, and the tool result.
    assert any(m.get("reasoning_content") == "I'll run terminal" for m in restored)
    assert any(m.get("tool_calls") for m in restored)
    assert any(m.get("role") == "tool" and "a.txt" in str(m.get("content", "")) for m in restored)

    # The MODEL's replayed context is unchanged: render() drops reasoning and,
    # for paired calls, keeps tool results exactly as before this change.
    sess = handle._session
    view = sess.context.render(include_reasoning=False)
    assert all("reasoning_content" not in m for m in view)
    # The session context did NOT gain the display-only legacy rows.
    assert sess.context.messages == [dict(m) for m in a.transcript.load(
        max_messages=a.RESTORE_MAX_MESSAGES)]
