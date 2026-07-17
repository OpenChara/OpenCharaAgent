"""Plan §4b — OPTIONAL public-bind password login (alternative auth path).

Covers: the PBKDF2 store round-trip (never persists plaintext); ``ensure_password``
precedence; the live ``POST /login`` flow (correct → 204 + cookie, wrong → 401 +
delay, throttle → 429); the pre-auth ``GET /authinfo``; the TOKEN path is
UNCHANGED; and a loopback bind keeps login INERT (no password generated, no login
screen) — the constraint that the local app never sees a login screen.
"""
from __future__ import annotations

import http.client
import json
import time

import pytest

from chara.server import authpw as A
from chara.server import supervisor as SV


# ---- password store: PBKDF2 round-trip, never plaintext ---------------------

def test_hash_verify_roundtrip_and_no_plaintext():
    rec = A.hash_password("Hunter2Hunter2", iterations=1000)  # fast for the test
    assert rec["algo"] == "pbkdf2_sha256"
    assert A.verify_password(rec, "Hunter2Hunter2")
    assert not A.verify_password(rec, "wrong-password")
    assert not A.verify_password(rec, "")
    assert not A.verify_password(None, "Hunter2Hunter2")
    # The record carries ONLY salt+hash+iters — never the plaintext.
    blob = json.dumps(rec)
    assert "Hunter2Hunter2" not in blob
    assert set(rec) == {"algo", "iters", "salt", "hash"}


def test_hash_refuses_empty_or_short():
    with pytest.raises(ValueError):
        A.hash_password("")
    with pytest.raises(ValueError):
        A.hash_password("short")  # < 8 chars


def test_store_save_load_roundtrip_and_corrupt(tmp_path):
    p = tmp_path / "auth.json"
    rec = A.hash_password("CorrectHorse9", iterations=1000)
    A.save_record(rec, p)
    assert "CorrectHorse9" not in p.read_text()  # plaintext never on disk
    loaded = A.load_record(p)
    assert loaded == rec
    assert A.verify_password(loaded, "CorrectHorse9")
    # corrupt / missing → None
    p.write_text("{not json")
    assert A.load_record(p) is None
    assert A.load_record(tmp_path / "nope.json") is None


def test_ensure_password_precedence(tmp_path):
    p = tmp_path / "auth.json"
    # 1. env password wins, stored hashed, nothing generated
    enabled, gen = A.ensure_password(env_password="MyEnvPass77", path=p)
    assert enabled and gen is None
    assert A.verify_password(A.load_record(p), "MyEnvPass77")
    # 2. existing record kept (no env) — no regeneration
    enabled, gen = A.ensure_password(env_password=None, path=p)
    assert enabled and gen is None
    assert A.verify_password(A.load_record(p), "MyEnvPass77")
    # 3. fresh generation when neither env nor a record exists
    p2 = tmp_path / "fresh.json"
    enabled, gen = A.ensure_password(env_password=None, path=p2)
    assert enabled and gen and len(gen) == 24
    assert A.verify_password(A.load_record(p2), gen)
    # an empty/weak env password is refused
    with pytest.raises(ValueError):
        A.ensure_password(env_password="short", path=tmp_path / "x.json")


def test_auth_store_is_private_from_creation(tmp_path):
    """The password record must be 0600 with NO group/world bits — and private
    from creation (no write-then-chmod race), even if a stale tmp pre-existed."""
    import os
    import stat

    p = tmp_path / "auth.json"
    # a stale tmp left world-readable by a prior crash must not leak through
    stale = p.with_suffix(".json.tmp")
    stale.write_text("{}", encoding="utf-8")
    os.chmod(stale, 0o644)
    A.save_record({"algo": "pbkdf2_sha256", "iters": 600000, "salt": "aa", "hash": "bb"}, path=p)
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert not (mode & 0o077)  # no group/other access
    # round-trips as a real record, hash-only (no plaintext anywhere)
    assert A.load_record(p) is not None
    assert "pbkdf2" in p.read_text(encoding="utf-8")


def test_client_ip_trusts_xff_only_from_a_loopback_peer():
    """Regression: the login rate-limit key must NOT trust X-Forwarded-For from a
    non-loopback peer — a direct hit to the published port could spoof it to mint a
    fresh bucket per request and defeat the per-IP brute-force throttle. XFF is
    honored ONLY when the socket peer is loopback (the same-host reverse proxy)."""
    from chara.server.supervisor import WebHandler

    class _H:
        def __init__(self, peer, xff):
            self.client_address = (peer, 12345)
            self.headers = {"X-Forwarded-For": xff} if xff else {}

    # loopback peer (the proxy on the same host) → trust XFF (the real client IP)
    assert WebHandler._client_ip(_H("127.0.0.1", "9.9.9.9, 1.1.1.1")) == "9.9.9.9"
    assert WebHandler._client_ip(_H("::1", "9.9.9.9")) == "9.9.9.9"
    # a remote peer hitting the port directly → IGNORE the spoofable XFF, key by peer
    assert WebHandler._client_ip(_H("203.0.113.7", "9.9.9.9")) == "203.0.113.7"
    # no XFF → always the peer IP
    assert WebHandler._client_ip(_H("203.0.113.7", "")) == "203.0.113.7"


def test_rate_limiter_throttles_then_refills():
    rl = A.LoginRateLimiter(capacity=3, window=60.0)
    assert all(rl.allow("1.2.3.4") for _ in range(3))  # burst of 3
    assert not rl.allow("1.2.3.4")  # bucket empty → throttled
    assert rl.allow("9.9.9.9")  # a different IP has its own bucket


# ---- live HTTP: POST /login + GET /authinfo ---------------------------------

@pytest.fixture()
def login_server():
    """A public-bind server with a configured password (fast PBKDF2 for tests)."""
    rec = A.hash_password("PublicPass42!", iterations=1000)
    port = SV.free_port()
    srv = SV.start_http(
        "127.0.0.1", port, token="sekret", supervisor=None,
        pw_record=rec, pw_limiter=A.LoginRateLimiter(capacity=3, window=60.0),
    )
    # Shrink the fixed failure delay so the wrong-password test stays quick but
    # still asserts a non-zero delay is applied.
    srv.RequestHandlerClass.login_fail_delay = 0.05  # type: ignore[attr-defined]
    try:
        yield port
    finally:
        srv.shutdown()


def _post(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json", **(headers or {})}
    conn.request("POST", path, body=body, headers=h)
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()), resp.read())
    conn.close()
    return out


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()), resp.read())
    conn.close()
    return out


def test_authinfo_reports_login_enabled(login_server):
    # Fresh visitor (no cookie) → the client should show the login form.
    status, _h, body = _get(login_server, "/authinfo")
    assert status == 200
    assert json.loads(body) == {"login": True}
    # ALREADY-authed (valid lm_auth cookie, e.g. just after /login) → login:false.
    # Without this, a password user who logged in (cookie set, no #token=) would
    # see /authinfo:true on reload and the Gate would loop forever (HIGH bug).
    status, _h, body = _get(login_server, "/authinfo", headers={"Cookie": "lm_auth=sekret"})
    assert json.loads(body) == {"login": False}
    # a valid ?token= likewise counts as authed
    status, _h, body = _get(login_server, "/authinfo?token=sekret")
    assert json.loads(body) == {"login": False}


def test_login_correct_mints_cookie(login_server):
    status, headers, _b = _post(login_server, "/login", json.dumps({"password": "PublicPass42!"}))
    assert status == 204
    sc = headers.get("Set-Cookie", "")
    assert "lm_auth=sekret" in sc and "HttpOnly" in sc and "SameSite=Strict" in sc
    # The minted cookie now authenticates a gated route with NO token in the URL.
    status, _h, _b = _get(login_server, "/asset?p=/etc/hosts", headers={"Cookie": "lm_auth=sekret"})
    assert status == 404  # past auth (404 = path not an allowed asset, not 401)


def test_login_wrong_returns_401_with_delay(login_server):
    t0 = time.monotonic()
    status, headers, _b = _post(login_server, "/login", json.dumps({"password": "nope"}))
    assert status == 401
    assert time.monotonic() - t0 >= 0.05  # fixed-delay anti-brute-force applied
    assert "lm_auth" not in headers.get("Set-Cookie", "")


def test_login_rate_limit_kicks_in(login_server):
    # capacity=3: three attempts allowed, the fourth is throttled (429).
    for _ in range(3):
        st, _h, _b = _post(login_server, "/login", json.dumps({"password": "nope"}))
        assert st == 401
    st, _h, _b = _post(login_server, "/login", json.dumps({"password": "nope"}))
    assert st == 429


def test_token_path_still_authenticates_unchanged(login_server):
    # The existing token→cookie handshake is byte-for-byte unchanged: a ?token=
    # query still mints the cookie on a gated route, login present or not.
    status, headers, _b = _get(login_server, "/asset?token=sekret&p=/etc/hosts")
    assert status == 404  # auth PASSED (path just isn't an allowed asset)
    assert "lm_auth=sekret" in headers.get("Set-Cookie", "")


# ---- the constraint: a loopback bind keeps login INERT ----------------------

@pytest.fixture()
def loopback_server():
    """A normal loopback bind — NO password, the local-app default path."""
    port = SV.free_port()
    srv = SV.start_http("127.0.0.1", port, token="sekret", supervisor=None)
    try:
        yield port
    finally:
        srv.shutdown()


def test_loopback_authinfo_says_login_false(loopback_server):
    status, _h, body = _get(loopback_server, "/authinfo")
    assert status == 200
    assert json.loads(body) == {"login": False}  # local app shows NO login screen


def test_loopback_login_post_is_inert_404(loopback_server):
    # With no password configured, POST /login is inert (404) — never an auth path.
    status, _h, _b = _post(loopback_server, "/login", json.dumps({"password": "anything"}))
    assert status == 404


def test_loopback_bind_generates_no_password(tmp_path, monkeypatch):
    """A loopback bind must never generate/persist a password — the cmd_desktop
    path is what proves the local app stays inert."""
    import argparse

    from chara.front import cli as CLI

    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CHARA_PASSWORD", raising=False)
    # Short-circuit before actually serving: reuse-our-daemon path returns 0.
    fake = {"pid": 1, "http_port": 0, "ws_port": 0, "token": "t", "path": "x"}
    # Make serve_desktop a no-op so we only exercise the password-resolution branch.
    captured = {}
    monkeypatch.setattr(
        "chara.server.desktop.serve_desktop",
        lambda *a, **k: captured.setdefault("pw", k.get("pw_record")) or 0,
    )
    ns = argparse.Namespace(
        host="127.0.0.1", allow_host="", port=0, ws_port=0, token="",
        no_open=True, daemon=False, debug=False,
    )
    rc = CLI.cmd_desktop(ns)
    assert rc == 0
    # No password resolved for loopback, and auth.json was never written.
    assert captured.get("pw") is None
    assert not (tmp_path / "home" / "auth.json").exists()
    _ = fake
