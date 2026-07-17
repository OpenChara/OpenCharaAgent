"""providers.max_output_tokens + llm._max_tokens_param — the write_file/patch
truncation fix (owner 2026-06-19). The request "follows the model" (OpenRouter's
reported max_completion_tokens), defaults to 8192 when unknown, and honors an
explicit LLM_MAX_TOKENS override. Replaces the flat 4096 that cut large tool-call
arguments mid-argument (~12KB).
"""
from __future__ import annotations

from chara.core import providers


def test_default_when_offline_or_non_openrouter():
    # mock/offline and non-openrouter routes have no catalogue → the 8192 default.
    assert providers.max_output_tokens("mock", "", "whatever") == providers.DEFAULT_MAX_OUTPUT
    assert providers.max_output_tokens("local", "http://localhost:1234/v1", "m") == 8192


def test_operator_override_wins():
    assert providers.max_output_tokens("openrouter", "", "any/model", override=20000) == 20000
    # override<=0 is ignored (falls through to resolution/default)
    assert providers.max_output_tokens("mock", "", "m", override=0) == providers.DEFAULT_MAX_OUTPUT


def test_resolves_from_openrouter_catalogue(monkeypatch):
    # Seed the in-process output memo as if the catalogue had been fetched.
    monkeypatch.setitem(providers._memo, "openrouter", {"acme/big": 200000})
    monkeypatch.setitem(providers._memo, "openrouter_out", {"acme/big": 64000})
    assert providers.max_output_tokens("openrouter", "", "acme/big") == 64000
    # A model absent from the output map → the default.
    assert providers.max_output_tokens("openrouter", "", "acme/unknown") == 8192


def test_failed_catalogue_fetch_is_retried_not_memoized(monkeypatch):
    """REGRESSION: one offline moment at first resolve used to memoize {} for the
    life of the process — a 200K model ran on the 32K default window until
    restart. A failure must be retried after the cooldown; only success memoizes."""
    import json
    import time
    import urllib.request

    monkeypatch.setattr(providers, "_memo", {})
    monkeypatch.setattr(providers, "_fetch_failed_at", 0.0)
    calls = {"n": 0}

    def dead(*a, **k):
        calls["n"] += 1
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    window, determined = providers.context_window_resolved("openrouter", "", "acme/big")
    assert (window, determined) == (providers.DEFAULT_WINDOW, False)
    assert calls["n"] == 1
    assert "openrouter" not in providers._memo  # the FAILURE is not memoized

    # Within the cooldown: fail fast, no second network attempt per turn.
    providers.context_window_resolved("openrouter", "", "acme/big")
    assert calls["n"] == 1

    # After the cooldown the fetch is retried, and a SUCCESS is memoized.
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "acme/big", "context_length": 200000,
                                         "top_provider": {"max_completion_tokens": 64000}}]}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(providers, "_fetch_failed_at",
                        time.time() - providers._FETCH_RETRY_SECONDS - 1)
    window, determined = providers.context_window_resolved("openrouter", "", "acme/big")
    assert (window, determined) == (200000, True)
    assert providers._memo["openrouter"]["acme/big"] == 200000
    # ... and the parallel output map rode the same successful payload.
    assert providers.max_output_tokens("openrouter", "", "acme/big") == 64000


def test_max_tokens_param_routing(monkeypatch):
    from chara.core.llm import LLMClient
    from chara.config import LLMConfig

    # OpenAI-direct route → max_completion_tokens; everything else → max_tokens.
    monkeypatch.setattr(providers, "max_output_tokens", lambda *a, **k: 12345)
    direct = LLMClient(LLMConfig(provider="openai", base_url="https://api.openai.com/v1",
                                 model="gpt-x", api_key="k", max_tokens=0))
    assert direct._max_tokens_param() == {"max_completion_tokens": 12345}
    other = LLMClient(LLMConfig(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                                model="m", api_key="k", max_tokens=0))
    assert other._max_tokens_param() == {"max_tokens": 12345}
