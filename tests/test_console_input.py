"""Console input caret: the REAL terminal cursor must sit on the insertion cell.

The TUI hides Textual's drawn block and shows the hardware cursor at
Input.cursor_screen_offset (that's also where the IME composition window
opens). Stock Textual parks it one cell right of the insertion point whenever
the caret is at the end of the value — i.e. the whole time you're typing.
ConsoleInput overrides the offset; these tests pin the corrected geometry.
"""
import asyncio
import json

import pytest


@pytest.fixture
def tui_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    from chara.session.settings import config_path

    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"provider": "mock"}))
    return tmp_path


def test_cursor_lands_on_insertion_cell(tui_env):
    from chara.front.tui import OpenCharaAgentTUI

    async def scenario():
        app = OpenCharaAgentTUI(patience=999, mode_override="chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            inp = app.input
            x0 = inp.content_region.x

            # Empty input: cursor on the very first cell, no phantom offset.
            assert inp.value == ""
            assert inp.cursor_screen_offset.x == x0

            # ASCII at end-of-value (the normal typing position): the cursor
            # sits where the NEXT character will appear — not one past it.
            inp.value = "abc"
            inp.cursor_position = 3
            await pilot.pause()
            assert inp.cursor_screen_offset.x == x0 + 3

            # CJK: cells, not characters — 你好 is 4 cells wide.
            inp.value = "你好"
            inp.cursor_position = 2
            await pilot.pause()
            assert inp.cursor_screen_offset.x == x0 + 4

            # Mid-value: between the two characters.
            inp.cursor_position = 1
            await pilot.pause()
            assert inp.cursor_screen_offset.x == x0 + 2

    asyncio.run(scenario())


def test_console_input_has_no_placeholder(tui_env):
    # The cursor cell is styled as plain text, so any placeholder's first
    # character renders bright — a typed-looking glyph you can't delete.
    from chara.front.tui import OpenCharaAgentTUI

    async def scenario():
        app = OpenCharaAgentTUI(patience=999, mode_override="chat")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.input.placeholder == ""

    asyncio.run(scenario())
