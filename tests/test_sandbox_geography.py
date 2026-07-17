"""Sandbox geography confinement (R7) — SECURITY-CRITICAL.

The chara's space has three areas:
  workspace/        private read-write home
  workspace/works/  the shareable shelf (surfaced by the Works tab)
  assets/           a read-only reference SIBLING of workspace (card art +
                    operator-dropped reference material) — readable, never writable

These tests pin the confinement contract: reads resolve under {workspace, assets};
writes/patches resolve under workspace ONLY; assets is truly read-only; and no
path (absolute, ``..`` traversal, symlink) escapes the sandbox.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chara.tools.builtin import file_tools, search
from chara.tools.sandbox import Sandbox, SandboxViolation


# ---------------------------------------------------------------------------
# A ctx that mirrors the REAL ToolContext contract (workspace + assets), backed
# by a real Sandbox so the sibling geography is exactly what the runtime builds.
# ---------------------------------------------------------------------------
class GeoCtx:
    def __init__(self, sandbox: Sandbox, writable=()):
        self.sandbox = sandbox
        self._writable = list(writable)
        self._scratch = {}

    @property
    def workspace(self) -> Path:
        return self.sandbox.workspace_dir

    @property
    def assets(self) -> Path:
        return self.sandbox.assets_dir

    def writable_paths(self):
        return list(self._writable)


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "sandbox")


@pytest.fixture
def ctx(sandbox):
    return GeoCtx(sandbox)


def _j(fn, args, ctx):
    return json.loads(fn(args, ctx))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def test_assets_is_a_sibling_of_workspace_not_under_it(sandbox):
    assert sandbox.assets_dir.parent == sandbox.root
    assert sandbox.workspace_dir.parent == sandbox.root
    assert sandbox.assets_dir != sandbox.workspace_dir
    # assets is NOT inside workspace
    assert sandbox.workspace_dir not in sandbox.assets_dir.parents
    assert sandbox.assets_dir.is_dir() and sandbox.workspace_dir.is_dir()


# ---------------------------------------------------------------------------
# Reads: workspace + the virtual assets/ prefix both resolve
# ---------------------------------------------------------------------------
def test_read_file_reaches_assets_via_virtual_prefix(sandbox, ctx):
    (sandbox.assets_dir / "lore.txt").write_text("the realm of Eldra", encoding="utf-8")
    out = _j(file_tools.read_file, {"path": "assets/lore.txt"}, ctx)
    assert "error" not in out
    assert "Eldra" in out["content"]


def test_read_file_reaches_operator_dropped_reference(sandbox, ctx):
    # An operator drops a rulebook into assets/ out of band — the chara reads it.
    (sandbox.assets_dir / "rules").mkdir()
    (sandbox.assets_dir / "rules" / "dnd.md").write_text("roll 1d20", encoding="utf-8")
    out = _j(file_tools.read_file, {"path": "assets/rules/dnd.md"}, ctx)
    assert "error" not in out and "1d20" in out["content"]


def test_read_file_reads_private_workspace(sandbox, ctx):
    (sandbox.workspace_dir / "note.txt").write_text("mine", encoding="utf-8")
    out = _j(file_tools.read_file, {"path": "note.txt"}, ctx)
    assert "error" not in out and "mine" in out["content"]


# ---------------------------------------------------------------------------
# Writes: assets is read-only; workspace is writable
# ---------------------------------------------------------------------------
def test_write_to_assets_is_refused(sandbox, ctx):
    out = _j(file_tools.write_file, {"path": "assets/hack.txt", "content": "x"}, ctx)
    assert "error" in out and "read-only" in out["error"]
    # Nothing was written to the sibling.
    assert not (sandbox.assets_dir / "hack.txt").exists()


def test_write_to_assets_subdir_is_refused(sandbox, ctx):
    (sandbox.assets_dir / "art").mkdir()
    out = _j(file_tools.write_file, {"path": "assets/art/new.png", "content": "x"}, ctx)
    assert "error" in out and "read-only" in out["error"]
    assert not (sandbox.assets_dir / "art" / "new.png").exists()


def test_patch_into_assets_is_refused(sandbox, ctx):
    (sandbox.assets_dir / "lore.txt").write_text("original", encoding="utf-8")
    out = _j(file_tools.patch, {
        "mode": "replace", "path": "assets/lore.txt",
        "old_string": "original", "new_string": "tampered",
    }, ctx)
    assert "error" in out and "read-only" in out["error"]
    assert (sandbox.assets_dir / "lore.txt").read_text(encoding="utf-8") == "original"


def test_v4a_patch_into_assets_is_refused(sandbox, ctx):
    (sandbox.assets_dir / "lore.txt").write_text("original\n", encoding="utf-8")
    body = (
        "*** Begin Patch\n"
        "*** Update File: assets/lore.txt\n"
        "@@\n"
        "-original\n"
        "+tampered\n"
        "*** End Patch\n"
    )
    out = _j(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert "error" in out and "read-only" in out["error"]
    assert (sandbox.assets_dir / "lore.txt").read_text(encoding="utf-8") == "original\n"


def test_v4a_move_into_assets_is_refused(sandbox, ctx):
    """A V4A `Move File: src -> assets/dst` is refused with the friendly read-only
    message (the Move header is covered by the same scan as Update/Add/Delete)."""
    (sandbox.workspace_dir / "note.txt").write_text("hi", encoding="utf-8")
    body = (
        "*** Begin Patch\n"
        "*** Move File: note.txt -> assets/note.txt\n"
        "*** End Patch\n"
    )
    out = _j(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert "error" in out and "read-only" in out["error"]
    assert not (sandbox.assets_dir / "note.txt").exists()
    assert (sandbox.workspace_dir / "note.txt").is_file()  # source untouched


def test_v4a_move_traversal_in_dest_is_refused(sandbox, ctx):
    """A `..` traversal in a Move destination is caught by the header guard."""
    body = (
        "*** Begin Patch\n"
        "*** Move File: note.txt -> ../escape.txt\n"
        "*** End Patch\n"
    )
    out = _j(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert "error" in out and "traversal" in out["error"].lower()
    assert not (sandbox.root / "escape.txt").exists()


def test_write_to_works_shelf_succeeds(sandbox, ctx):
    out = _j(file_tools.write_file, {"path": "works/poem.md", "content": "# hi"}, ctx)
    assert "error" not in out
    assert (sandbox.workspace_dir / "works" / "poem.md").is_file()


# ---------------------------------------------------------------------------
# Escape attempts — absolute, traversal, symlink
# ---------------------------------------------------------------------------
def test_absolute_path_write_is_refused(sandbox, ctx, tmp_path):
    target = tmp_path / "outside.txt"
    out = _j(file_tools.write_file, {"path": str(target), "content": "x"}, ctx)
    assert "error" in out
    assert not target.exists()


def test_traversal_write_is_refused(sandbox, ctx):
    out = _j(file_tools.write_file, {"path": "../escape.txt", "content": "x"}, ctx)
    assert "error" in out
    assert not (sandbox.root / "escape.txt").exists()


def test_traversal_from_assets_into_workspace_does_not_write_assets(sandbox, ctx):
    # assets/../ lands back in the sandbox root (sibling of workspace) → escape.
    out = _j(file_tools.write_file, {"path": "assets/../sneak.txt", "content": "x"}, ctx)
    assert "error" in out
    assert not (sandbox.root / "sneak.txt").exists()


def test_symlink_out_of_assets_is_not_readable(sandbox, ctx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    link = sandbox.assets_dir / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    out = _j(file_tools.read_file, {"path": "assets/link.txt"}, ctx)
    # The symlink resolves outside both workspace and assets → refused.
    assert "error" in out
    assert "classified" not in json.dumps(out)


# ---------------------------------------------------------------------------
# Sandbox.resolve_readable (send_file / image vision path)
# ---------------------------------------------------------------------------
def test_resolve_readable_maps_assets_prefix(sandbox):
    p = sandbox.resolve_readable("assets/sprite.png")
    assert p == (sandbox.assets_dir / "sprite.png").resolve()


def test_resolve_readable_workspace_default(sandbox):
    p = sandbox.resolve_readable("works/out.png")
    assert p == (sandbox.workspace_dir / "works" / "out.png").resolve()


def test_resolve_readable_rejects_absolute(sandbox):
    with pytest.raises(SandboxViolation):
        sandbox.resolve_readable("/etc/passwd")


def test_resolve_readable_rejects_traversal(sandbox):
    with pytest.raises(SandboxViolation):
        sandbox.resolve_readable("assets/../../etc/passwd")


# ---------------------------------------------------------------------------
# search confinement — assets readable, escapes refused
# ---------------------------------------------------------------------------
def test_search_confine_allows_assets(ctx):
    resolved, err = search._confine(ctx, "assets")
    assert err is None
    assert resolved == str(ctx.assets.resolve())


def test_search_confine_allows_workspace_default(ctx):
    resolved, err = search._confine(ctx, ".")
    assert err is None
    assert resolved == str(ctx.workspace.resolve())


def test_search_confine_refuses_escape(ctx):
    resolved, err = search._confine(ctx, "/etc")
    assert resolved is None and err
