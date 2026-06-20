"""Track D: auth on GET/asset/WS, the post-handshake cookie, the Host/Origin
allowlist, the --host refusal, and port-in-use attribution vs daemon reuse."""
from __future__ import annotations

import http.client

import pytest

from lunamoth.server import netsec as N
from lunamoth.server import supervisor as SV


# ---- netsec unit coverage ---------------------------------------------------

def test_loopback_and_wildcard_classification():
    assert N.is_loopback_host("127.0.0.1") and N.is_loopback_host("localhost")
    assert N.is_loopback_host("::1")
    assert not N.is_loopback_host("0.0.0.0") and not N.is_loopback_host("10.0.0.4")
    assert N.is_wildcard_host("0.0.0.0") and N.is_wildcard_host("::")
    assert not N.is_wildcard_host("127.0.0.1")


def test_host_allowlist_blocks_rebinding_names():
    allow = N.allowed_hosts("192.168.1.5", ["proxy.example"])
    assert N.host_allowed("192.168.1.5:8080", allow, wildcard_bind=False)
    assert N.host_allowed("proxy.example", allow, wildcard_bind=False)
    assert N.host_allowed("localhost:1234", allow, wildcard_bind=False)
    assert not N.host_allowed("evil.attacker.test", allow, wildcard_bind=False)
    # empty Host (HTTP/1.0) is permitted — rebinding needs a controlled name
    assert N.host_allowed("", allow, wildcard_bind=False)
    # under a wildcard bind a bare IP literal is fine, a foreign NAME is not
    assert N.host_allowed("203.0.113.9", allow, wildcard_bind=True)
    assert not N.host_allowed("evil.test", allow, wildcard_bind=True)


def test_origin_allowlist_blocks_cross_site_but_allows_missing():
    allow = N.allowed_hosts("127.0.0.1")
    assert N.origin_allowed("", allow, wildcard_bind=False)        # native client
    assert N.origin_allowed("null", allow, wildcard_bind=False)
    assert N.origin_allowed("http://localhost:9000", allow, wildcard_bind=False)
    assert not N.origin_allowed("http://evil.test", allow, wildcard_bind=False)


def test_cookie_roundtrip_and_secure_flag():
    val = N.auth_cookie_header("tok123", secure=True)
    assert "lm_auth=tok123" in val and "HttpOnly" in val and "SameSite=Strict" in val
    assert "Secure" in val
    assert "Secure" not in N.auth_cookie_header("tok123", secure=False)
    assert N.token_from_cookie("a=1; lm_auth=tok123; b=2") == "tok123"
    assert N.token_from_cookie("a=1") == ""


def test_request_authed_dual_read():
    assert N.request_authed("token=abc", "", "abc")
    assert N.request_authed("", "lm_auth=abc", "abc")
    assert not N.request_authed("token=bad", "lm_auth=bad", "abc")
    assert not N.request_authed("", "", "abc")
    assert not N.request_authed("token=abc", "", "")  # no expected ⇒ never authed


def test_ws_handshake_authenticates_via_cookie():
    """Regression: a password-login user reaches the WS with NO ?token= but the
    lm_auth cookie on the handshake — the WS gate must accept it (else the live,
    WS-driven UI never connects). _ws_cookie extracts the header; the gate then
    dual-reads via request_authed exactly as the HTTP gate does."""
    from lunamoth.server.supervisor import Supervisor

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    class _WS:
        def __init__(self, headers):
            self.request = _Req(headers)

    # _ws_cookie doesn't touch self → call unbound with None.
    cookie = Supervisor._ws_cookie(None, _WS({"Cookie": "a=1; lm_auth=tok9; b=2"}))
    assert cookie == "a=1; lm_auth=tok9; b=2"
    # the WS gate's actual decision (query empty, cookie present):
    assert N.request_authed("", cookie, "tok9")          # cookie authenticates
    assert not N.request_authed("", cookie, "other")     # wrong expected ⇒ no
    # missing/empty headers degrade to "" (then request_authed fails closed):
    assert Supervisor._ws_cookie(None, _WS({})) == ""
    assert Supervisor._ws_cookie(None, object()) == ""
    assert not N.request_authed("", "", "tok9")


# ---- live HTTP server: auth gate + cookie path ------------------------------

@pytest.fixture()
def _webui_stub():
    """The SPA shell is served from SV.WEB_DIR (front/webui), a BUILD ARTIFACT
    that's gitignored — present locally after `npm run build`, ABSENT in a clean
    checkout / CI backend job. Lay down a minimal index.html if it's missing so
    the static-serving + auth-gate tests are hermetic; clean up only what we made."""
    index = SV.WEB_DIR / "index.html"
    created_file = not index.exists()
    created_dir = not SV.WEB_DIR.exists()
    if created_file:
        SV.WEB_DIR.mkdir(parents=True, exist_ok=True)
        index.write_text("<!doctype html><title>stub</title>", encoding="utf-8")
    try:
        yield
    finally:
        if created_file:
            index.unlink(missing_ok=True)
        if created_dir:
            try:
                SV.WEB_DIR.rmdir()
            except OSError:
                pass


@pytest.fixture()
def http_server(_webui_stub):
    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="sekret", supervisor=None)
    try:
        yield port
    finally:
        srv.shutdown()


def _raw_get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    out = (resp.status, dict(resp.getheaders()), body)
    conn.close()
    return out


def test_spa_shell_loads_pre_auth(http_server):
    # The SPA shell + hashed bundle load without a token so the page can run the
    # ?token= handshake. (index.html is served from front/webui.)
    status, _h, _b = _raw_get(http_server, "/")
    assert status == 200
    status, _h, _b = _raw_get(http_server, "/index.html")
    assert status == 200


def test_asset_requires_auth(http_server):
    status, _h, _b = _raw_get(http_server, "/asset?p=/etc/hosts")
    assert status == 401  # no token, no cookie


def test_token_query_sets_cookie_and_cookie_then_authenticates(http_server):
    # A ?token= handshake on any gated route mints the SameSite auth cookie.
    status, headers, _b = _raw_get(http_server, "/asset?token=sekret&p=/etc/hosts")
    # 404 (the path isn't an allowed asset) but auth PASSED — and a cookie was set.
    assert status == 404
    set_cookie = headers.get("Set-Cookie", "")
    assert "lm_auth=sekret" in set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    # Now an <img>-style request with ONLY the cookie (no token in the URL) authenticates.
    status, _h, _b = _raw_get(http_server, "/asset?p=/etc/hosts", headers={"Cookie": "lm_auth=sekret"})
    assert status == 404  # past auth (404 = path not an allowed asset, not 401)
    # A wrong cookie is rejected.
    status, _h, _b = _raw_get(http_server, "/asset?p=/etc/hosts", headers={"Cookie": "lm_auth=nope"})
    assert status == 401


def test_post_rpc_requires_auth(http_server):
    conn = http.client.HTTPConnection("127.0.0.1", http_server, timeout=5)
    conn.request("POST", "/rpc", body=b"{}", headers={"Content-Type": "application/json"})
    assert conn.getresponse().status == 403
    conn.close()


def test_host_header_allowlist_blocks_foreign_name(http_server):
    # A loopback bind's allow set does not include a foreign DNS name.
    status, _h, _b = _raw_get(http_server, "/", headers={"Host": "evil.attacker.test"})
    assert status == 403
    # The bound loopback host is fine.
    status, _h, _b = _raw_get(http_server, "/", headers={"Host": "127.0.0.1"})
    assert status == 200


# ---- WS: auth + Origin allowlist (anti-CSWSH) -------------------------------

def _run(coro):
    import asyncio
    return asyncio.run(asyncio.wait_for(coro, timeout=15.0))


def test_ws_hub_authed_with_good_token_and_origin():
    import websockets

    async def scenario():
        port = SV.free_port()
        sup = SV.Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            url = f"ws://127.0.0.1:{port}/hub?token=sesame"
            async with websockets.connect(url, origin="http://localhost") as ws:
                hello = await __import__("asyncio").wait_for(ws.recv(), timeout=5.0)
                assert "hello" in hello
        await sup.shutdown()

    _run(scenario())


def test_ws_rejected_on_bad_origin_even_with_valid_token():
    """CSWSH: a foreign Origin must be rejected (4403) before auth even with a
    valid token — a leaked token from a malicious page must not open the WS."""
    import websockets

    async def scenario():
        port = SV.free_port()
        sup = SV.Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            url = f"ws://127.0.0.1:{port}/hub?token=sesame"
            async with websockets.connect(url, origin="http://evil.attacker.test") as ws:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await __import__("asyncio").wait_for(ws.recv(), timeout=5.0)
                assert ws.close_code == 4403
        await sup.shutdown()

    _run(scenario())


def test_ws_rejected_on_bad_token():
    import websockets

    async def scenario():
        port = SV.free_port()
        sup = SV.Supervisor("127.0.0.1", 0, port, "sesame")
        async with websockets.serve(sup._ws_entry, "127.0.0.1", port):
            url = f"ws://127.0.0.1:{port}/hub?token=WRONG"
            async with websockets.connect(url, origin="http://localhost") as ws:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await __import__("asyncio").wait_for(ws.recv(), timeout=5.0)
                assert ws.close_code == 4401
        await sup.shutdown()

    _run(scenario())


# ---- cmd_desktop: --host refusal + port handling (D2) -----------------------

def _desktop_args(**over):
    import argparse
    ns = argparse.Namespace(
        host="127.0.0.1", allow_host="", port=0, ws_port=0, token="",
        no_open=True, daemon=False, debug=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_wildcard_bind_without_token_is_refused(capsys):
    from lunamoth.front.cli import cmd_desktop

    rc = cmd_desktop(_desktop_args(host="0.0.0.0", token=""))
    assert rc == 2
    assert "refusing to bind 0.0.0.0" in capsys.readouterr().err


def test_foreign_port_in_use_fails_with_attribution(capsys):
    import socket as _socket

    from lunamoth.front.cli import cmd_desktop

    # Hold a port so the requested HTTP port is taken by a FOREIGN listener.
    held = _socket.socket()
    held.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        rc = cmd_desktop(_desktop_args(port=port))
        assert rc == 1
        err = capsys.readouterr().err
        assert f"HTTP port {port} held by" in err
    finally:
        held.close()


def test_taken_port_that_is_our_daemon_reuses_not_respawns(capsys, monkeypatch):
    from lunamoth.front import cli as CLI

    fake = {"pid": 4242, "http_port": 51234, "ws_port": 51235, "token": "t", "path": "/x/daemon.json"}
    monkeypatch.setattr("lunamoth.server.supervisor.read_daemon_json", lambda: fake)
    monkeypatch.setattr("lunamoth.server.supervisor.daemon_alive", lambda data=None: True)
    # serve_desktop must NOT be called — reuse short-circuits before it.
    monkeypatch.setattr(
        "lunamoth.server.desktop.serve_desktop",
        lambda *a, **k: pytest.fail("should not spawn a second supervisor"),
    )
    rc = CLI.cmd_desktop(_desktop_args(port=51234))
    assert rc == 0
    assert "already running" in capsys.readouterr().out
