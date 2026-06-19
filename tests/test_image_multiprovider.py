"""Multi-provider image generation — the catalogue, provider routing, per-provider
key resolution from the shared keyring, and each adapter's request/response shape.

No real network: the shared ``_request_json`` / ``download_bytes`` seams are mocked,
so each adapter is exercised against a synthetic provider response.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lunamoth.content import image_providers as ip
from lunamoth.tools.builtin import _image_gen as g


_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-IMAGE"


def _write_desktop(home: Path, **fields):
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(json.dumps(fields), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(h))
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_IMAGE_MODEL", raising=False)
    return h


# ---------------------------------------------------------------------------
# Catalogue + routing (pure)
# ---------------------------------------------------------------------------
def test_catalogue_lists_all_providers():
    cat = ip.catalogue({})
    ids = [c["id"] for c in cat]
    assert ids == ["volcano", "dashscope", "openai", "openrouter"]
    # every provider ships at least one selectable model
    assert all(c["models"] for c in cat)


def test_catalogue_merges_dynamic_models_for_flagged_provider():
    # OpenRouter is flagged dynamic: with a key, the injected fetcher's models are
    # grafted onto the curated picks (dedup by id, curated first).
    raw = {"keys": {"OR": {"provider": "openrouter", "api_key": "sk-or"}}}

    def fetch(pid, base, key):
        assert pid == "openrouter"
        return [{"id": "google/gemini-3-pro-image", "label": "Gemini 3 Pro"},
                {"id": "x-ai/grok-imagine-image-quality", "label": "dup ignored"}]

    cat = {c["id"]: c for c in ip.catalogue(raw, fetch)}
    or_ids = [m["id"] for m in cat["openrouter"]["models"]]
    assert or_ids[0] == "x-ai/grok-imagine-image-quality"      # curated stays first
    assert "google/gemini-3-pro-image" in or_ids                 # fetched grafted on
    assert or_ids.count("x-ai/grok-imagine-image-quality") == 1  # no dup
    # a provider without a key never triggers the fetch (stays curated-only)
    assert "doubao-seedream-4-0-250828" in [m["id"] for m in cat["volcano"]["models"]]


def test_resolve_provider_is_selection_only_no_inference():
    # the active provider is EXACTLY the selected one; never inferred from a model
    assert ip.resolve_provider("openrouter") == "openrouter"
    assert ip.resolve_provider("dashscope") == "dashscope"
    # unset or invalid → "" (caller surfaces a plain error, no fallback)
    assert ip.resolve_provider("") == ""
    assert ip.resolve_provider("nonsense") == ""


# ---------------------------------------------------------------------------
# Per-provider key resolution from the shared keyring
# ---------------------------------------------------------------------------
def test_key_matched_by_provider_id():
    raw = {"keys": {"Ali": {"provider": "dashscope", "api_key": "sk-ali"}}}
    assert ip.resolve_key(raw, "dashscope") == "sk-ali"
    assert ip.resolve_key(raw, "openai") == ""


def test_key_matched_by_base_url_host():
    # a custom openai_compatible entry pointed at OpenAI is matched by host
    raw = {"keys": {"My OpenAI": {"provider": "openai_compatible",
                                  "base_url": "https://api.openai.com/v1",
                                  "api_key": "sk-oai"}}}
    assert ip.resolve_key(raw, "openai") == "sk-oai"


def test_no_legacy_image_api_key_path():
    # the legacy single image_api_key field is NOT a key source any more — image
    # keys come ONLY from the unified provider keyring
    raw = {"image_api_key": "ark-legacy"}
    assert ip.resolve_key(raw, "volcano") == ""


def test_base_url_prefers_keyring_entry_then_default():
    raw = {"keys": {"relay": {"provider": "openai", "base_url": "https://relay.example/v1",
                              "api_key": "sk"}}}
    assert ip.resolve_base_url(raw, "openai") == "https://relay.example/v1"
    assert ip.resolve_base_url({}, "openai") == "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Adapter: OpenAI (b64_json and url shapes)
# ---------------------------------------------------------------------------
def test_openai_adapter_b64(home, monkeypatch):
    _write_desktop(home, image_provider="openai", image_model="gpt-image-1",
                   keys={"OpenAI": {"provider": "openai", "api_key": "sk-oai"}})
    b64 = base64.b64encode(_FAKE_PNG).decode()
    seen = {}

    def fake_request(url, *, data, headers, method, timeout, tries):
        seen["url"] = url
        seen["body"] = json.loads(data)
        return {"data": [{"b64_json": b64}]}

    monkeypatch.setattr(g, "_request_json", fake_request)
    out = g.generate_bytes("a fox", "2048x2048")
    assert out == _FAKE_PNG
    assert seen["url"].endswith("/images/generations")
    assert seen["body"]["model"] == "gpt-image-1"
    # 2048x2048 is not an OpenAI size → coerced to a safe square
    assert seen["body"]["size"] == "1024x1024"


def test_openai_adapter_url(home, monkeypatch):
    _write_desktop(home, image_provider="openai", image_model="dall-e-3",
                   keys={"OpenAI": {"provider": "openai", "api_key": "sk-oai"}})
    monkeypatch.setattr(g, "_request_json",
                        lambda *a, **k: {"data": [{"url": "http://x/i.png"}]})
    monkeypatch.setattr(g, "download_bytes", lambda url, **k: _FAKE_PNG)
    assert g.generate_bytes("p", "1024x1024") == _FAKE_PNG


# ---------------------------------------------------------------------------
# Adapter: DashScope (async create → poll → download)
# ---------------------------------------------------------------------------
def test_dashscope_adapter_polls_until_succeeded(home, monkeypatch):
    _write_desktop(home, image_provider="dashscope", image_model="wan2.6-image",
                   keys={"Ali": {"provider": "dashscope", "api_key": "sk-ali"}})
    calls = {"n": 0, "create_body": None}

    def fake_request(url, *, data, headers, method, timeout, tries):
        if method == "POST":
            calls["create_body"] = json.loads(data)
            assert headers.get("X-DashScope-Async") == "enable"
            return {"output": {"task_id": "t1", "task_status": "PENDING"}}
        # GET poll: PENDING once, then SUCCEEDED
        calls["n"] += 1
        if calls["n"] < 2:
            return {"output": {"task_status": "RUNNING"}}
        return {"output": {"task_status": "SUCCEEDED",
                           "choices": [{"message": {"content": [{"image": "http://x/o.png"}]}}]}}

    monkeypatch.setattr(g, "_request_json", fake_request)
    monkeypatch.setattr(g, "download_bytes", lambda url, **k: _FAKE_PNG)
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)
    out = g.generate_bytes("a city", "1280x1280")
    assert out == _FAKE_PNG
    # size converted x → * for DashScope
    assert calls["create_body"]["parameters"]["size"] == "1280*1280"


def test_dashscope_adapter_failed_task_raises(home, monkeypatch):
    _write_desktop(home, image_provider="dashscope", image_model="wan2.6-image",
                   keys={"Ali": {"provider": "dashscope", "api_key": "sk-ali"}})

    def fake_request(url, *, data, headers, method, timeout, tries):
        if method == "POST":
            return {"output": {"task_id": "t1", "task_status": "PENDING"}}
        return {"output": {"task_status": "FAILED", "message": "content policy"}}

    monkeypatch.setattr(g, "_request_json", fake_request)
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="FAILED"):
        g.generate_bytes("p", "1280x1280")


# ---------------------------------------------------------------------------
# Adapter: OpenRouter (chat/completions modalities → data: URL)
# ---------------------------------------------------------------------------
def test_openrouter_adapter_data_url(home, monkeypatch):
    _write_desktop(home, image_provider="openrouter",
                   image_model="google/gemini-2.5-flash-image-preview",
                   keys={"OpenRouter": {"provider": "openrouter", "api_key": "sk-or"}})
    data_url = "data:image/png;base64," + base64.b64encode(_FAKE_PNG).decode()
    seen = {}

    def fake_request(url, *, data, headers, method, timeout, tries):
        seen["url"] = url
        seen["body"] = json.loads(data)
        return {"choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]}

    monkeypatch.setattr(g, "_request_json", fake_request)
    out = g.generate_bytes("a robot", "1024x1024")
    assert out == _FAKE_PNG
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["modalities"] == ["image", "text"]


# ---------------------------------------------------------------------------
# Dispatch guards
# ---------------------------------------------------------------------------
def test_generate_bytes_no_key_raises(home, monkeypatch):
    _write_desktop(home, image_provider="openai", image_model="gpt-image-1")
    with pytest.raises(RuntimeError, match="no key"):
        g.generate_bytes("p", "1024x1024")


def test_generate_bytes_no_provider_selected_raises(home, monkeypatch):
    # a model alone, no provider → plain error (no inference)
    _write_desktop(home, image_model="gpt-image-1",
                   keys={"o": {"provider": "openai", "api_key": "sk"}})
    with pytest.raises(RuntimeError, match="no image provider"):
        g.generate_bytes("p", "1024x1024")


def test_generate_bytes_no_model_selected_raises(home, monkeypatch):
    _write_desktop(home, image_provider="openai",
                   keys={"o": {"provider": "openai", "api_key": "sk"}})
    with pytest.raises(RuntimeError, match="no image model"):
        g.generate_bytes("p", "1024x1024")


def test_generate_bytes_rejects_non_image(home, monkeypatch):
    _write_desktop(home, image_provider="openai", image_model="gpt-image-1",
                   keys={"OpenAI": {"provider": "openai", "api_key": "sk"}})
    bad = base64.b64encode(b"<html>nope</html>").decode()
    monkeypatch.setattr(g, "_request_json", lambda *a, **k: {"data": [{"b64_json": bad}]})
    with pytest.raises(RuntimeError, match="image"):
        g.generate_bytes("p", "1024x1024")
