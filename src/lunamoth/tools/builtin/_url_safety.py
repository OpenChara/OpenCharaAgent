"""URL safety + secret-in-URL guards for the web tools — a self-contained port
of hermes-agent ``tools/url_safety.py`` + the secret-prefix screen from
``agent/redact.py`` (the parts ``web_extract`` depends on).

Two guards, both standalone (no hermes imports):

- ``contains_secret(url)`` — True when a URL appears to embed an API key/token
  (so we refuse to send it on the wire). Ported from redact ``_PREFIX_PATTERNS``.
- ``is_safe_url(url)`` — SSRF check: resolves the hostname and blocks
  private/internal/metadata targets. Fails CLOSED on DNS errors and parse
  failures. Cloud-metadata endpoints are blocked unconditionally.

This module has a leading underscore so the registry's AST discovery never
imports it as a tool module (it carries no top-level ``registry.register``).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import unquote, urlparse

logger = logging.getLogger("lunamoth.tools.web.url_safety")

# ---------------------------------------------------------------------------
# Secret-in-URL screen (hermes agent/redact.py:70-193, the prefix alternation)
# ---------------------------------------------------------------------------

# The key-prefix list is single-sourced in config (so the at-rest redactor + disk log
# scrubber can't drift from this wire/URL guard). This module is the historical home of
# the alternation; the list now lives in config.SECRET_PREFIX_PATTERNS.
from ...config import SECRET_PREFIX_PATTERNS as _PREFIX_PATTERNS

_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def contains_secret(url: str) -> bool:
    """True when *url* (raw OR URL-decoded) appears to embed an API key/token.

    Mirrors hermes web_extract's pre-fetch screen: both the raw string and the
    percent-decoded string are scanned, so a key hidden behind ``%73k-`` is
    still caught."""
    if not isinstance(url, str) or not url:
        return False
    if _PREFIX_RE.search(url):
        return True
    try:
        decoded = unquote(url)
    except Exception:  # noqa: BLE001
        return False
    if decoded != url and _PREFIX_RE.search(decoded):
        return True
    return False


# ---------------------------------------------------------------------------
# SSRF guard (hermes tools/url_safety.py) — standalone subset, fail-closed.
# ---------------------------------------------------------------------------

_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),      # AWS ECS task metadata
    ipaddress.ip_address("169.254.169.253"),    # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),      # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),    # Alibaba Cloud metadata
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
)

# 100.64.0.0/10 (CGNAT / RFC 6598) is not flagged by ipaddress.is_private.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip) -> bool:
    """True if *ip* is a private/internal address we must not reach."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded = ip.ipv4_mapped
        return (embedded.is_private or embedded.is_loopback or
                embedded.is_link_local or embedded.is_reserved or
                embedded.is_multicast or embedded.is_unspecified or
                embedded in _CGNAT_NETWORK)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def is_safe_url(url: str) -> bool:
    """True when *url*'s target is a public address (not private/internal).

    Resolves the hostname and checks each answer against private ranges and the
    always-blocked cloud-metadata set. Fails CLOSED: an unsupported scheme,
    a DNS failure, or any unexpected error blocks the request."""
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            logger.warning("Blocked — unsupported URL scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked — internal hostname: %s", hostname)
            return False

        try:
            addr_info = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("Blocked — DNS resolution failed for: %s", hostname)
            return False

        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip in _ALWAYS_BLOCKED_IPS or any(
                ip in net for net in _ALWAYS_BLOCKED_NETWORKS
            ):
                logger.warning("Blocked — cloud metadata address: %s -> %s", hostname, ip_str)
                return False
            if _is_blocked_ip(ip):
                logger.warning("Blocked — private/internal address: %s -> %s", hostname, ip_str)
                return False
        return True
    except Exception as exc:  # noqa: BLE001 — fail closed on parse edge cases
        logger.warning("Blocked — URL safety check error for %s: %s", url, exc)
        return False
