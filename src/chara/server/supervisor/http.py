"""The static HTTP front: SPA shell, auth gate, /asset + /home + /rpc + /upload.

A threaded ``http.server`` handler. The auth gate (Host allowlist + token
cookie/query + optional password login) and the two narrow file-serving lanes
(/asset, /chara/<name>/home) live here; the JSON-RPC and upload bodies are
handed to the hub dispatcher. WebSocket / PTY routing lives on the Supervisor
coordinator (core.py).
"""
from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from ...session import sessions as S
from .. import authpw as AUTHPW
from .. import hub as H
from .. import netsec as N
from .paths import UPLOAD_MAX, WEB_DIR

if TYPE_CHECKING:
    from .core import Supervisor

_log = logging.getLogger("chara.server.supervisor")

# Static files that may be served BEFORE auth — the SPA shell + its hashed JS/CSS
# bundle. They carry no secrets (the token arrives in the URL hash, never baked
# into the bundle) and must load so the page can run the `?token=` handshake.
# Everything else (/asset, /rpc, /upload, the data the app fetches) is gated.
_PREAUTH_EXACT = frozenset({"/", "/index.html", "/favicon.ico", "/authinfo", "/login"})
_PREAUTH_PREFIXES = ("/assets/",)


def _is_preauth_path(path: str) -> bool:
    return path in _PREAUTH_EXACT or any(path.startswith(p) for p in _PREAUTH_PREFIXES)


class WebHandler(http.server.SimpleHTTPRequestHandler):
    token = ""
    supervisor: "Supervisor | None" = None
    # Set per-server by start_http (see the `type(...)` subclass there).
    allow_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
    wildcard_bind: bool = False
    secure_cookie: bool = False  # add Secure to the auth cookie (https / proxy)
    # OPTIONAL password login for a public bind (alternative to the token URL).
    # `pw_record` is the stored PBKDF2 record (hash+salt, never plaintext) — set
    # ONLY for a non-loopback bind with a configured/generated password; None
    # keeps login inert (the local app never sees a login screen). `pw_limiter`
    # throttles failed POST /login per client IP.
    pw_record: dict | None = None
    pw_limiter: Any | None = None
    login_fail_delay: float = 1.0  # fixed delay on a wrong password (anti-brute-force)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        _log.debug("http: " + fmt, *args)

    # ---- request gating (Host allowlist + token cookie/query) ---------------

    def _host_ok(self) -> bool:
        """Reject Host headers outside the allowlist (anti DNS-rebinding)."""
        return N.host_allowed(
            self.headers.get("Host", ""), self.allow_hosts, wildcard_bind=self.wildcard_bind
        )

    def _is_secure_request(self) -> bool:
        """True when the cookie should carry Secure: a TLS reverse proxy in
        front (X-Forwarded-Proto: https) or a direct https connection."""
        if self.secure_cookie:
            return True
        return self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"

    def _auth_ok(self, url) -> bool:
        """Dual-read auth: a valid ``?token=`` query OR the auth cookie.

        On a valid ``?token=`` handshake we mint the SameSite cookie so later
        ``<img src>``/``/asset`` requests (which can't send the query) pass on
        the cookie alone. The actual Set-Cookie is emitted by send_auth_cookie,
        called from the response path once a 200 is going out.

        No token configured (an explicit token-less ``start_http(token="")`` —
        never the desktop, which always auto-generates one) means auth is
        DISABLED: the route is open, as it was before the gate existed."""
        if not self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        if N.request_authed(url.query, cookie, self.token):
            # Mint/refresh the cookie when the token arrived via the query.
            if N.token_from_query(url.query):
                self._pending_set_cookie = N.auth_cookie_header(
                    self.token, secure=self._is_secure_request()
                )
            return True
        return False

    # Raster only: no svg/html — an attacker-controlled SVG served same-origin
    # with image/svg+xml would be a stored-XSS vector. Card avatars use inline
    # SVG via a separate sanitized data-URI path, never this route.
    _ASSET_MIME = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
                   ".jpeg": "image/jpeg", ".gif": "image/gif"}
    # Browser-NATIVE media the webui can preview inline (audio/video/pdf): served
    # INLINE (no forced download) so <audio>/<video>/<iframe> play in place, from a
    # session's workspace/assets only. These types render but CANNOT run script in
    # the page's origin (unlike html/svg/js, which stay on the forced-download lane —
    # serving those inline same-origin would be a stored-XSS vector). A `download`
    # attribute on the client still downloads the same URL when the user wants the file.
    _ASSET_INLINE_MIME = {
        ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".ogv": "video/ogg", ".pdf": "application/pdf",
    }

    def end_headers(self) -> None:
        if not getattr(self, "_skip_no_store", False):
            self.send_header("Cache-Control", "no-store")
        pending = getattr(self, "_pending_set_cookie", "")
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_set_cookie = ""
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _client_ip(self) -> str:
        """Per-client key for the login rate limit. Trust ``X-Forwarded-For`` ONLY
        when the socket peer is loopback (the reverse proxy runs on the same host).
        A direct connection to the published port could spoof XFF to mint a fresh
        rate-limit bucket per request and defeat the per-IP brute-force throttle —
        so for a non-loopback peer use the real peer IP and ignore XFF."""
        try:
            peer = self.client_address[0]
        except (AttributeError, IndexError):
            return "?"
        if N.is_loopback_host(peer):
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return peer

    def _handle_login(self) -> None:
        """POST /login {password} → mint the SAME auth cookie on success.

        Pre-auth (the login form must reach it without a token). Defends with a
        per-IP token-bucket throttle + a fixed delay on every failure. Mints the
        token cookie via netsec.auth_cookie_header — the SAME cookie the
        ?token= handshake sets — so the rest of the app is unchanged after login.
        """
        if self.pw_record is None:
            # Login not enabled (loopback / no password). Behave as if absent.
            self.send_error(404)
            return
        ip = self._client_ip()
        limiter = self.pw_limiter
        if limiter is not None and not limiter.allow(ip):
            self._send_json(429, {"error": "too many attempts"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(max(0, length)) if length > 0 else b""
        try:
            req = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            req = {}
        password = str(req.get("password") or "")
        if AUTHPW.verify_password(self.pw_record, password):
            cookie = N.auth_cookie_header(self.token, secure=self._is_secure_request())
            if not cookie:  # an unsafe --token can't ride a cookie; refuse cleanly
                self._send_json(500, {"error": "token not cookie-safe"})
                return
            self._pending_set_cookie = cookie
            self.send_response(204)
            self.end_headers()
            return
        time.sleep(self.login_fail_delay)  # fixed delay slows brute-force
        self._send_json(401, {"error": "invalid password"})

    def _card_roots(self) -> list[Path]:
        return [H.bundled_cards_dir().resolve(), H.user_cards_dir().resolve()]

    def _session_roots(self) -> list[Path]:
        # The session ROOT tree — used ONLY to locate raster card-art sidecars
        # (a living chara's frozen card copies sprite.png etc. to its session root)
        # and to flag a file as session-volatile for caching. NON-image files are
        # NOT served from here (see _readable_session_roots) — that is what keeps
        # config.json / session.json / transcript.db off this route.
        try:
            return [m.root.resolve() for m in S.list_sessions()]
        except Exception:  # noqa: BLE001 - serving must not depend on session health
            return []

    def _readable_session_roots(self) -> list[Path]:
        """The ONLY session subtrees /asset may hand out NON-image files from: the
        chara's workspace and the read-only assets shelf (send_file docs / works /
        reference material). The session ROOT — which holds config.json (the provider
        api_key!), session.json, env_status.json and transcript.db — is deliberately
        absent, so those secrets are unreachable via this route."""
        out: list[Path] = []
        try:
            for m in S.list_sessions():
                sb = m.sandbox_dir.resolve()
                out += [(sb / "workspace").resolve(), (sb / "assets").resolve()]
        except Exception:  # noqa: BLE001
            return []
        return out

    # Never serve these by name, wherever they sit — session secrets / state / logs.
    _ASSET_DENY_NAMES = frozenset({"config.json", "session.json", "env_status.json", "presence.json"})

    def _serve_asset(self, url) -> None:
        """Serve a file by absolute path. Two narrow lanes:

        - Raster card art (png/webp/jpg/gif): from the card decks (cacheable) or a
          session's art sidecars / workspace images (no-store). Images carry no
          secrets, so the broad session tree is acceptable here.
        - Non-image (a doc the chara sent with send_file): FORCED DOWNLOAD, and ONLY
          from a session's sandbox/workspace or sandbox/assets. The session ROOT is
          NOT a read root for non-images — that is what keeps config.json (the
          provider api_key) and transcript.db off this route. A hard name denylist
          backs it up.
        """
        raw = (parse_qs(url.query).get("p") or [""])[0]
        try:
            target = Path(unquote(raw)).resolve()
        except Exception:  # noqa: BLE001
            self.send_error(404); return
        # Hard denylist: session secrets / state / transcript / pidfiles, by name,
        # regardless of where they resolve — defense in depth behind the lane split.
        if (target.name in self._ASSET_DENY_NAMES
                or target.name.startswith("transcript.db")
                or target.suffix in (".pid", ".log")):
            self.send_error(404); return
        if not target.is_file():
            self.send_error(404); return

        def under(roots: list[Path]) -> bool:
            return any(target == r or r in target.parents for r in roots)

        suffix = target.suffix.lower()
        mime = self._ASSET_MIME.get(suffix)
        inline_mime = self._ASSET_INLINE_MIME.get(suffix)
        if mime is not None:
            # Raster image lane: card decks (cacheable) or session art/images (no-store).
            session_roots = self._session_roots()
            if not under(self._card_roots() + session_roots):
                self.send_error(404); return
            in_session = under(session_roots)
            disposition = None
            cache = "no-store" if in_session else "public, max-age=86400"
        elif inline_mime is not None and under(self._readable_session_roots()):
            # Inline browser-native media lane (audio/video/pdf): ONLY a session's
            # workspace/assets, served inline so the webui can play/preview it in place.
            mime = inline_mime
            disposition = None
            cache = "no-store"
        else:
            # Everything else: ONLY a session's workspace/assets, forced download.
            if not under(self._readable_session_roots()):
                self.send_error(404); return
            mime = "application/octet-stream"
            safe = target.name.replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")
            disposition = f'attachment; filename="{safe}"'
            cache = "no-store"
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404); return
        self._skip_no_store = True
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # nosniff: the inline media lane (audio/video/pdf) serves chara-written files
        # same-origin, so never let a browser sniff a mislabeled payload into HTML/JS —
        # matches the hardened /home route. Declared media types aren't sniffed anyway,
        # but this removes the dependence on per-browser heuristics.
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _serve_home(self, url) -> None:
        """Serve a chara's personal website from its workspace/home/ tree, read-only.

        Route: /chara/<name>/home[/<rel...>] (rel defaults to index.html). The
        whole tree under home/ is served (HTML/CSS/JS/media) so a chara can build a
        real, linkable site. Two security properties:
          • Confined to that ONE home/ dir (path-traversal resolved + checked), so
            config.json (the api key), the transcript, other sessions stay off it.
          • Rendered in a sandboxed iframe (allow-scripts, NO allow-same-origin →
            opaque origin) and served with a CSP that blocks connect-src and
            form-action: chara-authored JS cannot reach /rpc or read the app token.
        """
        segs = url.path.split("/")  # ['', 'chara', '<name>', 'home', ...]
        if len(segs) < 4 or segs[3] != "home":
            self.send_error(404); return
        meta = S.load_session(unquote(segs[2]))
        if meta is None:
            self.send_error(404); return
        home = (meta.sandbox_dir / "workspace" / "home").resolve()
        rel = unquote("/".join(segs[4:])) or "index.html"
        try:
            target = (home / rel).resolve()
        except Exception:  # noqa: BLE001
            self.send_error(404); return
        if target.is_dir():
            target = (target / "index.html").resolve()
        # Confine to the home/ subtree; deny session secrets by name (defense in depth).
        if not (target == home or home in target.parents):
            self.send_error(404); return
        if (target.name in self._ASSET_DENY_NAMES or target.suffix in (".pid", ".log")
                or not target.is_file()):
            self.send_error(404); return
        import mimetypes
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404); return
        self._skip_no_store = True
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; "
            "connect-src 'none'; form-action 'none'; frame-ancestors 'self'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._skip_no_store = False
        self._pending_set_cookie = ""
        if not self._host_ok():
            self.send_error(403, "host not allowed")
            return
        url = urlsplit(self.path)
        # The SPA shell + its hashed bundle load pre-auth (they carry no secrets
        # and must run to perform the ?token= handshake). Everything else —
        # /asset and any other path — requires the token (query) or auth cookie.
        if not _is_preauth_path(url.path) and not self._auth_ok(url):
            self.send_error(401, "authentication required")
            return
        if url.path == "/authinfo":
            # Pre-auth probe: should the client show a login form? No secrets —
            # just a boolean. True only when a password is configured (a public
            # bind) AND the request is NOT already authenticated. The "already
            # authed" clause is essential: after a successful /login the user
            # carries the lm_auth cookie but still has no #token=, so without it
            # /authinfo would keep saying login:true and the Gate would loop
            # forever (the user could never enter). Loopback ⇒ pw_record=None ⇒ false.
            cookie = self.headers.get("Cookie", "")
            already = N.request_authed(url.query, cookie, self.token)
            self._send_json(200, {"login": self.pw_record is not None and not already})
            return
        if url.path == "/auth":
            # Boot handshake: the SPA loads its token from the URL hash (never sent
            # to the server), so the shell GET mints no cookie. The client calls
            # GET /auth?token=… once at boot — _auth_ok above validated it and
            # queued the Set-Cookie, so subsequent tokenless <img>/asset requests
            # authenticate via the SameSite cookie. 204, no body.
            self.send_response(204)
            self.end_headers()
            return
        if url.path == "/asset":
            self._serve_asset(url)
            return
        segs = url.path.split("/")
        if len(segs) >= 4 and segs[1] == "chara" and segs[3] == "home":
            self._serve_home(url)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._skip_no_store = False  # never inherit an asset GET's cache flag (keep-alive safety)
        self._pending_set_cookie = ""
        if not self._host_ok():
            self.send_error(403, "host not allowed")
            return
        url = urlsplit(self.path)
        if url.path == "/login":
            # Pre-auth: the login form is exactly how an un-tokened client gets
            # authed. Inert (404) unless a password is configured (public bind).
            self._handle_login()
            return
        if not self._auth_ok(url):
            self.send_error(403)
            return
        if url.path == "/rpc":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(max(0, length))
            try:
                req = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                payload = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                dispatcher = H.HubDispatcher(lambda frame: True, supervisor=self.supervisor)
                payload = dispatcher.dispatch(req) or {"jsonrpc": "2.0", "id": None, "result": None}
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if url.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        name = Path(self.headers.get("X-Filename") or "card.json").name
        if length <= 0 or length > UPLOAD_MAX or Path(name).suffix.lower() not in (".json", ".png"):
            self.send_error(400, "expected a .json or .png card under 8 MB")
            return
        body = self.rfile.read(length)
        # A .json that parses as a standalone world book is stored aside and
        # reported as kind="world" so the deck can offer "merge into card X".
        payload = json.dumps(H.store_upload(name, body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_http(
    host: str,
    port: int,
    token: str,
    supervisor: "Supervisor | None" = None,
    *,
    allow_hosts: frozenset[str] | None = None,
    secure_cookie: bool = False,
    pw_record: dict | None = None,
    pw_limiter: Any | None = None,
) -> http.server.ThreadingHTTPServer:
    attrs = {
        "token": token,
        "supervisor": supervisor,
        "allow_hosts": allow_hosts if allow_hosts is not None else N.allowed_hosts(host),
        "wildcard_bind": N.is_wildcard_host(host),
        "secure_cookie": bool(secure_cookie),
        "pw_record": pw_record,
        # One limiter per server (shared across the handler threads); only made
        # when login is enabled (a public bind with a password).
        "pw_limiter": pw_limiter if pw_limiter is not None
        else (AUTHPW.LoginRateLimiter() if pw_record is not None else None),
    }
    handler = type("Handler", (WebHandler,), attrs)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="desktop-http", daemon=True)
    thread.start()
    return server


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _reachable_ips(host: str) -> list[str]:
    """Best-effort list of addresses a remote browser could use to reach a
    non-loopback bind. For a wildcard bind, enumerate the host's own IPs; for a
    specific host, just that host."""
    if not N.is_wildcard_host(host):
        return [host]
    ips: list[str] = []
    import contextlib
    with contextlib.suppress(Exception):
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127.") and ip != "::1":
                ips.append(ip)
    return ips or [host]
