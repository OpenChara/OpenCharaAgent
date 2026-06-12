import pytest

from lunamoth.front import terminal


class DummyHandle:
    class Settings:
        quiet = 0

    settings = Settings()

    class Snap:
        rest_until = 0

    def __init__(self):
        self.calls = 0

    def snapshot(self):
        return self.Snap()

    def stream_idle(self):
        self.calls += 1
        if self.calls <= 2:
            raise RuntimeError("HTTP 401: User not found")
        yield from ()


def test_permanent_error_backoff_schedule_and_user_reset(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(terminal.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(terminal.time, "time", lambda: now["t"])
    monkeypatch.setattr(terminal, "_stream_with_interrupt", lambda prefix, events, allow_interrupt=True: (list(events), None))
    monkeypatch.setattr(terminal, "_cooldown", lambda seconds: None)
    monkeypatch.setattr(terminal, "_prompt", lambda: None)

    state = terminal.TerminalState()
    handle = DummyHandle()
    assert terminal._idle_ready(state, handle, last_user_at=0.0)

    terminal._run_idle_cycle(state, handle, "x> ", 0.0, interactive=False)
    assert state.idle_backoff == 60
    assert state.idle_blocked_until == pytest.approx(1060)
    assert not terminal._idle_ready(state, handle, last_user_at=0.0)

    now["t"] = 1060
    assert terminal._idle_ready(state, handle, last_user_at=0.0)
    terminal._run_idle_cycle(state, handle, "x> ", 0.0, interactive=False)
    assert state.idle_backoff == 120
    assert state.idle_blocked_until == pytest.approx(1180)

    # A real operator message resets the idle-only gate; interactive replies are
    # not suppressed by the permanent-error backoff.
    state.reset_idle_backoff()
    assert state.idle_backoff == 0
    assert state.idle_delay_remaining() == 0
    assert terminal._idle_ready(state, handle, last_user_at=0.0)


def test_permanent_model_error_prefixes_only():
    assert terminal.permanent_model_error("HTTP 401: bad")
    assert terminal.permanent_model_error("HTTP 403: forbidden")
    assert terminal.permanent_model_error("HTTP 404: model")
    assert not terminal.permanent_model_error("HTTP 429: rate limited")
    assert not terminal.permanent_model_error("connection failed: timeout")
