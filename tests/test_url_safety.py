"""Exercise the REAL SSRF classifier (tools/builtin/_url_safety.is_safe_url).

The browser tests monkeypatch is_safe_url to a constant, so its plumbing is
verified but its *brain* never is. This test drives the actual classifier with
IP-literal URLs (no DNS, so no network flakiness) to prove the private/metadata/
loopback blocking and the fail-closed scheme handling actually work.
"""
from __future__ import annotations

import pytest

from chara.tools.builtin._url_safety import is_safe_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",      # AWS/GCP/Azure metadata
    "http://169.254.170.2/",                          # AWS ECS task metadata
    "http://100.100.100.200/",                        # Alibaba Cloud metadata
    "http://[::ffff:169.254.169.254]/",               # IPv4-mapped metadata
    "http://127.0.0.1:6180/rpc",                      # loopback (the hub itself!)
    "http://10.0.0.5/",                               # RFC1918 private
    "http://192.168.1.1/",                            # RFC1918 private
    "http://172.16.0.1/",                             # RFC1918 private
    "http://100.64.0.1/",                             # CGNAT / RFC 6598
    "http://[::1]/",                                  # IPv6 loopback
    "http://169.254.1.1/",                            # link-local
])
def test_blocks_private_and_metadata_targets(url):
    assert is_safe_url(url) is False, f"SSRF guard let through {url}"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",        # non-http scheme — fail closed
    "ftp://example.com/",        # non-http scheme
    "gopher://127.0.0.1/",       # classic SSRF scheme
    "not a url at all",          # parse failure — fail closed
    "",                          # empty — fail closed
])
def test_fails_closed_on_bad_scheme_or_parse(url):
    assert is_safe_url(url) is False


def test_allows_a_public_ip_literal():
    # A public IP literal needs no DNS; the classifier should permit it.
    assert is_safe_url("http://93.184.216.34/") is True   # documentation/public range
    assert is_safe_url("https://1.1.1.1/") is True
