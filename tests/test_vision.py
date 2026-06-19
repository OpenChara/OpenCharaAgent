"""Auxiliary vision model: a non-vision main model understands an uploaded image
by handing it to a SEPARATE vision model (cfg.vision_model) and feeding the text
back — hermes' auxiliary task=vision shape."""
from lunamoth.config import LLMConfig
from lunamoth.core.attachments import RawAttachment, ingest_attachments
from lunamoth.core.llm import LLMClient
from lunamoth.session.settings import Settings


def _client(vision_model="", live=True):
    return LLMClient(LLMConfig(
        provider="openai_compatible" if live else "mock",
        base_url="https://x.test/v1" if live else "",
        model="deepseek/deepseek-v4", vision_model=vision_model,
    ))


class _SB:
    def write_bytes(self, path, data):
        return path  # returns the workspace-relative path, like Sandbox


# ---- config plumbing -----------------------------------------------------------

def test_vision_model_flows_settings_to_llmconfig():
    cfg = Settings(vision_model="google/gemini-3-flash").to_llm_config()
    assert cfg.vision_model == "google/gemini-3-flash"


def test_vision_model_env_seed(monkeypatch):
    monkeypatch.setenv("LLM_VISION_MODEL", "openai/gpt-4o")
    from lunamoth.session import settings as S
    # _ENV_MAP carries the seed; coercion is a plain str
    assert "vision_model" in S._ENV_MAP


# ---- describe_image (the aux call) ---------------------------------------------

def test_describe_image_none_without_vision_model():
    c = _client(vision_model="")
    assert c.describe_image(b"\x89PNGfake", "image/png") is None


def test_describe_image_none_for_non_image():
    c = _client(vision_model="google/gemini-3-flash")
    assert c.describe_image(b"data", "text/plain") is None


def test_describe_image_calls_the_vision_model(monkeypatch):
    c = _client(vision_model="google/gemini-3-flash")
    seen = {}

    def fake_raw(messages, max_tokens=1024, timeout=60.0, model="", temperature=0.3):
        seen["model"] = model
        seen["content"] = messages[0]["content"]
        return "a red square on white"

    monkeypatch.setattr(c, "raw_complete", fake_raw)
    out = c.describe_image(b"\x89PNGfake", "image/png", question="what color?")
    assert out == "a red square on white"
    assert seen["model"] == "google/gemini-3-flash"      # the SEPARATE model, not the main one
    assert any(p.get("type") == "image_url" for p in seen["content"])
    # the question is folded into the prompt (task-relevant description)
    assert any(p.get("type") == "text" and "what color?" in p["text"] for p in seen["content"])


def test_describe_image_generic_prompt_without_question(monkeypatch):
    c = _client(vision_model="google/gemini-3-flash")
    seen = {}
    monkeypatch.setattr(c, "raw_complete",
                        lambda messages, **k: seen.update(text=messages[0]["content"][0]["text"]) or "desc")
    c.describe_image(b"\x89PNGfake", "image/png")
    assert "Describe everything visible" in seen["text"]   # the generic hermes prompt


def test_describe_image_empty_completion_is_none(monkeypatch):
    c = _client(vision_model="google/gemini-3-flash")
    monkeypatch.setattr(c, "raw_complete", lambda *a, **k: "")
    assert c.describe_image(b"\x89PNGfake", "image/png") is None  # "" → None (honest)


# ---- the interceptor in ingest_attachments -------------------------------------

def test_ingest_injects_description_when_described():
    raws = [RawAttachment(name="p.png", mime="image/png", data=b"\x89PNGx")]
    res = ingest_attachments(raws, sandbox=_SB(), vision_ok=False,
                             describe=lambda d, m: "a cat sitting on a mat")
    joined = "\n".join(res.notes)
    assert "a cat sitting on a mat" in joined
    assert not res.notices                       # no "can't see" notice once described
    assert res.saved == ["uploads/p.png"]        # still saved to disk for re-inspection


def test_ingest_keeps_honest_note_without_vision_model():
    raws = [RawAttachment(name="p.png", mime="image/png", data=b"\x89PNGx")]
    res = ingest_attachments(raws, sandbox=_SB(), vision_ok=False,
                             describe=lambda d, m: None)  # no vision model
    assert res.notices and "no vision" in res.notices[0].lower()
    assert not any("described by the vision model" in n for n in res.notes)


def test_ingest_vision_ok_still_inlines_no_describe_call():
    called = {"n": 0}

    def describe(d, m):
        called["n"] += 1
        return "should not be called"

    raws = [RawAttachment(name="p.png", mime="image/png", data=b"\x89PNGx")]
    res = ingest_attachments(raws, sandbox=_SB(), vision_ok=True, describe=describe)
    assert res.content_parts and res.content_parts[0]["type"] == "image_url"
    assert called["n"] == 0  # vision-capable main model → inline, no aux describe


def test_ingest_describe_failure_falls_back_to_honest_note():
    def boom(d, m):
        raise RuntimeError("vision endpoint down")

    raws = [RawAttachment(name="p.png", mime="image/png", data=b"\x89PNGx")]
    res = ingest_attachments(raws, sandbox=_SB(), vision_ok=False, describe=boom)
    assert res.notices and "no vision" in res.notices[0].lower()  # never crashes the turn


# ---- raw_complete: adapt to reasoning-mandatory side-task models ----------------

def test_raw_complete_retries_with_exclude_on_reasoning_mandatory(monkeypatch):
    """A reasoning-MANDATORY model (e.g. a vision model that can't disable thinking)
    rejects the cheap reasoning:{enabled:false} side-tasks send. raw_complete adapts
    the REQUEST — reason internally, exclude it from the output — and retries once."""
    import io
    import json as _json
    import urllib.error
    import urllib.request

    c = LLMClient(LLMConfig(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                            api_key="sk", model="deepseek/deepseek-v4", reasoning="off"))
    assert c.reasoning_supported()  # openrouter route → the reasoning param is sent
    seen = []

    class _R:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._p

    def fake_urlopen(req, timeout=0):
        body = _json.loads(req.data)
        seen.append(body.get("reasoning"))
        if body.get("reasoning") == {"enabled": False}:
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {},
                io.BytesIO(b'{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be disabled."}}'))
        return _R(_json.dumps({"choices": [{"message": {"content": "a red square"}}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = c.raw_complete([{"role": "user", "content": "hi"}])
    assert out == "a red square"
    assert seen == [{"enabled": False}, {"exclude": True}]  # retried with exclude:true


def test_raw_complete_non_reasoning_400_does_not_retry(monkeypatch):
    # a 400 unrelated to reasoning surfaces (degrades to "") with NO second attempt
    import io
    import urllib.error
    import urllib.request

    c = LLMClient(LLMConfig(provider="openrouter", base_url="https://openrouter.ai/api/v1",
                            api_key="sk", model="deepseek/deepseek-v4", reasoning="off"))
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                     io.BytesIO(b'{"error":{"message":"bad messages"}}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert c.raw_complete([{"role": "user", "content": "hi"}]) == ""
    assert calls["n"] == 1  # no reasoning retry
