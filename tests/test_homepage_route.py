"""The chara personal-website HTTP route: /chara/<name>/home/* serves a chara's
workspace/home/ tree read-only, confined, with a hardened CSP — and never leaks
the session secrets that sit just outside home/."""
from __future__ import annotations

import http.client

import pytest

from chara.server import supervisor as SV
from chara.session import sessions as S


@pytest.fixture()
def home_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    meta = S.create_session("webby", isolation="sandbox")
    home = meta.sandbox_dir / "workspace" / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "index.html").write_text("<h1>HELLO-FROM-HOME</h1>", encoding="utf-8")
    (home / "app.js").write_text("console.log('hi')", encoding="utf-8")
    # A secret one level up that MUST never be reachable through this route.
    (meta.root / "config.json").write_text('{"api_key":"SECRET-KEY-XYZ"}', encoding="utf-8")
    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="sekret", supervisor=None)
    try:
        yield port
    finally:
        srv.shutdown()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()), resp.read())
    conn.close()
    return out


def test_home_serves_index_and_assets_with_hardened_csp(home_server):
    status, headers, body = _get(home_server, "/chara/webby/home/index.html?token=sekret")
    assert status == 200
    assert b"HELLO-FROM-HOME" in body
    assert "text/html" in headers.get("Content-Type", "")
    csp = headers.get("Content-Security-Policy", "")
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "frame-ancestors 'self'" in csp

    # Bare /home resolves to index.html.
    status, _h, body = _get(home_server, "/chara/webby/home?token=sekret")
    assert status == 200 and b"HELLO-FROM-HOME" in body

    # A linked subresource (the chara's own JS) is served.
    status, _h, body = _get(home_server, "/chara/webby/home/app.js?token=sekret")
    assert status == 200 and b"console.log" in body


def test_home_route_confines_to_home_and_hides_secrets(home_server):
    # Path traversal to the session secret must 404, never leak the key.
    status, _h, body = _get(home_server, "/chara/webby/home/../../config.json?token=sekret")
    assert status == 404
    assert b"SECRET-KEY-XYZ" not in body
    # An unknown chara → 404.
    status, _h, _b = _get(home_server, "/chara/ghost/home/index.html?token=sekret")
    assert status == 404
    # A missing file under home → 404 (not a server error).
    status, _h, _b = _get(home_server, "/chara/webby/home/nope.html?token=sekret")
    assert status == 404


def test_home_route_requires_auth(home_server):
    # No token / cookie → 401 (the route sits behind the same gate as /asset).
    status, _h, _b = _get(home_server, "/chara/webby/home/index.html")
    assert status == 401


@pytest.fixture()
def asset_server(tmp_path, monkeypatch):
    """A session with workspace files, served behind the /asset route."""
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    meta = S.create_session("worky", isolation="sandbox")
    ws = meta.sandbox_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "song.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    (ws / "data.zip").write_bytes(b"PK\x03\x04zipbytes")
    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="sekret", supervisor=None)
    try:
        yield port, ws
    finally:
        srv.shutdown()


def _ci(headers):
    return {k.lower(): v for k, v in headers.items()}


def test_asset_serves_audio_inline_for_webui_preview(asset_server):
    import urllib.parse
    port, ws = asset_server
    p = urllib.parse.quote(str((ws / "song.wav").resolve()))
    status, headers, _b = _get(port, f"/asset?p={p}&token=sekret")
    assert status == 200
    h = _ci(headers)
    assert h.get("content-type") == "audio/wav"
    # INLINE (not forced download) so <audio> plays in place in the browser
    assert "attachment" not in (h.get("content-disposition") or "")


def test_asset_forces_download_for_non_media(asset_server):
    import urllib.parse
    port, ws = asset_server
    p = urllib.parse.quote(str((ws / "data.zip").resolve()))
    status, headers, _b = _get(port, f"/asset?p={p}&token=sekret")
    assert status == 200
    h = _ci(headers)
    # a non-media file stays on the forced-download lane (no inline same-origin XSS surface)
    assert "attachment" in (h.get("content-disposition") or "")
