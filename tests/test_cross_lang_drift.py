"""Drift guards for constants duplicated across modules / languages.

Some contracts are necessarily mirrored in two places (a Python source of truth +
a TypeScript copy the SPA needs, or a redaction list + the exfil guard). These are
exactly the things that rot silently when one side is edited. Each test below pins
a mirror so a future edit to one side fails CI until the other is updated.
"""
from __future__ import annotations

import re
from pathlib import Path

from lunamoth.core import redact
from lunamoth.protocol import media
from lunamoth.tools.builtin import _url_safety

_REPO = Path(__file__).resolve().parents[1]


def _ts_string_set(ts_src: str, const_name: str) -> set[str]:
    """Extract the `".x", ".y", …` members of an `export const NAME = new Set([ … ])`."""
    m = re.search(rf"{const_name}[^=]*=\s*new Set\(\[(.*?)\]\)", ts_src, re.DOTALL)
    assert m, f"could not find {const_name} in media.ts"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_media_delivery_exts_in_sync_py_ts():
    ts = (_REPO / "apps/web/src/lib/media.ts").read_text(encoding="utf-8")
    ts_delivery = _ts_string_set(ts, "MEDIA_DELIVERY_EXTS")
    ts_image = _ts_string_set(ts, "IMAGE_EXTS")
    assert ts_delivery == set(media.MEDIA_DELIVERY_EXTS), (
        "media.ts MEDIA_DELIVERY_EXTS drifted from protocol/media.py — update both"
    )
    assert ts_image == set(media.IMAGE_EXTS), (
        "media.ts IMAGE_EXTS drifted from protocol/media.py IMAGE_EXTS — update both"
    )


def test_all_redactors_share_the_one_prefix_list():
    # The key-prefix list is single-sourced in config.SECRET_PREFIX_PATTERNS; the at-rest
    # redactor (transcripts/request log) and the URL/browser exfil guard both build from
    # it, so a prefix can't be masked in one but leak through the other.
    from lunamoth import config
    assert redact._PREFIX_PATTERNS is config.SECRET_PREFIX_PATTERNS
    assert _url_safety._PREFIX_PATTERNS is config.SECRET_PREFIX_PATTERNS


def test_disk_log_scrubber_covers_every_shared_prefix():
    # obs/log.py can't import core (enforced leaf boundary) so it builds its OWN regex —
    # but from the SAME config list, so a sample token of every prefix is scrubbed before
    # it hits lunamoth.log / errors.log on disk.
    from lunamoth import config
    from lunamoth.obs import log
    samples = {
        "sk-": "sk-abcDEF0123456789", "ghp_": "ghp_abcDEF0123456789",
        "AKIA": "AKIAABCDEFGHIJKLMNOP", "xai-": "xai-" + "a" * 32,
        "r8_": "r8_abcDEF0123456789", "npm_": "npm_abcDEF0123456789",
        "mem0_": "mem0_abcDEF0123456789", "AIza": "AIza" + "b" * 32,
    }
    for token in samples.values():
        assert log._REDACT.sub("·", token) != token, f"{token!r} not scrubbed by obs/log"
    # and the list it builds from is the shared one
    assert all(p in log._REDACT.pattern for p in config.SECRET_PREFIX_PATTERNS)


def test_browser_shares_the_one_exfil_regex():
    # The browser navigation guard must be the SAME compiled regex as the fetch/URL
    # guard (deduped), so the two can't drift apart.
    from lunamoth.tools.builtin import browser
    assert browser._PREFIX_RE is _url_safety._PREFIX_RE
