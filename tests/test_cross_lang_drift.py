"""Drift guards for constants duplicated across modules / languages.

Some contracts are necessarily mirrored in two places (a Python source of truth +
a TypeScript copy the SPA needs, or a redaction list + the exfil guard). These are
exactly the things that rot silently when one side is edited. Each test below pins
a mirror so a future edit to one side fails CI until the other is updated.
"""
from __future__ import annotations

import re
from pathlib import Path

from chara.core import redact
from chara.protocol import media
from chara.tools.builtin import _url_safety

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
    from chara import config
    assert redact._PREFIX_PATTERNS is config.SECRET_PREFIX_PATTERNS
    assert _url_safety._PREFIX_PATTERNS is config.SECRET_PREFIX_PATTERNS


def test_disk_log_scrubber_covers_every_shared_prefix():
    # obs/log.py can't import core (enforced leaf boundary) so it builds its OWN regex —
    # but from the SAME config list, so a sample token of every prefix is scrubbed before
    # it hits chara.log / errors.log on disk.
    from chara import config
    from chara.obs import log
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


def test_provider_presets_in_sync_py_ts():
    # The webui provider list (id · label · base_url) lives in Python (settings.PROVIDER_
    # PRESETS, also driving the migration's labels) + a TS mirror the SPA renders. A drift
    # would mislabel a migrated key or point a provider row at the wrong endpoint.
    from chara.session import settings as S
    ts = (_REPO / "apps/web/src/lib/providers.ts").read_text(encoding="utf-8")
    # anchor on the declaration, not the first mention (the file's doc-comment also
    # names PROVIDER_PRESETS) — otherwise a comment edit could skew the parse.
    body = ts[ts.index("export const PROVIDER_PRESETS"):]

    def field(row: str, name: str) -> str:
        m = re.search(rf'\b{name}:\s*"([^"]*)"', row)
        return m.group(1) if m else ""

    ts_presets = [{"provider": field(r, "provider"), "label": field(r, "label"), "base_url": field(r, "base_url")}
                  for r in re.findall(r"\{([^}]*)\}", body) if field(r, "provider")]
    py_presets = [{"provider": p["provider"], "label": p["label"], "base_url": p["base_url"]}
                  for p in S.PROVIDER_PRESETS]
    assert ts_presets == py_presets, (
        "lib/providers.ts drifted from settings.PROVIDER_PRESETS — update both (same order)"
    )


def test_live_providers_match_across_core_and_session():
    # The LIVE (OpenAI-compatible HTTP) provider set is mirrored in core/llm + session/
    # settings (core keeps its session imports function-local, so it can't import the
    # constant at module scope). Adding a provider to one but not the other makes the
    # client gate and Settings.is_live() disagree.
    from chara.core import llm
    from chara.session import settings as S
    assert llm.LIVE_PROVIDERS == S.LIVE_PROVIDERS, "core/llm LIVE_PROVIDERS drifted from session/settings"


def test_reasoning_efforts_in_sync_py_ts():
    # The reasoning-effort enum: a Python validation tuple (settings.REASONING_EFFORTS)
    # + the ONE TS copy in lib/providers.ts (imported by BOTH ModelPane and ChatPanel,
    # so there's no per-component literal to drift). A new tier added to one side drifts
    # the picker vs the validator.
    from chara.session import settings as S
    ts = (_REPO / "apps/web/src/lib/providers.ts").read_text(encoding="utf-8")
    m = re.search(r"REASONING_EFFORTS\s*=\s*\[(.*?)\]", ts, re.DOTALL)
    assert m, "could not find REASONING_EFFORTS in lib/providers.ts"
    ts_efforts = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert ts_efforts == S.REASONING_EFFORTS, "lib/providers.ts REASONING_EFFORTS drifted from settings.REASONING_EFFORTS"


def test_life_state_words_in_sync_py_ts():
    # The life.state vocabulary: Python emits it (supervisor/lifestate.py LifeState("...")),
    # and status.ts lifeText branches on each word as a string literal. A Python rename
    # would make the SPA silently render no status word for that state.
    py = (_REPO / "src/chara/server/supervisor/lifestate.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'LifeState\("([a-z_]+)"', py))
    ts = (_REPO / "apps/web/src/lib/status.ts").read_text(encoding="utf-8")
    handled = set(re.findall(r'life\.state === "([a-z_]+)"', ts))
    missing = emitted - handled
    assert emitted and not missing, (
        f"status.ts lifeText doesn't handle life.state(s) {sorted(missing)} that Python emits"
    )


def test_browser_shares_the_one_exfil_regex():
    # The browser navigation guard must be the SAME compiled regex as the fetch/URL
    # guard (deduped), so the two can't drift apart.
    from chara.tools.builtin import browser
    assert browser._PREFIX_RE is _url_safety._PREFIX_RE
