"""Adversarial path-confinement battery for tools/builtin/_pathsec.py.

This is the application-level backstop behind the OS jail for a runtime that runs
untrusted, AI-supplied paths: every model-supplied path must resolve UNDER the
workspace (or an operator-opted-in writable root) or raise PathEscape — never
silently escape. The OS jail is the outer wall; this is the inner one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from chara.tools.builtin._pathsec import PathEscape, resolve_in_workspace


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = (tmp_path / "sandbox" / "workspace").resolve()
    w.mkdir(parents=True)
    return w


# ---- escapes that MUST raise ------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../../etc/passwd",                 # plain relative traversal
    "../outside.txt",                   # one level up
    "sub/../../../etc/hosts",           # .. after a valid prefix
    "a/b/c/../../../../../../etc/shadow",  # deep climb past the root
    "./../../escape",                   # leading ./ then climb
])
def test_relative_traversal_escapes_raise(ws, evil):
    with pytest.raises(PathEscape):
        resolve_in_workspace(evil, ws)


def test_absolute_path_outside_workspace_raises(ws):
    with pytest.raises(PathEscape):
        resolve_in_workspace("/etc/passwd", ws)
    with pytest.raises(PathEscape):
        resolve_in_workspace(str(ws.parent.parent / "secret.txt"), ws)


def test_empty_path_raises(ws):
    with pytest.raises(PathEscape):
        resolve_in_workspace("", ws)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlink_inside_workspace_pointing_outside_escapes(ws, tmp_path):
    """A symlink the chara could create INSIDE its workspace must not become an
    escape hatch — resolution follows the link and confinement is checked on the
    real target."""
    secret_dir = (tmp_path / "outside")
    secret_dir.mkdir()
    (secret_dir / "key").write_text("SECRET")
    link = ws / "escape"
    os.symlink(secret_dir, link)
    # accessing through the in-workspace symlink resolves to the outside target
    with pytest.raises(PathEscape):
        resolve_in_workspace("escape/key", ws)


def test_null_byte_path_is_rejected(ws):
    # A NUL byte can truncate a path at the OS layer; it must never resolve clean.
    with pytest.raises((PathEscape, ValueError)):
        resolve_in_workspace("ok.txt\x00/../../etc/passwd", ws)


# ---- legitimate paths that MUST resolve under the workspace -----------------

def test_plain_relative_path_allowed(ws):
    got = resolve_in_workspace("notes/today.md", ws)
    assert got == ws / "notes" / "today.md"
    assert str(got).startswith(str(ws))


def test_tilde_expands_to_workspace_not_real_home(ws):
    got = resolve_in_workspace("~/mine.txt", ws)
    assert got == ws / "mine.txt"
    assert str(Path.home()) not in str(got)


def test_operator_opted_in_writable_path_allowed(ws, tmp_path):
    extra = (tmp_path / "shared").resolve()
    extra.mkdir()
    got = resolve_in_workspace(str(extra / "out.txt"), ws, writable_paths=[str(extra)])
    assert got == extra / "out.txt"
    # but a sibling of the opted-in dir is still outside
    with pytest.raises(PathEscape):
        resolve_in_workspace(str(tmp_path / "elsewhere.txt"), ws, writable_paths=[str(extra)])


def test_assets_is_readable_but_not_writable(ws):
    assets = (ws.parent / "assets").resolve()
    assets.mkdir(parents=True, exist_ok=True)
    # readable=True allows the assets sibling…
    got = resolve_in_workspace("assets/ref.png", ws, assets_dir=assets, readable=True)
    assert got == assets / "ref.png"
    # …but a WRITE (readable=False, the default) into assets is refused.
    with pytest.raises(PathEscape):
        resolve_in_workspace("assets/ref.png", ws, assets_dir=assets, readable=False)
