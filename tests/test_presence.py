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
        return LunaMothAgent(Settings(character_path="", world_path="", **kw))

    return make


def test_default_card_declares_presence_prompts(agent):
    a = agent()
    assert a.settings.user_name in a.attach_event_text()
    assert a.settings.user_name in a.detach_event_text()


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
