"""R4 — agent self-image-generation (generate_image).

Pins the tool contract end to end with NO real network: registration,
the two-layer gate (static check_fn = key present; runtime handler = network on),
the happy path through a MOCKED generate_bytes, and the R7 write confinement
(the saved file always stays under sandbox/workspace).

Image config is UNIFIED on the provider keyring: a key is "present" when the
selected image provider has an entry in the desktop ``keys`` map. No ARK_API_KEY
env, no legacy single image_api_key, no bare file — exactly the provider + model
chosen in Settings · 模型 · 生图模型.
"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from lunamoth.tools.builtin import _image_gen, media
from lunamoth.tools.registry import discover_builtin_tools, registry
from lunamoth.tools.sandbox import Sandbox


# ---------------------------------------------------------------------------
# A ctx mirroring the real ToolContext surface, backed by a REAL Sandbox so the
# workspace/assets sibling geography is exactly what the runtime builds.
# ---------------------------------------------------------------------------
class GenCtx:
    def __init__(self, sandbox: Sandbox, network=True):
        self.sandbox = sandbox
        self._network = network

    @property
    def workspace(self) -> Path:
        return self.sandbox.workspace_dir

    @property
    def assets(self) -> Path:
        return self.sandbox.assets_dir

    def network_on(self) -> bool:
        return self._network

    def writable_paths(self):
        return []


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "sandbox")


def _write_desktop(home: Path, **fields):
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(json.dumps(fields), encoding="utf-8")


def _key_present(monkeypatch, tmp_path, *, provider="volcano",
                 model="doubao-seedream-4-0-250828", key="sk-test-key"):
    """Configure a complete, unified image selection: provider + model + a keyring
    entry for that provider, in a temp LUNAMOTH_HOME."""
    home = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    _write_desktop(home, image_provider=provider, image_model=model,
                   keys={provider: {"provider": provider, "api_key": key}})


def _no_key(monkeypatch, tmp_path):
    # Point LUNAMOTH_HOME at an empty dir so nothing resolves (no provider/key).
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "empty_home"))


# ---------------------------------------------------------------------------
# Registration / discovery
# ---------------------------------------------------------------------------
def test_tool_registers():
    entry = registry.get_entry("generate_image")
    assert entry is not None
    assert entry.toolset == "media"
    assert entry.handler is media.generate_image
    assert entry.check_fn is media._check_image_key
    assert "required" in entry.schema["parameters"]
    assert entry.schema["parameters"]["required"] == ["prompt"]


def test_discover_imports_media():
    imported = discover_builtin_tools()
    assert "lunamoth.tools.builtin.media" in imported


# ---------------------------------------------------------------------------
# check_fn — static capability gate (selected provider has a key / not)
# ---------------------------------------------------------------------------
def test_check_fn_true_when_provider_key_configured(monkeypatch, tmp_path):
    _key_present(monkeypatch, tmp_path)
    assert media._check_image_key() is True


def test_check_fn_false_with_no_key_and_empty_home(monkeypatch, tmp_path):
    _no_key(monkeypatch, tmp_path)
    assert media._check_image_key() is False


# ---------------------------------------------------------------------------
# provider + model + key resolved EXACTLY from the selection (no inference,
# no env, no default) — Settings · 模型 · 生图模型.
# ---------------------------------------------------------------------------
def test_active_provider_is_the_selected_one(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    _write_desktop(home, image_provider="dashscope", image_model="wan2.6-image")
    assert _image_gen.active_provider() == "dashscope"


def test_active_provider_blank_when_unset(monkeypatch, tmp_path):
    # a model id alone NEVER infers a provider — selection is explicit
    home = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    _write_desktop(home, image_model="doubao-seedream-4-0-250828")
    assert _image_gen.active_provider() == ""


def test_image_key_resolves_from_keyring(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    _write_desktop(home, image_provider="volcano", image_model="m",
                   keys={"火山": {"provider": "volcano", "api_key": "ark-from-keyring"}})
    assert _image_gen.image_key() == "ark-from-keyring"


def test_image_model_is_the_selected_model(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LUNAMOTH_HOME", str(home))
    _write_desktop(home, image_provider="volcano", image_model="doubao-seedream-custom")
    assert _image_gen.image_model() == "doubao-seedream-custom"


def test_image_model_blank_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "empty"))
    assert _image_gen.image_model() == ""


# ---------------------------------------------------------------------------
# handler — network gate (runtime, in the handler)
# ---------------------------------------------------------------------------
def test_handler_network_off_errors(monkeypatch, tmp_path, sandbox):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=False)
    out = json.loads(media.generate_image({"prompt": "a cat"}, ctx))
    assert "error" in out
    assert "network" in out["error"].lower()


def test_handler_empty_prompt_errors(monkeypatch, tmp_path, sandbox):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "   "}, ctx))
    assert "error" in out and "prompt" in out["error"].lower()


# ---------------------------------------------------------------------------
# handler — happy path with MOCKED dispatch (no real HTTP)
# ---------------------------------------------------------------------------
_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"


@pytest.fixture
def mock_network(monkeypatch):
    # media calls _image_gen.generate_bytes (the multi-provider dispatch). Mock it
    # to return a valid PNG, no real HTTP. media imports the name into its own ns.
    monkeypatch.setattr(_image_gen, "generate_bytes", lambda *a, **k: _FAKE_PNG)
    monkeypatch.setattr(media, "generate_bytes", lambda *a, **k: _FAKE_PNG)


def test_handler_happy_path_default_path(monkeypatch, tmp_path, sandbox, mock_network):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "a cat"}, ctx))
    assert out["ok"] is True
    assert out["path"].startswith("works/")
    assert out["bytes"] == len(_FAKE_PNG)
    on_disk = sandbox.workspace_dir / out["path"]
    assert on_disk.is_file()
    assert on_disk.read_bytes() == _FAKE_PNG


def test_handler_happy_path_custom_path(monkeypatch, tmp_path, sandbox, mock_network):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "a cat", "path": "works/cat.png"}, ctx))
    assert out["ok"] is True and out["path"] == "works/cat.png"
    assert (sandbox.workspace_dir / "works" / "cat.png").read_bytes() == _FAKE_PNG


# ---------------------------------------------------------------------------
# R7 confinement — the saved file ALWAYS stays under sandbox/workspace
# ---------------------------------------------------------------------------
def test_traversal_path_stays_in_workspace(monkeypatch, tmp_path, sandbox, mock_network):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x", "path": "../escape.png"}, ctx))
    # write_bytes (resolve_inside) refuses the traversal → visible error.
    assert "error" in out
    assert not (sandbox.root / "escape.png").exists()
    assert not (sandbox.root.parent / "escape.png").exists()


def test_assets_path_does_not_write_assets(monkeypatch, tmp_path, sandbox, mock_network):
    _key_present(monkeypatch, tmp_path)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x", "path": "assets/x.png"}, ctx))
    # write_bytes anchors to workspace_dir: "assets/x.png" lands UNDER workspace,
    # never in the read-only assets sibling.
    assert out["ok"] is True
    saved = sandbox.workspace_dir / out["path"]
    assert saved.is_file()
    assert sandbox.workspace_dir in saved.resolve().parents
    # The real assets sibling is untouched.
    assert not (sandbox.assets_dir / "x.png").exists()


# ---------------------------------------------------------------------------
# Image-signature validation — a non-image body is rejected, never saved as .png
# ---------------------------------------------------------------------------
def test_is_image_bytes_recognizes_formats():
    assert _image_gen.is_image_bytes(b"\x89PNG\r\n\x1a\nrest")
    assert _image_gen.is_image_bytes(b"\xff\xd8\xff and jpeg")
    assert _image_gen.is_image_bytes(b"GIF89a...")
    assert _image_gen.is_image_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP")
    assert not _image_gen.is_image_bytes(b"<html>error</html>")
    assert not _image_gen.is_image_bytes(b"")


def test_handler_rejects_non_image_body(monkeypatch, tmp_path, sandbox):
    # generate_bytes validates the body and raises on a non-image response; the
    # handler surfaces that as a visible error and writes nothing.
    _key_present(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise RuntimeError("the generation endpoint did not return an image")

    monkeypatch.setattr(media, "generate_bytes", _raise)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x"}, ctx))
    assert "error" in out and "image" in out["error"].lower()
    # nothing was written
    works = sandbox.workspace_dir / "works"
    assert not works.exists() or not list(works.glob("*"))


def test_handler_empty_result_errors(monkeypatch, tmp_path, sandbox):
    _key_present(monkeypatch, tmp_path)

    def _raise(*a, **k):
        raise RuntimeError("image generation returned no result")

    monkeypatch.setattr(media, "generate_bytes", _raise)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x"}, ctx))
    assert "error" in out and "no result" in out["error"].lower()


# ---------------------------------------------------------------------------
# Retry / no-fallback — transient HTTP retries then surfaces; 4xx breaks at once
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload: bytes):
        self._p = payload

    def read(self, *a):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _httperror(code: int):
    return urllib.error.HTTPError("http://ark", code, "err", {}, io.BytesIO(b'{"m":"x"}'))


def test_ark_generate_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _httperror(429)
        return _Resp(json.dumps({"data": [{"url": "http://x/i.png"}]}).encode())

    monkeypatch.setattr(_image_gen.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_image_gen.time, "sleep", lambda *_: None)
    assert _image_gen.ark_generate("p", "2048x2048") == ["http://x/i.png"]
    assert calls["n"] == 2  # retried once


def test_ark_generate_4xx_raises_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise _httperror(400)

    monkeypatch.setattr(_image_gen.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_image_gen.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        _image_gen.ark_generate("p", "2048x2048")
    assert calls["n"] == 1  # non-transient → no retry, no extra paid calls


def test_download_bytes_caps_size(monkeypatch):
    monkeypatch.setattr(_image_gen.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(b"x" * 50))
    with pytest.raises(RuntimeError):
        _image_gen.download_bytes("http://x/big", max_bytes=10)


def test_download_bytes_rejects_non_http_scheme():
    # SSRF guard: a file:// (or any non-http(s)) URL is refused before any fetch.
    for bad in ("file:///etc/passwd", "ftp://x/y", "data:text/plain,hi", "gopher://x"):
        with pytest.raises(RuntimeError):
            _image_gen.download_bytes(bad)
