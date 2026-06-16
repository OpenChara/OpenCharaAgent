"""R4 — agent self-image-generation (generate_image).

Pins the tool contract end to end with NO real network: registration,
the two-layer gate (static check_fn = key present; runtime handler = network on),
the happy path through MOCKED ark_generate/download_bytes, and the R7 write
confinement (the saved file always stays under sandbox/workspace).
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


def _key_present(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "sk-test-key")


def _no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARK_IMAGE_MODEL", raising=False)
    # Point LUNAMOTH_HOME at an empty dir so the file fallback finds nothing.
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
# check_fn — static capability gate (key present / absent)
# ---------------------------------------------------------------------------
def test_check_fn_true_when_key_in_env(monkeypatch):
    _key_present(monkeypatch)
    assert media._check_image_key() is True


def test_check_fn_false_with_no_key_and_empty_home(monkeypatch, tmp_path):
    _no_key(monkeypatch, tmp_path)
    assert media._check_image_key() is False


# ---------------------------------------------------------------------------
# handler — network gate (runtime, in the handler)
# ---------------------------------------------------------------------------
def test_handler_network_off_errors(monkeypatch, sandbox):
    _key_present(monkeypatch)
    ctx = GenCtx(sandbox, network=False)
    out = json.loads(media.generate_image({"prompt": "a cat"}, ctx))
    assert "error" in out
    assert "network" in out["error"].lower()


def test_handler_empty_prompt_errors(monkeypatch, sandbox):
    _key_present(monkeypatch)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "   "}, ctx))
    assert "error" in out and "prompt" in out["error"].lower()


# ---------------------------------------------------------------------------
# handler — happy path with MOCKED network (no real HTTP)
# ---------------------------------------------------------------------------
_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"


@pytest.fixture
def mock_network(monkeypatch):
    monkeypatch.setattr(_image_gen, "ark_generate", lambda *a, **k: ["http://x/img.png"])
    monkeypatch.setattr(_image_gen, "download_bytes", lambda *a, **k: _FAKE_PNG)
    # media imports the names into its own module namespace.
    monkeypatch.setattr(media, "ark_generate", lambda *a, **k: ["http://x/img.png"])
    monkeypatch.setattr(media, "download_bytes", lambda *a, **k: _FAKE_PNG)


def test_handler_happy_path_default_path(monkeypatch, sandbox, mock_network):
    _key_present(monkeypatch)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "a cat"}, ctx))
    assert out["ok"] is True
    assert out["path"].startswith("works/")
    assert out["bytes"] == len(_FAKE_PNG)
    on_disk = sandbox.workspace_dir / out["path"]
    assert on_disk.is_file()
    assert on_disk.read_bytes() == _FAKE_PNG


def test_handler_happy_path_custom_path(monkeypatch, sandbox, mock_network):
    _key_present(monkeypatch)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "a cat", "path": "works/cat.png"}, ctx))
    assert out["ok"] is True and out["path"] == "works/cat.png"
    assert (sandbox.workspace_dir / "works" / "cat.png").read_bytes() == _FAKE_PNG


# ---------------------------------------------------------------------------
# R7 confinement — the saved file ALWAYS stays under sandbox/workspace
# ---------------------------------------------------------------------------
def test_traversal_path_stays_in_workspace(monkeypatch, sandbox, mock_network):
    _key_present(monkeypatch)
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x", "path": "../escape.png"}, ctx))
    # write_bytes (resolve_inside) refuses the traversal → visible error.
    assert "error" in out
    assert not (sandbox.root / "escape.png").exists()
    assert not (sandbox.root.parent / "escape.png").exists()


def test_assets_path_does_not_write_assets(monkeypatch, sandbox, mock_network):
    _key_present(monkeypatch)
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


def test_handler_rejects_non_image_body(monkeypatch, sandbox):
    _key_present(monkeypatch)
    monkeypatch.setattr(media, "ark_generate", lambda *a, **k: ["http://x/err.html"])
    monkeypatch.setattr(media, "download_bytes", lambda *a, **k: b"<html>error</html>")
    ctx = GenCtx(sandbox, network=True)
    out = json.loads(media.generate_image({"prompt": "x"}, ctx))
    assert "error" in out and "image" in out["error"].lower()
    # nothing was written
    works = sandbox.workspace_dir / "works"
    assert not works.exists() or not list(works.glob("*"))


def test_handler_empty_result_errors(monkeypatch, sandbox):
    _key_present(monkeypatch)
    monkeypatch.setattr(media, "ark_generate", lambda *a, **k: [])
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
    _key_present(monkeypatch)
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
    _key_present(monkeypatch)
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
