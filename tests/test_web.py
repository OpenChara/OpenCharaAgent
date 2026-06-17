"""Tests for the ported web_search + web_extract tools (builtin/web.py).

All HTTP / LLM are mocked; no real network or model is touched. We assert the
tools register, gate on network, resolve backends from env (and error clearly
when none is configured), enforce SSRF + secret-in-URL guards, apply the size
caps, and degrade to truncation when no LLM is available.
"""
from __future__ import annotations

import json

from types import SimpleNamespace

from lunamoth.tools.builtin import web
from lunamoth.tools.builtin import _url_safety
from lunamoth.tools.registry import registry, discover_builtin_tools


# --------------------------------------------------------------------------- #
# fake ctx
# --------------------------------------------------------------------------- #

class FakeLLM:
    def __init__(self, reply="## Summary\nkey facts here"):
        self.reply = reply
        self.calls = []

    def raw_complete(self, messages, max_tokens=1024, timeout=60.0):
        self.calls.append(messages)
        return self.reply


def make_ctx(*, network=True, llm=None):
    return SimpleNamespace(
        network_on=lambda: network,
        llm=llm,
    )


def parse(s: str) -> dict:
    return json.loads(s)


# --------------------------------------------------------------------------- #
# registration / discovery
# --------------------------------------------------------------------------- #

def test_registers_both_tools():
    discover_builtin_tools()
    names = registry.get_all_tool_names()
    assert "web_search" in names
    assert "web_extract" in names


def test_schemas_match_hermes_shape():
    assert web.WEB_SEARCH_SCHEMA["parameters"]["required"] == ["query"]
    limit = web.WEB_SEARCH_SCHEMA["parameters"]["properties"]["limit"]
    assert limit["minimum"] == 1 and limit["maximum"] == 100 and limit["default"] == 5
    assert web.WEB_EXTRACT_SCHEMA["parameters"]["properties"]["urls"]["maxItems"] == 5
    assert web.WEB_EXTRACT_SCHEMA["parameters"]["required"] == ["urls"]


# --------------------------------------------------------------------------- #
# network gating
# --------------------------------------------------------------------------- #

def test_search_blocked_when_network_off(monkeypatch):
    monkeypatch.setenv("LUNAMOTH_SEARXNG_URL", "https://searx.example")
    out = parse(web.web_search({"query": "hi"}, make_ctx(network=False)))
    assert "error" in out and "Network is off" in out["error"]


def test_extract_blocked_when_network_off():
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx(network=False)))
    assert "error" in out and "Network is off" in out["error"]


# --------------------------------------------------------------------------- #
# backend resolution
# --------------------------------------------------------------------------- #

def _clear_backend_env(monkeypatch):
    for k in ("LUNAMOTH_WEB_SEARCH_BACKEND", "LUNAMOTH_SEARXNG_URL", "SEARXNG_URL",
              "LUNAMOTH_SERPER_API_KEY", "SERPER_API_KEY",
              "LUNAMOTH_BRAVE_API_KEY", "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_no_backend_configured_falls_back_to_duckduckgo(monkeypatch):
    # No key configured → keyless DuckDuckGo is the default; the tool is always
    # available (check_fn True) and search works without any provider env.
    _clear_backend_env(monkeypatch)
    assert web.check_web_api_key() is True
    assert web._resolve_search_backend() == "duckduckgo"
    ddg_html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">'
        'Example A</a><a class="result__snippet">the first result</a>'
    )
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (200, ddg_html.encode(), "text/html"))
    out = parse(web.web_search({"query": "hi"}, make_ctx()))
    assert out["success"] is True
    hit = out["data"]["web"][0]
    assert hit["url"] == "https://example.com/a" and hit["title"] == "Example A"


def test_ddg_challenge_is_visible_error_not_empty_success(monkeypatch):
    # The core fix: a 202 anti-bot challenge (zero parseable results) must surface
    # as a visible error, NOT a misleading {success:true, web:[]} — that empty
    # 'success' was read by charas as "the tools are down" (operator: 你的tools挂了).
    _clear_backend_env(monkeypatch)
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (202, b"<html>challenge</html>", "text/html"))
    out = parse(web.web_search({"query": "hi"}, make_ctx()))
    assert "error" in out
    assert "duckduckgo" in out["error"].lower() or "search backend" in out["error"].lower()


def test_ddg_genuine_no_results_is_honest_empty(monkeypatch):
    # A real zero-hit page (carries a no-results marker) is honestly empty success.
    _clear_backend_env(monkeypatch)
    monkeypatch.setattr(web, "_http_get",
                        lambda *a, **k: (200, b'<div class="no-results">No results.</div>', "text/html"))
    out = parse(web.web_search({"query": "zxqw"}, make_ctx()))
    assert out["success"] is True and out["data"]["web"] == []


def test_ddg_unparseable_200_is_error(monkeypatch):
    # 200 but neither results nor a no-results marker → page shape changed/challenged.
    _clear_backend_env(monkeypatch)
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (200, b"<html><body>?</body></html>", "text/html"))
    out = parse(web.web_search({"query": "hi"}, make_ctx()))
    assert "error" in out


def test_ddg_falls_back_to_lite_endpoint(monkeypatch):
    # html endpoint challenged (202) → the lite endpoint's result-link markup parses.
    _clear_backend_env(monkeypatch)
    lite = ('<a class="result-link" href="https://example.org/x">Lite Hit</a>'
            '<td class="result-snippet">snip</td>')

    def fake_get(url, headers=None, timeout=0):
        if "lite.duckduckgo" in url:
            return (200, lite.encode(), "text/html")
        return (202, b"challenge", "text/html")

    monkeypatch.setattr(web, "_http_get", fake_get)
    out = parse(web.web_search({"query": "hi"}, make_ctx()))
    assert out["success"] is True
    assert out["data"]["web"][0]["url"] == "https://example.org/x"


def test_check_fn_true_when_searxng_set(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("LUNAMOTH_SEARXNG_URL", "https://searx.example")
    assert web.check_web_api_key() is True
    assert web._resolve_search_backend() == "searxng"


# --------------------------------------------------------------------------- #
# web_search backends (mock HTTP)
# --------------------------------------------------------------------------- #

def test_search_searxng(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("LUNAMOTH_SEARXNG_URL", "https://searx.example")
    payload = {"results": [
        {"title": "T1", "url": "https://a.com", "content": "desc1"},
        {"title": "T2", "url": "https://b.com", "content": "desc2"},
    ]}

    def fake_get(url, headers=None, timeout=web._HTTP_TIMEOUT):
        assert "format=json" in url
        return 200, json.dumps(payload).encode(), "application/json"

    monkeypatch.setattr(web, "_http_get", fake_get)
    out = parse(web.web_search({"query": "x", "limit": 5}, make_ctx()))
    assert out["success"] is True
    web_results = out["data"]["web"]
    assert len(web_results) == 2
    assert web_results[0] == {"title": "T1", "url": "https://a.com", "description": "desc1", "position": 1}
    assert web_results[1]["position"] == 2


def test_search_limit_clamped(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("LUNAMOTH_SEARXNG_URL", "https://searx.example")
    results = [{"title": f"T{i}", "url": f"https://{i}.com", "content": "d"} for i in range(50)]
    monkeypatch.setattr(web, "_http_get",
                        lambda url, headers=None, timeout=web._HTTP_TIMEOUT:
                        (200, json.dumps({"results": results}).encode(), "application/json"))
    # limit 99999 -> clamped to 100, but searxng returned 50
    out = parse(web.web_search({"query": "x", "limit": 99999}, make_ctx()))
    assert len(out["data"]["web"]) == 50
    # limit 0 -> clamped to 1
    out2 = parse(web.web_search({"query": "x", "limit": 0}, make_ctx()))
    assert len(out2["data"]["web"]) == 1


def test_search_serper(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "key123")
    payload = {"organic": [{"title": "S", "link": "https://s.com", "snippet": "snip", "position": 1}]}

    class FakeResp:
        def __init__(self):
            self._b = json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    out = parse(web.web_search({"query": "x"}, make_ctx()))
    assert out["data"]["web"][0]["url"] == "https://s.com"
    assert out["data"]["web"][0]["description"] == "snip"


def test_search_brave(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "bkey")
    payload = {"web": {"results": [{"title": "B", "url": "https://br.com", "description": "bd"}]}}
    monkeypatch.setattr(web, "_http_get",
                        lambda url, headers=None, timeout=web._HTTP_TIMEOUT:
                        (200, json.dumps(payload).encode(), "application/json"))
    out = parse(web.web_search({"query": "x"}, make_ctx()))
    assert out["data"]["web"][0] == {"title": "B", "url": "https://br.com", "description": "bd", "position": 1}


def test_search_http_error_surfaces(monkeypatch):
    import urllib.error
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("LUNAMOTH_SEARXNG_URL", "https://searx.example")

    def boom(url, headers=None, timeout=web._HTTP_TIMEOUT):
        raise urllib.error.HTTPError(url, 503, "down", {}, None)

    monkeypatch.setattr(web, "_http_get", boom)
    out = parse(web.web_search({"query": "x"}, make_ctx()))
    assert "error" in out and "503" in out["error"]


# --------------------------------------------------------------------------- #
# secret-in-URL + SSRF guards
# --------------------------------------------------------------------------- #

def test_secret_in_url_blocked(monkeypatch):
    # never reaches the network: _http_get must NOT be called
    called = {"n": 0}
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    bad = "https://evil.com/x?token=sk-abcdefghij1234567890"
    res = web._extract_one(bad, make_ctx())
    assert res["blocked_by_policy"] is True
    assert "API key or token" in res["error"]
    assert called["n"] == 0


def test_ssrf_blocked_localhost(monkeypatch):
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    res = web._extract_one("http://127.0.0.1/admin", make_ctx())
    assert res["blocked_by_policy"] is True
    assert "private/internal" in res["error"] or "unsupported scheme" in res["error"]


def test_ssrf_blocks_metadata_ip():
    assert _url_safety.is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_ssrf_allows_public_dns():
    # 8.8.8.8 is public; is_safe_url resolves the literal IP directly
    assert _url_safety.is_safe_url("https://8.8.8.8/") is True


def test_ssrf_rejects_non_http_scheme():
    assert _url_safety.is_safe_url("file:///etc/passwd") is False
    assert _url_safety.is_safe_url("ftp://example.com/x") is False


def test_contains_secret_decoded():
    assert _url_safety.contains_secret("https://x.com/?k=%73k-abcdefghij1234567890") is True
    assert _url_safety.contains_secret("https://x.com/page") is False


# --------------------------------------------------------------------------- #
# web_extract content paths
# --------------------------------------------------------------------------- #

def _html(body: str, title: str = "Title") -> bytes:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


def test_extract_short_html_returns_raw(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)
    monkeypatch.setattr(web, "_http_get",
                        lambda *a, **k: (200, _html("<p>Hello world</p>", "My Page"), "text/html"))
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx()))
    r = out["results"][0]
    assert r["title"] == "My Page"
    assert "Hello world" in r["content"]
    assert r["error"] is None


def test_extract_large_html_summarized_when_llm(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)
    big = "<p>" + ("data point. " * 1000) + "</p>"  # > 5000 chars
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (200, _html(big), "text/html"))
    llm = FakeLLM("## summary\nshort")
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx(llm=llm)))
    r = out["results"][0]
    assert r["content"] == "## summary\nshort"
    assert len(llm.calls) == 1


def test_extract_large_html_truncates_without_llm(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)
    big = "<p>" + ("word " * 3000) + "</p>"
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (200, _html(big), "text/html"))
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx(llm=None)))
    r = out["results"][0]
    assert "Content truncated" in r["content"]
    assert len(r["content"]) <= web.MAX_OUTPUT_SIZE + 300


def test_summary_output_hard_capped(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)
    big = "<p>" + ("z " * 4000) + "</p>"
    monkeypatch.setattr(web, "_http_get", lambda *a, **k: (200, _html(big), "text/html"))
    llm = FakeLLM("Q" * 9000)  # over the 5000 cap
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx(llm=llm)))
    content = out["results"][0]["content"]
    assert "summary truncated for context management" in content
    assert len(content) <= web.MAX_OUTPUT_SIZE + 100


def test_extract_over_2M_refused(monkeypatch):
    huge = "x" * (web.MAX_CONTENT_SIZE + 10)
    res = web._process_content(huge, url="https://x.com", title="", ctx=make_ctx(llm=FakeLLM()))
    assert "Content too large to process" in res


def test_extract_all_failed_returns_error(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)

    def boom(*a, **k):
        raise ConnectionError("nope")

    monkeypatch.setattr(web, "_http_get", boom)
    out = parse(web.web_extract({"urls": ["https://example.com"]}, make_ctx()))
    assert "error" in out
    assert "inaccessible" in out["error"]


def test_extract_blocked_url_among_good(monkeypatch):
    monkeypatch.setattr(web, "_http_get",
                        lambda *a, **k: (200, _html("<p>ok content here</p>", "Good"), "text/html"))
    monkeypatch.setattr(web, "is_safe_url", lambda u: "good.com" in u)
    out = parse(web.web_extract(
        {"urls": ["http://127.0.0.1/x", "https://good.com/p"]}, make_ctx()))
    results = out["results"]
    assert results[0]["blocked_by_policy"] is True
    assert results[1]["content"]


def test_extract_empty_urls():
    out = parse(web.web_extract({"urls": []}, make_ctx()))
    assert "error" in out


def test_extract_slices_to_5(monkeypatch):
    monkeypatch.setattr(web, "is_safe_url", lambda u: True)
    monkeypatch.setattr(web, "_http_get",
                        lambda *a, **k: (200, _html("<p>ok ok ok</p>"), "text/html"))
    urls = [f"https://e{i}.com" for i in range(8)]
    out = parse(web.web_extract({"urls": urls}, make_ctx()))
    assert len(out["results"]) == 5


# --------------------------------------------------------------------------- #
# html → markdown helper
# --------------------------------------------------------------------------- #

def test_html_to_markdown_strips_scripts():
    body = _html("<script>evil()</script><p>Keep this</p><style>x{}</style>", "Pg")
    text, title = web._html_to_markdown(body)
    assert "evil" not in text
    assert "Keep this" in text
    assert title == "Pg"
