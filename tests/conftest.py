"""Shared test fixtures + a safety net for config isolation.

The runtime resolves its on-disk roots from three env vars at CALL time
(CHARA_HOME, CHARA_CONFIG_DIR, CHARA_SANDBOX — see core/providers.py,
content/rules.py, messaging/*.py, etc.). Most tests already point these at a
tmp dir before exercising the runtime, but the discipline is manual: a test that
forgets would silently read/write the developer's REAL ~/.chara (the global
API key, login hash, every other chara's session).

`_isolate_config_roots` below is an AUTOUSE safety net: before every test it
defaults all three to a per-test tmp dir. A test that sets its own values still
wins (its monkeypatch.setenv runs after this fixture and last-write-wins; both
revert at teardown), so this changes nothing for the ~34 files that already
isolate — it only catches the ones that forget. It deliberately does NOT touch
already-imported module globals (some tests monkeypatch SANDBOX_ROOT on the
module directly for that); this net covers the env-var-resolved paths, which is
where the real "wrote to the real home" footgun lives.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_roots(tmp_path, monkeypatch):
    """Point the config/home/sandbox roots at a per-test tmp dir by default."""
    monkeypatch.setenv("CHARA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CHARA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CHARA_SANDBOX", str(tmp_path / "sandbox"))
    # Isolation is now read from CHARA_PY_BACKEND (the one authority behind
    # EnvState.permissions().isolation). A CLI test that does os.environ.update(
    # meta.env()) leaks it across tests, so pin it clean per test — a test that
    # needs a specific backend sets it itself (monkeypatch wins, last-write).
    monkeypatch.delenv("CHARA_PY_BACKEND", raising=False)
    monkeypatch.delenv("LUNAMOSS_PY_BACKEND", raising=False)
    yield
