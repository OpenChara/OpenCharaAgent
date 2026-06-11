"""Spotlight panel: console commands drive the right-side view-only panel."""
import asyncio
import json

import pytest


@pytest.fixture
def tui_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    # CONFIG_DIR is resolved at lunamoth.config import time — under a full pytest
    # run an earlier test already pinned it, so write the config wherever it
    # ACTUALLY points (otherwise the TUI boots into the welcome screen and the
    # keystrokes land there instead of the console input).
    from lunamoth.session.settings import config_path

    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"provider": "mock"}))
    return tmp_path


def test_panel_routing(tui_env):
    from lunamoth.front.tui import LunaMothTUI

    async def scenario():
        app = LunaMothTUI(patience=999, mode_override="chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._panel_view() == "telemetry"

            async def cmd(text, expect_view):
                app.input.value = text
                await pilot.press("enter")
                await pilot.pause()
                assert app._panel_view() == expect_view, (text, app._panel_view())

            await cmd("/help", "help")        # help lives in the panel, not the console
            # /goal: add as operator, list on the panel, complete — all through
            # the CharaHandle (frontends never touch backend objects directly).
            app.input.value = "/goal 给月蛾织一首夜曲"
            await pilot.press("enter")
            await pilot.pause()
            active = [g for g in app.handle.snapshot(fresh=True).goals if g["status"] == "active"]
            gid = active[-1]["id"]
            assert active[-1]["by"] == "operator"
            await cmd("/goal", "out")         # the list lights up the panel
            app.input.value = f"/goal done {gid}"
            await pilot.press("enter")
            await pilot.pause()
            # (SANDBOX_ROOT is shared across the test run — assert only OUR goal.)
            snap = app.handle.snapshot(fresh=True)
            assert gid not in [g["id"] for g in snap.goals if g["status"] == "active"]
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
            from lunamoth.protocol import TextDelta, ThinkDelta, ToolEnd
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
