"""Security tests for the browser tool's URL scheme/SSRF guard and the
background image worker's network re-check.

FIX 1 — browser_navigate / browser_cdp must reject non-http(s) schemes
(file:, ftp:, data:, chrome:, …) and SSRF targets (private/loopback/internal).
FIX 2 — the background image worker re-checks ctx.network_on() before the
outbound HTTP call, so a /net off between submit and run blocks the request.

The agent-browser driver is mocked throughout (no real Chromium). is_safe_url
is monkeypatched per-test so we never touch real DNS.
"""
from __future__ import annotations

import json

import pytest

from lunamoth.tools.builtin import browser, _browser_driver as drv


class FakeCtx:
    def __init__(self):
        self.browser = None
        self.llm = None

    def run_terminal(self, command, *, timeout=None, workdir=None, browser=False):
        return ""

    def isolation(self):
        return "sandbox"


class DriverStub:
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
    monkeypatch.setattr(drv, "find_agent_browser", lambda: "/usr/local/bin/agent-browser")
    monkeypatch.setattr(drv, "chromium_installed", lambda: True)


@pytest.fixture
def _safe_url_true(monkeypatch):
    """Pretend every http(s) host resolves to a public address (no real DNS)."""
    monkeypatch.setattr(browser, "is_safe_url", lambda url: True)


# ---------------------------------------------------------------------------
# FIX 1: scheme allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "file:///Users/x/.ssh/id_rsa",
    "ftp://example.com/x",
    "data:text/html,<script>alert(1)</script>",
    "chrome://settings",
    "view-source:http://example.com",
])
def test_navigate_rejects_disallowed_scheme(ctx, monkeypatch, _safe_url_true, bad_url):
    stub = DriverStub()
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": bad_url}, ctx))
    assert out["success"] is False
    assert "scheme" in out["error"].lower()
    assert stub.calls == []  # never reached the driver


def test_navigate_rejects_private_loopback(ctx, monkeypatch):
    # is_safe_url returns False for a private/loopback target.
    monkeypatch.setattr(browser, "is_safe_url", lambda url: False)
    stub = DriverStub()
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "http://127.0.0.1:8080/admin"}, ctx))
    assert out["success"] is False
    assert "private" in out["error"].lower() or "internal" in out["error"].lower()
    assert stub.calls == []


def test_navigate_allows_https_when_safe(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "OK", "url": "https://example.com"}},
        "snapshot": {"success": True, "data": {"snapshot": "x [@e1]", "refs": {"e1": {}}}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "https://example.com"}, ctx))
    assert out["success"] is True
    assert out["url"] == "https://example.com"
    assert stub.calls[0] == ("open", ["https://example.com"])


def test_navigate_allows_bare_host_normalized_to_https(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "OK", "url": "https://example.com"}},
        "snapshot": {"success": True, "data": {"snapshot": "", "refs": {}}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "example.com"}, ctx))
    assert out["success"] is True
    assert stub.calls[0] == ("open", ["https://example.com"])


def test_navigate_allows_about_blank_without_dns(ctx, monkeypatch):
    # about: must NOT trigger is_safe_url (no network target). Make is_safe_url
    # raise so the test fails if it is ever consulted for about:.
    def boom(url):
        raise AssertionError("is_safe_url must not run for about: URLs")
    monkeypatch.setattr(browser, "is_safe_url", boom)
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "", "url": "about:blank"}},
        "snapshot": {"success": True, "data": {"snapshot": "", "refs": {}}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "about:blank"}, ctx))
    assert out["success"] is True


def test_navigate_post_redirect_to_unsafe_blocked(ctx, monkeypatch):
    # First nav target is safe http(s); the page redirects to a private address.
    calls = {"n": 0}

    def is_safe(url):
        calls["n"] += 1
        # First call (pre-flight on https://safe.example) safe; the redirect
        # final_url (http://10.0.0.5/) is unsafe.
        return "10.0.0.5" not in url

    monkeypatch.setattr(browser, "is_safe_url", is_safe)
    stub = DriverStub(responses={
        "open": {"success": True, "data": {"title": "", "url": "http://10.0.0.5/"}},
    })
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_navigate({"url": "https://safe.example"}, ctx))
    assert out["success"] is False
    assert "redirect" in out["error"].lower()
    assert ("open", ["about:blank"]) in stub.calls


# ---------------------------------------------------------------------------
# FIX 1: browser_cdp navigation-verb guard
# ---------------------------------------------------------------------------

def test_cdp_page_navigate_rejects_file_scheme(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Page.navigate", "params": {"url": "file:///etc/passwd"}}, ctx))
    # browser_cdp uses tool_error → {"error": ...} (no "success" key).
    assert "error" in out and "scheme" in out["error"].lower()
    assert stub.calls == []  # CDP call never forwarded


def test_cdp_page_navigate_rejects_private(ctx, monkeypatch):
    monkeypatch.setattr(browser, "is_safe_url", lambda url: False)
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Page.navigate", "params": {"url": "http://169.254.169.254/"}}, ctx))
    assert "error" in out
    assert stub.calls == []


def test_cdp_page_navigate_allows_safe_https(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {"frameId": "f1"}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Page.navigate", "params": {"url": "https://example.com"}}, ctx))
    assert out["success"] is True
    assert stub.calls[0][0] == "cdp"


def test_cdp_non_navigation_method_unaffected(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {"targetInfos": []}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp({"method": "Target.getTargets", "params": {}}, ctx))
    assert out["success"] is True
    assert stub.calls[0][0] == "cdp"


def test_cdp_create_target_rejects_file_scheme(ctx, monkeypatch, _safe_url_true):
    """2026-07-02 P1: Target.createTarget also navigates → same guard as Page.navigate."""
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Target.createTarget", "params": {"url": "file:///etc/passwd"}}, ctx))
    assert "error" in out and "scheme" in out["error"].lower()
    assert stub.calls == []  # CDP call never forwarded


def test_cdp_create_target_rejects_metadata(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Target.createTarget", "params": {"url": "http://169.254.169.254/latest/"}}, ctx))
    assert "error" in out
    assert stub.calls == []


# ---------------------------------------------------------------------------
# P1 (2026-07-02): browser_console JS-eval can navigate around the guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "location.href='file:///etc/passwd'",
    "window.location = 'file:///Users/x/.ssh/id_rsa'",
    "fetch('http://169.254.169.254/latest/meta-data/')",
    "document.location.assign('ftp://internal/x')",
])
def test_console_eval_blocks_navigation_escape(ctx, monkeypatch, _safe_url_true, expr):
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": "ok"}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console({"expression": expr}, ctx))
    assert out["success"] is False
    assert stub.calls == []  # eval never forwarded to the driver


def test_console_eval_blocks_private_fetch(ctx, monkeypatch):
    monkeypatch.setattr(browser, "is_safe_url", lambda url: False)
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": "ok"}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console(
        {"expression": "fetch('http://10.0.0.1/secret')"}, ctx))
    assert out["success"] is False
    assert stub.calls == []


def test_console_eval_allows_benign_dom_read(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": "\"Title\""}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console({"expression": "document.title"}, ctx))
    assert out["success"] is True
    assert stub.calls[0][0] == "eval"


def test_console_eval_allows_public_fetch(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"eval": {"success": True, "data": {"result": "\"ok\""}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_console(
        {"expression": "fetch('https://api.example.com/v1')"}, ctx))
    assert out["success"] is True
    assert stub.calls[0][0] == "eval"


# ---------------------------------------------------------------------------
# P1 (2026-07-02): CDP Runtime.evaluate/callFunctionOn is the SAME JS-eval
# escape as browser_console — its expression is screened too.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,params", [
    ("Runtime.evaluate", {"expression": "location.href='file:///etc/passwd'"}),
    ("Runtime.callFunctionOn",
     {"functionDeclaration": "function(){location.href='file:///etc/passwd'}"}),
])
def test_cdp_runtime_eval_blocks_navigation_escape(ctx, monkeypatch, _safe_url_true, method, params):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp({"method": method, "params": params}, ctx))
    assert "error" in out and "scheme" in out["error"].lower()
    assert stub.calls == []  # CDP call never forwarded


def test_cdp_runtime_eval_allows_benign(ctx, monkeypatch, _safe_url_true):
    stub = DriverStub(responses={"cdp": {"success": True, "data": {"result": {"value": "ok"}}}})
    monkeypatch.setattr(drv, "run_browser_command", stub)
    out = json.loads(browser.browser_cdp(
        {"method": "Runtime.evaluate", "params": {"expression": "document.title"}}, ctx))
    assert out["success"] is True
    assert stub.calls[0][0] == "cdp"


# ---------------------------------------------------------------------------
# FIX 2: background image worker re-checks the network
# ---------------------------------------------------------------------------

class _NetCtx:
    def __init__(self, net_on: bool):
        self._net = net_on
        self.sandbox = self  # write_bytes lives here; never reached when net off

    def network_on(self) -> bool:
        return self._net

    def write_bytes(self, path, data):  # pragma: no cover - must not run net-off
        raise AssertionError("write_bytes must not run when network is off")


class _FakeReg:
    def __init__(self):
        import queue
        self.completion_queue = queue.Queue()


def test_image_worker_blocks_when_net_off(monkeypatch):
    from lunamoth.tools.builtin import media

    def boom(prompt, size):  # the HTTP call — must never happen net-off
        raise AssertionError("generate_bytes must not be called when network is off")

    monkeypatch.setattr(media, "generate_bytes", boom)

    reg = _FakeReg()
    ctx = _NetCtx(net_on=False)
    media._run_image_job(reg, ctx, "img-x", "a cat", "512x512", "works/a.png")

    evt = reg.completion_queue.get_nowait()
    assert evt["type"] == "image_gen"
    assert evt["status"] == "failed"
    assert "network" in evt["error"].lower()
    assert reg.completion_queue.empty()


def test_image_worker_runs_when_net_on(monkeypatch):
    from lunamoth.tools.builtin import media

    monkeypatch.setattr(media, "generate_bytes", lambda prompt, size, refs=None: b"PNGDATA")

    class _OkCtx:
        def network_on(self):
            return True

        class _SB:
            @staticmethod
            def write_bytes(path, data):
                return path
        sandbox = _SB()

    reg = _FakeReg()
    media._run_image_job(reg, _OkCtx(), "img-y", "a dog", "512x512", "works/b.png")
    evt = reg.completion_queue.get_nowait()
    assert evt["status"] == "ready"
    assert evt["path"] == "works/b.png"
    assert evt["bytes"] == len(b"PNGDATA")
