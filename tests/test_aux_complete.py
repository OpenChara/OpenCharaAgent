"""hub._complete surfaces an empty / no-content 200 as a real error, never "".

The aux-LLM path (card draft / world-book / visual brief / field rewrite) must
fail loudly rather than hand back a blank a caller could mistake for a valid empty
answer. _http_json is resolved off the hub package, so patching it here exercises
_complete's real body.
"""
from __future__ import annotations

import pytest

from chara.server import hub as H
from chara.server.hub._common import HubRpcError


def _defaults() -> dict[str, str]:
    return {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
            "model": "m", "api_key": "sk-x"}  # api_key present → no keyring needed


def test_complete_raises_on_empty_content(monkeypatch):
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: {"choices": [{"message": {"content": ""}}]})
    with pytest.raises(HubRpcError):
        H._complete(_defaults(), "sys", "user")


def test_complete_raises_on_missing_choices(monkeypatch):
    monkeypatch.setattr(H, "_http_json", lambda *a, **k: {})
    with pytest.raises(HubRpcError):
        H._complete(_defaults(), "sys", "user")


def test_complete_returns_real_content(monkeypatch):
    monkeypatch.setattr(H, "_http_json",
                        lambda *a, **k: {"choices": [{"message": {"content": "hello"}}]})
    assert H._complete(_defaults(), "sys", "user") == "hello"
