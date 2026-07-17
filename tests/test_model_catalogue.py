"""The provider model catalogue is pulled live from /models, DISK-cached, and only
re-pulled when older than the refresh interval (default one day); offline it degrades
to the stale disk copy, then to a curated fallback — never raising, never empty for a
known provider."""
import json

import pytest

from chara.server import hub as H
from chara.server.hub import models as M


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    M._models_cache.clear()  # isolate the in-process memo between tests
    yield
    M._models_cache.clear()


def _payload(*ids):
    return {"data": [{"id": i, "name": i, "context_length": 1000,
                      "architecture": {"input_modalities": ["text"]},
                      "supported_parameters": ["tools"]} for i in ids]}


def test_fetches_then_serves_from_disk_within_interval(monkeypatch):
    calls = {"n": 0}

    def fake(url, api_key="", payload=None, timeout=20.0):
        calls["n"] += 1
        return _payload("a", "b")

    monkeypatch.setattr(H, "_http_json", fake)
    out = M._catalogue("https://x.test/v1", "k", refresh_seconds=1000)
    assert [m["id"] for m in out] == ["a", "b"] and calls["n"] == 1
    assert "https://x.test/v1" in json.loads(M._catalogue_cache_path().read_text(encoding="utf-8"))

    # memo cleared, but the disk entry is still fresh → no second fetch
    M._models_cache.clear()
    out2 = M._catalogue("https://x.test/v1", "k", refresh_seconds=1000)
    assert [m["id"] for m in out2] == ["a", "b"] and calls["n"] == 1


def test_offline_with_no_cache_uses_curated_fallback(monkeypatch):
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    out = M._catalogue("https://openrouter.ai/api/v1", "", refresh_seconds=1000)
    assert out and any("gpt-4o" in m["id"] for m in out)  # fallback, not an exception


def test_offline_prefers_stale_disk_over_fallback(monkeypatch):
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: _payload("cached-model"))
    M._catalogue("https://y.test/v1", "k", refresh_seconds=1000)  # seed the disk cache
    M._models_cache.clear()
    # endpoint down AND we force a re-pull (interval 0) → stale disk wins over fallback
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    out = M._catalogue("https://y.test/v1", "k", refresh_seconds=0.0)
    assert [m["id"] for m in out] == ["cached-model"]


def test_catalogue_meta_reports_source(monkeypatch):
    # live pull → fresh
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: _payload("a"))
    out, src = M._catalogue_meta("https://z.test/v1", "k", refresh_seconds=1000)
    assert [m["id"] for m in out] == ["a"] and src == "fresh"
    # offline + a stale disk copy → stale
    M._models_cache.clear()
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    out, src = M._catalogue_meta("https://z.test/v1", "k", refresh_seconds=0.0)
    assert [m["id"] for m in out] == ["a"] and src == "stale"
    # offline + nothing cached for a known provider → fallback
    out, src = M._catalogue_meta("https://openrouter.ai/api/v1", "", refresh_seconds=1000)
    assert out and src == "fallback"


def test_refresh_interval_default_and_override():
    assert M.refresh_interval_seconds() == 86_400        # default: one day
    H.save_defaults({"model_refresh_interval": "3600"})
    assert M.refresh_interval_seconds() == 3600
    H.save_defaults({"model_refresh_interval": "0"})      # 0/blank/invalid → default
    assert M.refresh_interval_seconds() == 86_400
    # a fat-fingered tiny value is FLOORED, never honored verbatim (no hot-loop re-pull)
    H.save_defaults({"model_refresh_interval": "5"})
    assert M.refresh_interval_seconds() == M._MIN_REFRESH_SECONDS
    # non-finite text → default, never inf/nan
    H.save_defaults({"model_refresh_interval": "inf"})
    assert M.refresh_interval_seconds() == 86_400


def test_disk_catalogue_caps_entries(monkeypatch):
    # More distinct base_urls than the cap → only the newest N persist on disk, so a
    # once-tried relay / typo'd endpoint can't pile up full /models payloads forever.
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: _payload("m"))
    clock = {"t": 1000.0}
    def tick():
        clock["t"] += 1.0  # strictly increasing fetched_at → deterministic eviction order
        return clock["t"]
    monkeypatch.setattr(M.time, "time", tick)
    n = M._DISK_CACHE_MAX_ENTRIES + 5
    for i in range(n):
        M._models_cache.clear()  # force each base_url through the disk-write path
        M._catalogue(f"https://e{i}.test/v1", "k", refresh_seconds=1000)
    disk = json.loads(M._catalogue_cache_path().read_text(encoding="utf-8"))
    assert len(disk) == M._DISK_CACHE_MAX_ENTRIES
    assert "https://e0.test/v1" not in disk            # earliest evicted
    assert f"https://e{n - 1}.test/v1" in disk          # newest kept
