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


def test_redact_prefixes_are_a_subset_of_the_exfil_guard():
    # Every secret SHAPE the redactor scrubs from text-at-rest must also be caught by
    # the URL/browser exfil guard — otherwise a key that's masked in logs could still
    # be walked out over the wire. The exfil list MAY be a strict superset (more
    # aggressive on the network boundary); it must never be missing a redact shape.
    missing = set(redact._PREFIX_PATTERNS) - set(_url_safety._PREFIX_PATTERNS)
    assert not missing, (
        "these redact prefixes are NOT in the exfil guard (_url_safety._PREFIX_PATTERNS) "
        f"— a scrubbed secret could still leave on the wire: {sorted(missing)}"
    )


def test_browser_shares_the_one_exfil_regex():
    # The browser navigation guard must be the SAME compiled regex as the fetch/URL
    # guard (deduped), so the two can't drift apart.
    from lunamoth.tools.builtin import browser
    assert browser._PREFIX_RE is _url_safety._PREFIX_RE
