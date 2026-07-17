"""Network-security helpers for the desktop supervisor's HTTP + WS surface.

Three concerns, kept stdlib-only and out of supervisor.py so they can be unit
tested in isolation (ported in shape from AstrBot's dashboard server):

- **Host / Origin allow-listing** — anti DNS-rebinding (a malicious page on
  ``evil.test`` resolving to our bound IP) and anti cross-site WebSocket
  hijacking (CSWSH: a foreign ``Origin`` opening our WS even with a leaked
  token). The default allow set is the bound host plus loopback; it is
  configurable so a reverse-proxy deployment can name its public host.
- **Loopback classification** — used to decide whether a bind needs the token
  gate enforced (non-loopback) and whether the auth cookie may drop ``Secure``.
- **Port attribution** — when the HTTP port is taken by a FOREIGN process we
  surface ``port N held by <proc> pid <x>`` instead of a raw ``OSError``
  traceback (AstrBot ``server.py:517-554``). Uses psutil when present and
  degrades to a best-effort ``lsof`` probe, never fabricating a result.
"""
from __future__ import annotations

import hmac
import http.cookies
import ipaddress
import re
import socket
import subprocess
from urllib.parse import parse_qs, urlsplit

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})

# The auth cookie set after a successful ``?token=`` handshake. Lets `<img src>`
# / `/asset` / `/rpc` requests authenticate WITHOUT the token in every URL —
# the token would otherwise leak into proxy logs and browser history (§7).
AUTH_COOKIE = "lm_auth"


def is_loopback_host(host: str) -> bool:
    """True when *host* is a loopback name/literal (no network exposure)."""
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_wildcard_host(host: str) -> bool:
    """True when *host* binds every interface (``0.0.0.0`` / ``::``)."""
    h = (host or "").strip().strip("[]").lower()
    return h in ("0.0.0.0", "::")


def _host_only(value: str) -> str:
    """Strip a port (and brackets) from a ``Host:`` header or ``Origin`` URL."""
    v = (value or "").strip()
    if not v:
        return ""
    # Origin is a URL (scheme://host[:port]); Host is host[:port].
    if "://" in v:
        v = urlsplit(v).netloc
    if v.startswith("["):  # bracketed IPv6 literal: [::1]:8080
        end = v.find("]")
        return v[1:end].lower() if end != -1 else v.lower()
    if v.count(":") == 1:  # host:port (a lone IPv6 has many colons, no port here)
        v = v.split(":", 1)[0]
    return v.lower()


def allowed_hosts(bound_host: str, extra: list[str] | None = None) -> frozenset[str]:
    """The Host/Origin allow set: loopback names + the bound host + any extras.

    A wildcard bind (``0.0.0.0``) contributes no usable name on its own — the
    operator must name the reachable host(s) via *extra* (or accept loopback +
    whatever the request's own Host claims, which we additionally allow only
    when it resolves to the bound interface — see :func:`host_allowed`)."""
    out = {"localhost", "127.0.0.1", "::1"}
    bh = _host_only(bound_host)
    if bh and not is_wildcard_host(bh):
        out.add(bh)
    for e in extra or []:
        eh = _host_only(e)
        if eh:
            out.add(eh)
    return frozenset(out)


def host_allowed(host_header: str, allow: frozenset[str], *, wildcard_bind: bool) -> bool:
    """Validate an HTTP ``Host`` header against *allow*.

    Empty Host (HTTP/1.0) is permitted — a rebinding attack needs a controlled
    DNS name, which always rides in the Host header. Under a wildcard bind we
    also accept a Host that is a bare IP literal (an operator hitting the box by
    address); a NAMED host under wildcard must be allow-listed via config."""
    name = _host_only(host_header)
    if not name:
        return True
    if name in allow:
        return True
    if wildcard_bind:
        try:
            ipaddress.ip_address(name)
            return True  # bare IP literal under 0.0.0.0 — not a rebinding name
        except ValueError:
            return False
    return False


def origin_allowed(origin: str, allow: frozenset[str], *, wildcard_bind: bool) -> bool:
    """Validate a WS ``Origin`` against *allow* (anti-CSWSH).

    A missing Origin is allowed: native clients (the Electron shell, `chara`
    CLI tunnels, curl) send none, and the token already gates them. A PRESENT
    Origin must match the allow set — that is the browser cross-site case."""
    if not origin or origin == "null":
        return True
    name = _host_only(origin)
    if not name:
        return True
    if name in allow:
        return True
    if wildcard_bind:
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False
    return False


# ---- request authentication (token query + cookie dual-read) ----------------

def token_from_query(query: str) -> str:
    """Extract a ``?token=`` (or legacy ``?auth=``) value from a query string."""
    qs = parse_qs(query or "")
    vals = qs.get("token") or qs.get("auth") or []
    return str(vals[0]) if vals else ""


def token_from_cookie(cookie_header: str) -> str:
    """Extract the auth token from the ``Cookie:`` header, if present."""
    if not cookie_header:
        return ""
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(cookie_header)
    except http.cookies.CookieError:
        return ""
    morsel = jar.get(AUTH_COOKIE)
    return morsel.value if morsel else ""


def request_token(query: str, cookie_header: str) -> str:
    """The token a request presents: the ``?token=`` query wins, else the cookie.

    Mirrors AstrBot's header+cookie dual-read (``server.py:476-487``) — a header
    isn't usable here because ``<img src>``/``/asset`` URLs the serve-child builds
    can't carry one, so the cookie is the header's stand-in."""
    return token_from_query(query) or token_from_cookie(cookie_header)


def request_authed(query: str, cookie_header: str, expected: str) -> bool:
    """Constant-time check that a request carries the expected token."""
    if not expected:
        return False
    tok = request_token(query, cookie_header)
    return bool(tok) and hmac.compare_digest(tok, expected)


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


def auth_cookie_header(token: str, *, secure: bool) -> str:
    """Build the ``Set-Cookie`` value for the post-handshake auth cookie.

    ``SameSite=Strict; HttpOnly`` always; ``Secure`` added when the connection
    is https / behind a TLS proxy (§7) so the cookie never rides plain http.

    Returns "" for a token with any character outside the cookie-safe set
    (``[A-Za-z0-9._-]``) — our tokens are ``secrets.token_urlsafe`` so this only
    bites a hand-crafted ``--token``; refusing to emit the cookie closes a
    Set-Cookie header-injection vector (CR/LF/`;`) at no cost to real tokens."""
    if not _SAFE_TOKEN.match(token or ""):
        return ""
    parts = [
        f"{AUTH_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=86400",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# ---- port-conflict attribution ---------------------------------------------

def port_in_use(host: str, port: int) -> bool:
    """True when *port* on *host* refuses a fresh bind (someone holds it)."""
    probe = "127.0.0.1" if is_wildcard_host(host) else (host or "127.0.0.1")
    for fam, addr in ((socket.AF_INET, probe), (socket.AF_INET6, probe)):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((addr, int(port)))
            return False
        except OSError:
            return True
        except (ValueError, socket.gaierror):
            continue
    return True


def describe_port_holder(port: int) -> str:
    """Best-effort ``<proc> pid <x>`` for whoever holds *port*.

    Prefers psutil (AstrBot's path); falls back to ``lsof`` on POSIX. Returns
    a plain "an unknown process" when neither can attribute it — never a fake.
    """
    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency
        psutil = None  # type: ignore
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                laddr = getattr(conn, "laddr", None)
                if laddr and getattr(laddr, "port", None) == int(port) and conn.pid:
                    try:
                        p = psutil.Process(conn.pid)
                        return f"{p.name()} pid {conn.pid}"
                    except Exception:  # noqa: BLE001
                        return f"pid {conn.pid}"
        except Exception:  # noqa: BLE001 - net_connections may need privileges
            pass
    # POSIX fallback: lsof -nP -iTCP:<port> -sTCP:LISTEN
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fcpn"],
            capture_output=True, text=True, timeout=3.0,
        ).stdout
        name = pid = ""
        for line in out.splitlines():
            if line.startswith("p"):
                pid = line[1:]
            elif line.startswith("c"):
                name = line[1:]
        if name and pid:
            return f"{name} pid {pid}"
        if pid:
            return f"pid {pid}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "an unknown process"
