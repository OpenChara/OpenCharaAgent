"""Spotlight panel: console commands drive the right-side view-only panel."""
import asyncio
import json

import pytest


@pytest.fixture
def tui_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    # CONFIG_DIR is resolved at chara.config import time — under a full pytest
    # run an earlier test already pinned it, so write the config wherever it
    # ACTUALLY points (otherwise the TUI boots into the welcome screen and the
    # keystrokes land there instead of the console input).
    from chara.session.settings import config_path

    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"provider": "mock"}))
    return tmp_path


def test_panel_routing(tui_env):
    from chara.front.tui import OpenCharaAgentTUI

    async def scenario():
        app = OpenCharaAgentTUI(patience=999, mode_override="chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._panel_view() == "telemetry"

            async def cmd(text, expect_view):
                app.input.value = text
                await pilot.press("enter")
                await pilot.pause()
                assert app._panel_view() == expect_view, (text, app._panel_view())

            await cmd("/help", "help")        # help lives in the panel, not the console
            # /polaris: the operator SETS the chara's north-star and views it — all
            # through the CharaHandle (frontends never touch backend objects). The
            # chara cannot change it; there is no completion/list to manage.
            app.input.value = "/polaris 给月蛾织一首永远写不完的夜曲"
            await pilot.press("enter")
            await pilot.pause()
            assert app.handle.command("/polaris").data["polaris"] == "给月蛾织一首永远写不完的夜曲"
            await cmd("/polaris", "out")      # the value lights up the panel
            await cmd("/memory", "memory")
            await cmd("/status", "out")       # one-shot command output -> OUTPUT view
            await cmd("/files", "files")
            app.input.value = "!echo hi"      # operator shell -> terminal view
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert app._panel_view() == "term"
            await pilot.press("escape")       # Esc -> home to telemetry, caret in input
            await pilot.pause()
            assert app._panel_view() == "telemetry"
            assert app.focused is app.input

            # Thinking is hidden by default: ThinkDelta events never reach the
            # display, but they feed the ✶ indicator's token counter. ToolEnd
            # summaries render dimmed.
            from chara.protocol import TextDelta, ThinkDelta, ToolEnd
            before = len(app.display_segments)
            for ev in (ThinkDelta("secret pondering"), TextDelta("spoken words"),
                       ToolEnd("tool", summary="⚙ tool ✓")):
                app._handle_event(ev)
            shown = "".join(t for _, t in app.display_segments[before:])
            assert "secret pondering" not in shown
            assert "spoken words" in shown and "⚙ tool ✓" in shown
            dim_chunks = [t for s, t in app.display_segments[before:] if s == "dim"]
            assert any("⚙ tool ✓" in t for t in dim_chunks)  # machinery renders dim
            assert app._think_tokens > 0

    asyncio.run(scenario())


def test_conversation_and_mode_smoke(tui_env):
    """End-to-end: a real turn streams a reply, the shared /mode switch flips
    autonomy (the ONE switch — no separate pause), and Ctrl+C detaches cleanly.

    This guards against backend churn breaking the live path (CharaHandle's
    stream_user/attach/snapshot, the shared command registry) — the import-only
    checks miss a signature/field drift that only bites a running session."""
    from chara.front.tui import OpenCharaAgentTUI

    async def scenario():
        app = OpenCharaAgentTUI(patience=999, mode_override="chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._session_started
            assert app.char_name

            # A real message: queued (never dropped), then streamed by the mock
            # provider into the top pane, with the operator's line echoed above it.
            app.input.value = "hello there"
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if not app._is_streaming() and app.pending_input is None:
                    break
            shown = "".join(t for _, t in app.display_segments)
            assert "hello there" in shown          # operator echo in the transcript
            assert len(shown) > len("hello there")  # plus the streamed reply

            # /mode is the single autonomy control, served by the SHARED registry
            # (core/commands.py) — flipping it must update the frontend's view.
            app.input.value = "/mode live"
            await pilot.press("enter")
            await pilot.pause()
            assert app.mode == "live"
            app.input.value = "/mode chat"
            await pilot.press("enter")
            await pilot.pause()
            assert app.mode == "chat"

            # Status line renders against a live snapshot without raising.
            app._update_status()

            # Ctrl+C: clean shutdown + presence detach (the handoff bookkeeping).
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.shutdown_requested
            assert app._detached

    asyncio.run(scenario())
