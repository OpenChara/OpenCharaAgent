"""Headless smoke test for the plain-terminal driver (front/terminal.py).

front/terminal.py is BOTH the legacy plain terminal AND the background daemon
driver (`chara start` spawns `python -m chara.front.terminal`). A crash
here breaks every daemon, so this drives main() end-to-end against a fake
CharaHandle: a real attach, an operator turn, a couple shared /commands, the
/mode + /patience runtime-mirror sync, and a clean detach — with NO network and
NO real agent. It is the regression net for backend-surface drift.
"""
from __future__ import annotations

from chara.front import terminal
from chara.protocol import TextDelta
from chara.protocol.api import AttachInfo, Reply, StateSnapshot


def _snap(**over):
    base = dict(
        char_name="Tester", lang="en", mode="live", provider="p", model="m",
        reasoning="medium", reasoning_supported=True, show_thinking=False,
        user_name="op", isolation="sandbox", net_on=False,
        rest_until=0.0, quiet=300, patience=600.0, embodiment="literal", website=False,
        context_tokens=0, context_max=1000, memory_chars=0, memory_max=1,
        memory_text="", memory_path="/tmp", sandbox_root="/tmp",
        workspace_root="/tmp/workspace",
    )
    base.update(over)
    return StateSnapshot(**base)


class FakeHandle:
    """Records every call so the test can assert the driver exercised the
    backend surface and detached cleanly."""

    def __init__(self):
        self.attached = None
        self.detached = False
        self.user_turns: list[str] = []
        self.commands_run: list[str] = []
        self.greeting_recorded: str | None = None
        self.settings = type("S", (), {"quiet": 300})()
        self._mode = "live"
        self._patience = 600.0

    def set_permission_hook(self, hook):
        pass

    def set_clarify_hook(self, hook):
        pass

    def attach(self, present=True):
        self.attached = present
        return AttachInfo(
            char_name="Tester", lang="en", mode=self._mode, show_thinking=False,
            restored=(), opening="greeting", opening_text="hi there",
        )

    def record_greeting(self, text):
        self.greeting_recorded = text

    def snapshot(self, fresh=False):
        return _snap(mode=self._mode, patience=self._patience)

    def commands(self):
        return ()

    def stream_user(self, text):
        self.user_turns.append(text)
        yield TextDelta(text="ok", channel="say")

    def stream_idle(self):
        yield from ()

    def command(self, line):
        self.commands_run.append(line)
        if line.startswith("/mode"):
            self._mode = "chat"
            return Reply(True, "mode = chat", {"mode": "chat"})
        if line.startswith("/patience"):
            self._patience = 42.0
            return Reply(True, "patience = 42s", {"patience": 42.0})
        return Reply(True, f"ran {line}", {})

    def detach(self):
        self.detached = True


def _drive(monkeypatch, fake, script: list[str]):
    """Run terminal.main() interactively, feeding `script` lines one at a time."""
    monkeypatch.setattr(terminal, "CharaHandle", lambda *a, **k: fake)
    monkeypatch.setattr(terminal.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(terminal, "_cooldown", lambda seconds: None)
    monkeypatch.setattr(terminal, "_prompt", lambda: None)

    queue = list(script)

    def fake_ready():
        return bool(queue)

    def fake_read():
        return queue.pop(0) if queue else None

    monkeypatch.setattr(terminal, "_stdin_line_ready", fake_ready)
    monkeypatch.setattr(terminal, "_read_line", fake_read)
    # chat-mode start so the loop doesn't fire idle cycles before our script.
    return terminal.main(["--mode", "chat"])


def test_attach_turn_commands_detach(monkeypatch, capsys):
    fake = FakeHandle()
    rc = _drive(monkeypatch, fake, [
        "hello there",        # an operator turn
        "/status",            # a shared command
        "/mode live",         # autonomy switch — re-syncs state.eternal
        "/patience 42",       # pacing — re-syncs base_patience
        "/quit",              # clean exit
    ])
    assert rc == 0
    assert fake.attached is True            # real attach happened
    assert fake.greeting_recorded == "hi there"
    assert fake.user_turns == ["hello there"]
    assert "/status" in fake.commands_run
    assert "/mode live" in fake.commands_run
    assert "/patience 42" in fake.commands_run
    assert fake.detached is True            # detached cleanly on the way out


def test_mode_command_resyncs_idle_gate(monkeypatch):
    """/mode flips the loop's runtime idle gate (the ONE autonomy switch),
    proving there is no second pause concept and the shared command drives it."""
    fake = FakeHandle()

    captured = {}
    real_state_cls = terminal.TerminalState

    def spy_state(*a, **k):
        st = real_state_cls(*a, **k)
        captured["state"] = st
        return st

    monkeypatch.setattr(terminal, "TerminalState", spy_state)
    # Start in chat (eternal False), flip to live via /mode, then quit.
    rc = _drive(monkeypatch, fake, ["/mode live", "/quit"])
    assert rc == 0
    # FakeHandle.command maps any /mode to "chat"; the loop must mirror the
    # Reply.data mode, not the argument — proving it reads the shared backend.
    assert captured["state"].eternal is False


def test_operator_turn_model_error_does_not_crash(monkeypatch, capsys):
    """A failed request surfaces as a visible error but never crashes the loop
    (this process is the daemon driver)."""
    fake = FakeHandle()

    def boom(text):
        raise RuntimeError("HTTP 500: upstream blew up")
        yield  # pragma: no cover

    fake.stream_user = boom
    rc = _drive(monkeypatch, fake, ["trigger error", "/quit"])
    assert rc == 0
    assert fake.detached is True
    assert "model error" in capsys.readouterr().out
