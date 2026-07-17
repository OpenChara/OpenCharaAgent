"""R11 — local matte (抠像) model management.

No network and no rembg: the heavy ML stack is an optional extra, so deps are
absent here. Download is exercised against a TINY injected model with a mocked
urlopen; the real registry's sizes/URLs are only shape-checked.
"""
from __future__ import annotations

import io
import json

import pytest

from chara.visuals import matte
from chara.visuals.matte import MatteModel


@pytest.fixture(autouse=True)
def temp_dirs(tmp_path, monkeypatch):
    # Isolate both the rembg cache (U2NET_HOME) and the desktop.json home.
    monkeypatch.setenv("U2NET_HOME", str(tmp_path / "u2net"))
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CHARA_MATTE_MODEL", raising=False)
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


def test_registry_holds_only_the_two_birefnet_models():
    # #4: keep only the two BiRefNet models; the stronger one is the default.
    assert set(matte.MODELS) == {"birefnet-general", "birefnet-general-lite"}
    assert matte.DEFAULT_MODEL == "birefnet-general"
    # the lite model stays available as a lighter pick
    assert "birefnet-general-lite" in matte.MODELS


def test_matte_home_honors_u2net_home(tmp_path):
    assert matte.matte_home() == tmp_path / "u2net"
    assert matte.model_path("birefnet-general").name == "birefnet-general.onnx"


# --- selected model precedence ------------------------------------------------

def test_selected_model_default_then_desktop_then_env(tmp_path, monkeypatch):
    assert matte.selected_model() == matte.DEFAULT_MODEL
    assert matte.DEFAULT_MODEL == "birefnet-general"  # the stronger flagship, not lite
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "desktop.json").write_text(json.dumps({"matte_model": "birefnet-general-lite"}), encoding="utf-8")
    assert matte.selected_model() == "birefnet-general-lite"
    monkeypatch.setenv("CHARA_MATTE_MODEL", "birefnet-general")
    assert matte.selected_model() == "birefnet-general"
    # an unknown id never wins — falls through to the desktop.json choice
    monkeypatch.setenv("CHARA_MATTE_MODEL", "nope")
    assert matte.selected_model() == "birefnet-general-lite"


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


@pytest.mark.skipif(matte.deps_available(), reason="visuals stack (rembg) installed in this env")
def test_deps_unavailable_without_rembg():
    # rembg is an optional extra; this asserts the deps-ABSENT path, so it only runs
    # when rembg is genuinely missing (CI). On a dev box with the visuals extra it skips.
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


def _join_matte_worker(model_id="test-tiny", timeout=5):
    for th in list(__import__("threading").enumerate()):
        if th.name == f"matte-dl-{model_id}":
            th.join(timeout=timeout)


def test_download_async_records_done(tiny_model, monkeypatch):
    spec, payload = tiny_model
    # deps already present → install goes straight to the weights download
    monkeypatch.setattr(matte, "deps_available", lambda: True)
    monkeypatch.setattr(matte.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    matte.download_async("test-tiny")
    _join_matte_worker()
    prog = matte.progress("test-tiny")
    assert prog and prog["state"] == "done"
    assert matte.is_installed("test-tiny") is True


def test_download_async_installs_deps_first_when_missing(tiny_model, monkeypatch):
    # ONE click: with deps absent, the worker installs the engine FIRST (the
    # installing_deps phase), then downloads the weights — no separate deps step.
    spec, payload = tiny_model
    monkeypatch.setattr(matte, "deps_available", lambda: False)
    phases = []

    def fake_install_deps(timeout=1800):
        phases.append(matte.progress("test-tiny")["state"])  # should be 'installing_deps'

    monkeypatch.setattr(matte, "_install_deps_blocking", fake_install_deps)
    monkeypatch.setattr(matte.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload))
    matte.download_async("test-tiny")
    _join_matte_worker()
    assert phases == ["installing_deps"]                  # deps installed first
    prog = matte.progress("test-tiny")
    assert prog and prog["state"] == "done"               # then weights downloaded
    assert matte.is_installed("test-tiny") is True


def test_download_async_surfaces_deps_install_failure(tiny_model, monkeypatch):
    monkeypatch.setattr(matte, "deps_available", lambda: False)

    def boom(timeout=1800):
        raise RuntimeError("could not install background-removal dependencies: pip exploded")

    monkeypatch.setattr(matte, "_install_deps_blocking", boom)
    matte.download_async("test-tiny")
    _join_matte_worker()
    prog = matte.progress("test-tiny")
    assert prog and prog["state"] == "error" and "dependencies" in prog["error"]
    assert matte.is_installed("test-tiny") is False  # never downloaded the weights


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
    assert st["deps"] is matte.deps_available()  # status reports the REAL dep state
    assert st["active"] == matte.DEFAULT_MODEL
    ids = {m["id"] for m in st["models"]}
    assert matte.DEFAULT_MODEL in ids
    one = st["models"][0]
    assert set(one) >= {"id", "label", "note", "size", "installed", "active", "progress"}


# --- cut (runtime) ------------------------------------------------------------

@pytest.mark.skipif(matte.deps_available(), reason="visuals stack (rembg) installed in this env")
def test_cut_without_deps_is_a_visible_error():
    with pytest.raises(RuntimeError, match="visuals"):
        matte.cut("whatever.png")
