"""Dependency direction is enforced, not aspirational (docs/refactor-plan.md §3.2).

Violations here mean a layer boundary broke:
  - nothing outside front/ may import front/ or any UI library (textual/rich)
  - protocol/ is the pure contract: zero project-internal dependencies
  - obs/ is write-only infrastructure: it may import config and nothing else
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "lunamoth"

UI_LIBS = {"textual", "rich"}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        package = rel.parts[0] if len(rel.parts) > 1 else ""
        yield package, path


def _internal_imports(path: Path, package: str):
    """Top-level lunamoth package/module names this file imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.split(".")[0] == "lunamoth":
                    parts = node.module.split(".")
                    yield parts[1] if len(parts) > 1 else node.names[0].name
            elif node.level == 1 and package:
                yield package  # sibling within the same package
            else:  # relative to the lunamoth root
                if node.module:
                    yield node.module.split(".")[0]
                else:  # from .. import x, y
                    for alias in node.names:
                        yield alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "lunamoth":
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
