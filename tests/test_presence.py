"""Presence awareness: the neutral enter/leave conversation markers (card-
overridable wording), the cross-process handoff file, and the presence-gated
request_permission tool."""
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


def test_default_card_overrides_the_presence_markers(agent):
    """The bundled default card declares on_attach/on_detach, so its enter/leave
    markers use that card wording (macros applied) instead of the neutral default."""
    from lunamoth import presence

    a = agent()
    entered = presence.marker_text(a.character, "entered", a.char_name(), a.settings.user_name, a.lang == "zh")
    left = presence.marker_text(a.character, "left", a.char_name(), a.settings.user_name, a.lang == "zh")
    assert a.settings.user_name in entered  # the card's on_attach names {{user}}
    assert left.strip()


def test_card_without_overrides_uses_the_neutral_default():
    from lunamoth.content.cards import CharacterCard
    from lunamoth import presence

    bare = CharacterCard(name="Visitor")
    assert presence.marker_text(bare, "entered", "Visitor", "op", False) == "[op joined the conversation.]"
    assert presence.marker_text(bare, "left", "Visitor", "op", False) == "[op left the conversation.]"
    assert presence.marker_text(bare, "entered", "Visitor", "op", True) == "［op进入了对话。］"


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
    # Count system lines (the markers) rather than match text — the marker wording
    # is card-overridable (the default card supplies its own on_attach/on_detach).
    def n_sys():
        return sum(1 for m in handle._session.context.messages if m.get("role") == "system")

    base = n_sys()
    list(handle.stream_user("在吗？"))
    assert n_sys() == base + 1  # the entered marker, exactly once on first speech
    list(handle.stream_user("还在吗"))
    assert n_sys() == base + 1  # a second message does NOT add another entered marker
    handle.detach()
    # Departure is NON-BLOCKING: NOT injected into the live context now (that would
    # interrupt work in flight); it is QUEUED, flushed at the chara's next own cycle.
    assert n_sys() == base + 1            # no immediate departure marker
    assert a.presence.pop_event() != ""   # but it was queued for the next cycle


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


def test_detach_marker_is_flushed_at_the_next_self_work_cycle(agent):
    """The departure queued on detach is injected at the START of the next idle
    cycle (so in-flight work finishes first), NOT immediately on detach."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.presence.mark_met()
    handle = CharaHandle(agent=a)
    handle.attach(present=True)
    list(handle.stream_user("做个任务"))          # operator spoke → visit_spoke=True
    handle.detach()                               # queues the departure (non-blocking)
    session = handle._session
    before = sum(1 for m in session.context.messages if m.get("role") == "system")
    list(a.stream_think(session))                 # next self-work cycle flushes it first
    after = sum(1 for m in session.context.messages if m.get("role") == "system")
    assert after >= before + 1                    # the queued departure marker landed
    assert a.presence.pop_event() == ""           # consumed, not left dangling


def test_first_meeting_greets_even_after_self_work(agent):
    """A live chara may self-work (writing transcript) before you first open the
    chat; the first human attach must STILL show its first_mes intro — the greeting
    is gated on first_meeting(), not on an empty transcript."""
    from lunamoth.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.presence.path.unlink(missing_ok=True)
    a.transcript.append_message({"role": "assistant", "content": "(it muses to itself)", "kind": "think"})
    handle = CharaHandle(agent=a)
    info = handle.attach(present=True)
    assert info.opening == "greeting" and info.opening_text   # not suppressed by prior self-work
    assert info.restored                                      # the self-work still restores for display


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
