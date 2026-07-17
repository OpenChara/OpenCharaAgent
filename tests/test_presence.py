"""Mode normalization + the first-message (card first_mes) mechanism.

Presence (operator enter/leave awareness) was retired: the chara is INDEPENDENT
of whether a human is attached. There is no presence fact, no enter/leave marker,
no per-page greeting gate. The card's first_mes is shown exactly once — at the
chara's very first opening, recognized by an EMPTY transcript epoch — and is
persisted to the transcript FIRST, so it survives a process death or a dropped
frontend socket. `/reset` bumps the epoch to empty, so first_mes re-shows.
"""
import pytest

from chara.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.core.agent import CharaAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return CharaAgent(Settings(character_path="", **kw))

    return make


# ---- modes ---------------------------------------------------------------------

def test_mode_normalization():
    from chara.presence import normalize_mode

    assert normalize_mode("live") == "live"
    assert normalize_mode("CHAT ") == "chat"
    # Pre-rename spellings map onto the two modes.
    assert normalize_mode("auto") == "live"
    assert normalize_mode("always") == "live"
    assert normalize_mode("off") == "chat"
    assert normalize_mode("banana") == "live"
    assert normalize_mode("") == "live"


def test_presence_state_module_is_gone():
    """The PresenceState file/exports are deleted — only normalize_mode remains."""
    import chara.presence as presence

    assert hasattr(presence, "normalize_mode")
    assert not hasattr(presence, "PresenceState")
    assert not hasattr(presence, "marker_text")


# ---- the first-message mechanism (transcript is the single authority) ----------

def test_first_open_shows_the_card_greeting_on_an_empty_epoch(agent):
    """A brand-new chara introduces itself once (first_mes) the first time it is
    opened — recognized by an empty transcript epoch."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()  # fresh epoch → empty
    handle = CharaHandle(agent=a)
    info = handle.attach()
    assert info.opening == "greeting" and info.opening_text


def test_greeting_is_persisted_server_side_before_attach_returns(agent):
    """The opener reaches the transcript the moment attach() decides to greet —
    BEFORE attach returns, NOT on a frontend `greet` round-trip. So it survives a
    process death / dropped socket. A later greet RPC is idempotent."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    info = handle.attach()
    assert info.opening == "greeting" and info.opening_text

    def greet_rows():
        return [r for r in a.transcript.load(max_messages=0)
                if r.get("role") == "assistant" and info.opening_text[:30] in str(r.get("content", ""))]

    # persisted by attach() ALONE — no extra record_greeting/greet call yet
    assert len(greet_rows()) == 1, a.transcript.load(max_messages=0)
    # the frontend's later greet round-trip is a harmless no-op
    handle.record_greeting(info.opening_text)
    assert len(greet_rows()) == 1, "frontend greet must not double-record"


def test_opening_text_is_not_also_in_the_restored_tail(agent):
    """On the greeting attach, the opener is sent ONCE as opening_text; `restored`
    is captured before the commit, so the frontend never shows it twice."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    info = handle.attach()
    assert info.opening == "greeting"
    joined = " ".join(str(m.get("content") or "") for m in info.restored)
    assert info.opening_text not in joined


def test_reopen_does_not_replay_the_greeting(agent):
    """A non-empty epoch opens silently; the greeting rides `restored`, not a
    second opening — survives reconnect / page reload."""
    from chara.protocol.api import CharaHandle
    from chara.server.dispatch import JsonRpcDispatcher

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    out = []
    d = JsonRpcDispatcher(out.append, handle=CharaHandle(agent=a))

    def opening(resp):
        res = resp["result"]
        return res["opening"] if isinstance(res, dict) else res.opening

    r1 = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "attach", "params": {}})
    assert opening(r1) == "greeting"   # first open greets
    r2 = d.dispatch({"jsonrpc": "2.0", "id": 2, "method": "attach", "params": {}})
    assert opening(r2) == "none"       # reopen does not


def test_greeting_survives_a_new_handle_on_the_same_transcript(agent):
    """The KEY guarantee: the greeting authority is the transcript, not an
    in-memory flag. A fresh handle (e.g. a restarted child / new process) on the
    same already-greeted transcript must NOT re-greet."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    first = CharaHandle(agent=a).attach()
    assert first.opening == "greeting"
    # A brand-new handle wrapping the same agent/transcript: the row is on disk.
    again = CharaHandle(agent=a).attach()
    assert again.opening == "none"


def test_reset_reshows_the_greeting(agent):
    """/reset bumps the epoch to empty → the next open greets again (desired)."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    assert CharaHandle(agent=a).attach().opening == "greeting"
    assert CharaHandle(agent=a).attach().opening == "none"
    a.transcript.reset()  # like /reset
    assert CharaHandle(agent=a).attach().opening == "greeting"


def test_background_then_foreground_open_greets_once(agent):
    """A background open (the supervisor pre-attaching a resident) commits the
    greeting just like a foreground one — there is no present/away distinction.
    The point: it greets exactly once total, and a later open is silent."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    bg = handle.attach(present=False)      # back-compat arg accepted, ignored
    assert bg.opening == "greeting"        # empty epoch → greet (no present gate)
    again = handle.attach(present=True)    # next open is silent
    assert again.opening == "none"


def test_detach_leaves_no_trace_and_is_a_noop(agent):
    """Leaving the chat is not an event: detach touches nothing in the context."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    handle.attach()
    list(handle.stream_user("做个任务"))
    before = len(handle._session.context.messages)
    handle.detach()
    assert len(handle._session.context.messages) == before  # no departure marker


def test_speaking_inserts_no_presence_marker(agent):
    """A user message is just a user message — no "entered" marker is injected."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    handle.attach()

    def n_sys():
        return sum(1 for m in handle._session.context.messages if m.get("role") == "system")

    base = n_sys()
    list(handle.stream_user("在吗？"))
    assert n_sys() == base  # no presence/marker system line added


def test_env_facts_carry_no_operator_token(agent):
    """The volatile env-facts line no longer carries operator present/away —
    the chara's context is independent of attach state."""
    a = agent(toolpack="sandbox")  # tools on → env facts present
    s = a.make_session()
    tail = "\n\n".join(a._volatile_tail(a._scan_text(s, "x"), s))
    assert "operator=" not in tail


# ---- restore / display behavior (unchanged by the refactor) --------------------

def test_reconnect_shows_the_conversation_so_far(agent):
    """A reopen restores the conversation that happened since the child started."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    handle = CharaHandle(agent=a)
    handle.attach()
    list(handle.stream_user("记住：项目代号 Moth"))   # a real exchange lands in context
    reattached = handle.attach()                       # navigate away and back
    joined = " ".join(str(m.get("content") or "") for m in reattached.restored)
    assert "项目代号 Moth" in joined


def test_display_restore_shows_tools_without_changing_model_context(agent):
    """The FRONTEND restore gains tool calls / results / reasoning, while the
    MODEL's replayed context (context.render()) is unchanged."""
    from chara.protocol.api import CharaHandle

    a = agent()
    a.state.set_rest_until(0)
    a.transcript.reset()
    a.transcript.append_message({"role": "user", "content": "list files"})
    a.transcript.append_message({"role": "assistant", "content": "",
                                 "reasoning_content": "I'll run terminal",
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "terminal", "arguments": "{}"}}]})
    a.transcript.append_message({"role": "tool", "tool_call_id": "c1", "content": "a.txt\nb.txt"})

    handle = CharaHandle(agent=a)
    info = handle.attach()
    restored = [dict(m) for m in info.restored]

    assert any(m.get("reasoning_content") == "I'll run terminal" for m in restored)
    assert any(m.get("tool_calls") for m in restored)
    assert any(m.get("role") == "tool" and "a.txt" in str(m.get("content", "")) for m in restored)

    sess = handle._session
    view = sess.context.render(include_reasoning=False)
    assert all("reasoning_content" not in m for m in view)
    assert sess.context.messages == [dict(m) for m in a.transcript.load(
        max_messages=a.RESTORE_MAX_MESSAGES)]


# ---- FIX 3: StateSnapshot exposes the frozen-card visuals ----------------------

def test_snapshot_exposes_visual_fields(agent):
    """The snapshot carries avatar/sprite/bg/keyvisual so a chat view can show the
    real avatar. With no card art they are empty strings, never missing."""
    from chara.protocol.api import CharaHandle

    a = agent()
    handle = CharaHandle(agent=a)
    handle.attach()
    snap = handle.snapshot(fresh=True)
    for field in ("avatar_uri", "sprite_url", "bg_url", "keyvisual_url"):
        assert isinstance(getattr(snap, field), str)
    assert not hasattr(snap, "user_present")  # presence field retired
