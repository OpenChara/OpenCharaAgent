"""R11 — local matte (抠像) model management.

No network and no rembg: the heavy ML stack is an optional extra, so deps are
absent here. Download is exercised against a TINY injected model with a mocked
urlopen; the real registry's sizes/URLs are only shape-checked.
"""
from __future__ import annotations

import io
import json

import pytest

from lunamoth.visuals import matte
from lunamoth.visuals.matte import MatteModel


@pytest.fixture(autouse=True)
def temp_dirs(tmp_path, monkeypatch):
    # Isolate both the rembg cache (U2NET_HOME) and the desktop.json home.
    monkeypatch.setenv("U2NET_HOME", str(tmp_path / "u2net"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LUNAMOTH_MATTE_MODEL", raising=False)
    # Reset the module-global download state between tests.
    monkeypatch.setattr(matte, "_progress", {})
    monkeypatch.setattr(matte, "_active", set())
    yield


class _Resp:
    """A minimal urlopen response: read(n) yields the payload in chunks."""
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def tiny_model(monkeypatch):
    payload = b"ONNXFAKE" * 16  # 128 bytes
    spec = MatteModel("test-tiny", "Tiny Test", "https://example.invalid/x.onnx",
                      len(payload), "test model")
    monkeypatch.setitem(matte.MODELS, "test-tiny", spec)
    return spec, payload


# --- registry / paths ---------------------------------------------------------

def test_registry_is_well_formed():
    assert matte.DEFAULT_MODEL in matte.MODELS
    for mid, m in matte.MODELS.items():
        assert m.id == mid
        assert m.url.startswith("https://")
        assert m.size > 0 and m.label and m.note


def test_matte_home_honors_u2net_home(tmp_path):
    assert matte.matte_home() == tmp_path / "u2net"
    assert matte.model_path("birefnet-general").name == "birefnet-general.onnx"


# --- selected model precedence ------------------------------------------------

def test_selected_model_default_then_desktop_then_env(tmp_path, monkeypatch):
    assert matte.selected_model() == matte.DEFAULT_MODEL
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(json.dumps({"matte_model": "isnet-general-use"}), encoding="utf-8")
    assert matte.selected_model() == "isnet-general-use"
    monkeypatch.setenv("LUNAMOTH_MATTE_MODEL", "u2net")
    assert matte.selected_model() == "u2net"
    # an unknown id never wins — falls through to the default
    monkeypatch.setenv("LUNAMOTH_MATTE_MODEL", "nope")
    assert matte.selected_model() == "isnet-general-use"


# --- install state ------------------------------------------------------------

def test_is_installed_requires_exact_size(tiny_model):
    spec, payload = tiny_model
    p = matte.model_path("test-tiny")
    p.parent.mkdir(parents=True, exist_ok=True)
    assert matte.is_installed("test-tiny") is False
    p.write_bytes(payload[:-1])           # wrong size → not installed
    assert matte.is_installed("test-tiny") is False
    p.write_bytes(payload)                 # exact size → installed
    assert matte.is_installed("test-tiny") is True


def test_deps_unavailable_without_rembg():
    # rembg is an optional extra, absent in the test env.
    assert matte.deps_available() is False


# --- download -----------------------------------------------------------------

def test_download_streams_and_verifies_size(tiny_model, monkeypatch):
    spec, payload = tiny_model
    monkeypatch.setattr(matte.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    seen = []
    path = matte.download("test-tiny", progress_cb=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == payload
    assert matte.is_installed("test-tiny") is True
    assert seen and seen[-1] == (len(payload), len(payload))
    # no leftover .part file
    assert not path.with_name("test-tiny.onnx.part").exists()


def test_download_rejects_size_mismatch_and_cleans_up(tiny_model, monkeypatch):
    spec, payload = tiny_model
    monkeypatch.setattr(matte.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(payload[:-3]))  # short body
    with pytest.raises(RuntimeError, match="size mismatch"):
        matte.download("test-tiny")
    assert matte.is_installed("test-tiny") is False
    assert not matte.model_path("test-tiny").exists()
    assert not matte.model_path("test-tiny").with_name("test-tiny.onnx.part").exists()


def test_download_unknown_model_raises():
    with pytest.raises(KeyError):
        matte.download("ghost")


def test_download_noop_when_already_installed(tiny_model, monkeypatch):
    spec, payload = tiny_model
    p = matte.model_path("test-tiny")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)

    def _boom(*a, **k):
        raise AssertionError("should not fetch an already-installed model")

    monkeypatch.setattr(matte.urllib.request, "urlopen", _boom)
    assert matte.download("test-tiny") == p


def test_download_async_records_done(tiny_model, monkeypatch):
    spec, payload = tiny_model
    monkeypatch.setattr(matte.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    matte.download_async("test-tiny")
    # join the worker thread deterministically
    for th in list(__import__("threading").enumerate()):
        if th.name == "matte-dl-test-tiny":
            th.join(timeout=5)
    prog = matte.progress("test-tiny")
    assert prog and prog["state"] == "done"
    assert matte.is_installed("test-tiny") is True


def test_delete_removes_file(tiny_model):
    spec, payload = tiny_model
    p = matte.model_path("test-tiny")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    assert matte.delete("test-tiny") is True
    assert not p.exists()
    assert matte.delete("test-tiny") is False  # already gone


# --- status -------------------------------------------------------------------

def test_status_shape():
    st = matte.status()
    assert st["deps"] is False  # no rembg in the test env
    assert st["active"] == matte.DEFAULT_MODEL
    ids = {m["id"] for m in st["models"]}
    assert matte.DEFAULT_MODEL in ids
    one = st["models"][0]
    assert set(one) >= {"id", "label", "note", "size", "installed", "active", "progress"}


# --- cut (runtime) ------------------------------------------------------------

def test_cut_without_deps_is_a_visible_error():
    with pytest.raises(RuntimeError, match="visuals"):
        matte.cut("whatever.png")
