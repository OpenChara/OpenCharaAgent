"""Dependency direction is enforced, not aspirational (CLAUDE.md module map).

Violations here mean a layer boundary broke:
  - nothing outside front/ may import front/ or any UI library (textual/rich)
  - protocol/ is the pure contract: zero project-internal dependencies
  - obs/ is write-only infrastructure: it may import config and nothing else
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "chara"

UI_LIBS = {"textual", "rich"}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        package = rel.parts[0] if len(rel.parts) > 1 else ""
        yield package, path


def _internal_imports(path: Path, package: str):
    """Top-level chara package/module names this file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.split(".")[0] == "chara":
                    parts = node.module.split(".")
                    yield parts[1] if len(parts) > 1 else node.names[0].name
            elif node.level == 1 and package:
                yield package  # sibling within the same package
            else:  # relative to the chara root
                if node.module:
                    yield node.module.split(".")[0]
                else:  # from .. import x, y
                    for alias in node.names:
                        yield alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "chara":
                    parts = alias.name.split(".")
                    yield parts[1] if len(parts) > 1 else ""


def _external_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_nothing_outside_front_imports_front():
    for package, path in _modules():
        if package == "front":
            continue
        bad = [t for t in _internal_imports(path, package) if t == "front"]
        assert not bad, f"{path} imports front/ — backend must not know the frontends"


def test_ui_libraries_only_in_front():
    for package, path in _modules():
        if package == "front":
            continue
        bad = sorted(set(_external_imports(path)) & UI_LIBS)
        assert not bad, f"{path} imports {bad} — UI libraries live in front/ only"


def test_protocol_events_and_codec_are_pure():
    """events.py/codec.py are the wire contract: zero internal deps, trivially
    serializable. api.py (CharaHandle) is the in-process implementation and MAY
    reach into the backend — that's its whole job."""
    for package, path in _modules():
        if package != "protocol" or path.name in {"api.py"}:
            continue
        bad = [t for t in _internal_imports(path, package) if t != "protocol"]
        assert not bad, f"{path} imports {bad} — the wire contract must stay pure"


def test_front_reaches_backend_only_through_protocol():
    """Frontends hold a CharaHandle and nothing deeper: no core/, no tools/.
    (content/session/presence/obs/config are data+infra and stay importable.)"""
    for package, path in _modules():
        if package != "front":
            continue
        bad = sorted(set(_internal_imports(path, package)) & {"core", "tools"})
        assert not bad, f"{path} imports {bad} — frontends go through protocol/ (CharaHandle)"


def test_obs_imports_only_config():
    for package, path in _modules():
        if package != "obs":
            continue
        bad = [t for t in _internal_imports(path, package) if t not in {"obs", "config"}]
        assert not bad, f"{path} imports {bad} — obs/ is leaf infrastructure"


# The gateway layer is decoupled from the agent internals AND its adapters are
# islands of each other (owner principle, 2026-06-17): a channel must never reach
# into core/ or tools/, and one platform adapter must never import a sibling.
_SHARED_MSG_MODULES = {"gateway", "__init__", "base", "access", "filters", "text"}


def test_messaging_is_isolated_from_core_and_tools():
    """messaging/ talks to a chara ONLY through protocol/ (CharaHandle) — never
    core/ or tools/ directly. So the gateway can evolve (or be dropped) without
    touching the agent."""
    for package, path in _modules():
        if package != "messaging":
            continue
        bad = sorted(set(_internal_imports(path, package)) & {"core", "tools", "front"})
        assert not bad, f"{path} imports {bad} — messaging/ stays decoupled from core/tools/front"


def test_server_imports_only_protocol_session_content():
    """server/ (hub + supervisor + dispatch + transports) drives charas through
    protocol/ (CharaHandle) and reads session/content data — it must NEVER import
    core/ or tools/ directly (one process = one activated session; the hub never
    hosts an agent). front/ is off-limits too (backend mustn't know the frontends)."""
    for package, path in _modules():
        if package != "server":
            continue
        bad = sorted(set(_internal_imports(path, package)) & {"core", "tools", "front"})
        assert not bad, f"{path} imports {bad} — server/ goes through protocol/session/content only"


def test_messaging_adapters_are_decoupled_from_each_other():
    """Each platform adapter is an island: it may share base/access/filters/text,
    but must not import a sibling adapter. gateway.py is the ONE composition root
    that knows them all — so qq/telegram/weixin can be added or removed in isolation."""
    msg_dir = SRC / "messaging"
    adapters = {p.stem for p in msg_dir.glob("*.py")} - _SHARED_MSG_MODULES
    for path in sorted(msg_dir.glob("*.py")):
        if path.stem not in adapters:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                imported = node.module.split(".")[0]
                siblings = adapters - {path.stem}
                assert imported not in siblings, (
                    f"{path.name} imports sibling adapter '{node.module}' — adapters must stay decoupled")
