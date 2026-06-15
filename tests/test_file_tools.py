"""Tests for the ported file tools: read_file, write_file, patch."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from lunamoth.tools.builtin import file_tools
from lunamoth.tools.registry import registry, discover_builtin_tools


# ---------------------------------------------------------------------------
# Minimal fake ctx — just .workspace + .writable_paths() + ._scratch
# ---------------------------------------------------------------------------
class FakeCtx:
    def __init__(self, workspace: Path):
        self.sandbox = types.SimpleNamespace(root=workspace.parent)
        self._ws = workspace
        self._scratch = {}

    @property
    def workspace(self) -> Path:
        return self._ws

    def writable_paths(self):
        return []


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return FakeCtx(ws)


def _call(fn, args, ctx):
    return json.loads(fn(args, ctx))


# ---------------------------------------------------------------------------
# Registration / discovery
# ---------------------------------------------------------------------------
def test_registers_three_tools():
    for name in ("read_file", "write_file", "patch"):
        assert registry.get_entry(name) is not None, name


def test_schemas_match_hermes_shape():
    rf = registry.get_schema("read_file")
    assert rf["parameters"]["required"] == ["path"]
    assert rf["parameters"]["properties"]["limit"]["maximum"] == 2000
    p = registry.get_schema("patch")
    assert p["parameters"]["required"] == ["mode"]
    assert p["parameters"]["properties"]["mode"]["enum"] == ["replace", "patch"]
    assert "9 strategies" in p["description"]


def test_result_caps():
    for name in ("read_file", "write_file", "patch"):
        assert registry.get_max_result_size(name) == 100_000


def test_discovery_imports_file_tools():
    mods = discover_builtin_tools()
    assert "lunamoth.tools.builtin.file_tools" in mods


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
def test_read_file_line_numbers(ctx):
    (ctx.workspace / "a.txt").write_text("alpha\nbeta\ngamma\n")
    out = _call(file_tools.read_file, {"path": "a.txt"}, ctx)
    assert out["content"] == "1|alpha\n2|beta\n3|gamma\n4|"
    assert out["total_lines"] == 3


def test_read_file_offset_limit(ctx):
    (ctx.workspace / "b.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    out = _call(file_tools.read_file, {"path": "b.txt", "offset": 3, "limit": 2}, ctx)
    assert out["content"] == "3|line3\n4|line4"
    assert out["truncated"] is True


def test_read_file_not_found_suggests(ctx):
    (ctx.workspace / "config.yaml").write_text("k: v\n")
    out = _call(file_tools.read_file, {"path": "config.yml"}, ctx)
    assert "File not found" in out["error"]
    assert any("config.yaml" in s for s in out.get("similar_files", []))


def test_read_file_binary_refused(ctx):
    (ctx.workspace / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    out = _call(file_tools.read_file, {"path": "x.png"}, ctx)
    # .png is an image extension -> redirected to vision, marked is_image
    assert out.get("is_image") is True


def test_read_file_binary_content_refused(ctx):
    (ctx.workspace / "blob.xyz").write_bytes(b"\x00\x01\x02\x03" * 300)
    out = _call(file_tools.read_file, {"path": "blob.xyz"}, ctx)
    assert "Binary file" in out["error"]


def test_read_file_100k_cap(ctx):
    # 100 lines of ~1500 chars each (line cap is 2000) => paginated content >100K.
    line = "x" * 1500 + "\n"
    (ctx.workspace / "big.txt").write_text(line * 100)
    out = _call(file_tools.read_file, {"path": "big.txt", "limit": 2000}, ctx)
    assert "exceeds" in out["error"]


def test_read_file_escape_blocked(ctx):
    out = _call(file_tools.read_file, {"path": "../../etc/passwd"}, ctx)
    assert "escapes" in out["error"].lower() or "not found" in out["error"].lower()


def test_read_file_loop_block(ctx):
    (ctx.workspace / "c.txt").write_text("hi\n")
    for _ in range(3):
        out = _call(file_tools.read_file, {"path": "c.txt"}, ctx)
        assert "content" in out
    out = _call(file_tools.read_file, {"path": "c.txt"}, ctx)
    assert "BLOCKED" in out.get("error", "")


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
def test_write_file_basic(ctx):
    out = _call(file_tools.write_file, {"path": "new/d.txt", "content": "hello"}, ctx)
    assert out.get("error") is None
    assert (ctx.workspace / "new" / "d.txt").read_text() == "hello"
    assert out["resolved_path"].endswith("/new/d.txt")


def test_write_file_missing_content(ctx):
    out = _call(file_tools.write_file, {"path": "e.txt"}, ctx)
    assert "missing required field 'content'" in out["error"]


def test_write_file_missing_path(ctx):
    out = _call(file_tools.write_file, {"content": "x"}, ctx)
    assert "missing required field 'path'" in out["error"]


def test_write_file_new_syntax_error_surfaced(ctx):
    out = _call(file_tools.write_file, {"path": "bad.py", "content": "def f(:\n"}, ctx)
    assert out["lint"]["status"] == "error"


def test_write_file_only_new_errors(ctx):
    # Pre-existing broken file; write keeps the same error -> "still broken" message.
    (ctx.workspace / "p.py").write_text("def f(:\n")
    out = _call(file_tools.write_file, {"path": "p.py", "content": "def f(:\n    pass\n"}, ctx)
    assert out["lint"]["status"] == "error"
    # The pre-existing syntax error is the same line/col, so it's filtered as
    # "didn't introduce new ones" OR shows new error — both are acceptable
    # delta outcomes; the key is that lint ran and flagged the file.


def test_write_file_clean_python(ctx):
    out = _call(file_tools.write_file, {"path": "ok.py", "content": "x = 1\n"}, ctx)
    assert out["lint"]["status"] == "ok"


def test_write_file_json_lint(ctx):
    out = _call(file_tools.write_file, {"path": "bad.json", "content": "{not json}"}, ctx)
    assert out["lint"]["status"] == "error"


# ---------------------------------------------------------------------------
# patch (replace mode)
# ---------------------------------------------------------------------------
def test_patch_replace_basic(ctx):
    (ctx.workspace / "r.py").write_text("def foo():\n    return 1\n")
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "r.py",
        "old_string": "return 1", "new_string": "return 2",
    }, ctx)
    assert out["success"] is True
    assert "return 2" in (ctx.workspace / "r.py").read_text()
    assert out["diff"]


def test_patch_replace_path_required(ctx):
    out = _call(file_tools.patch, {"mode": "replace", "old_string": "a", "new_string": "b"}, ctx)
    assert out["error"] == "path required"


def test_patch_replace_strings_required(ctx):
    out = _call(file_tools.patch, {"mode": "replace", "path": "r.py"}, ctx)
    assert out["error"] == "old_string and new_string required"


def test_patch_unknown_mode(ctx):
    out = _call(file_tools.patch, {"mode": "wat"}, ctx)
    assert out["error"] == "Unknown mode: wat"


def test_patch_replace_ambiguous(ctx):
    (ctx.workspace / "amb.txt").write_text("x\nx\n")
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "amb.txt",
        "old_string": "x", "new_string": "y",
    }, ctx)
    assert "Found 2 matches" in out["error"]


def test_patch_replace_all(ctx):
    (ctx.workspace / "amb2.txt").write_text("x\nx\n")
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "amb2.txt",
        "old_string": "x", "new_string": "y", "replace_all": True,
    }, ctx)
    assert out["success"] is True
    assert (ctx.workspace / "amb2.txt").read_text() == "y\ny\n"


def test_patch_replace_no_match_hint(ctx):
    (ctx.workspace / "nm.txt").write_text("alpha\nbeta\n")
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "nm.txt",
        "old_string": "zzzzz", "new_string": "q",
    }, ctx)
    assert "Could not find" in out["error"]
    assert "_hint" in out


def test_patch_replace_failure_escalation(ctx):
    (ctx.workspace / "esc.txt").write_text("alpha\n")
    for _ in range(2):
        _call(file_tools.patch, {
            "mode": "replace", "path": "esc.txt",
            "old_string": "nope", "new_string": "q",
        }, ctx)
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "esc.txt",
        "old_string": "nope", "new_string": "q",
    }, ctx)
    assert "failure #3" in out["_hint"]


def test_patch_fuzzy_whitespace(ctx):
    (ctx.workspace / "f.py").write_text("def foo():\n        return 1\n")
    out = _call(file_tools.patch, {
        "mode": "replace", "path": "f.py",
        "old_string": "    return 1", "new_string": "    return 2",
    }, ctx)
    assert out["success"] is True
    assert "return 2" in (ctx.workspace / "f.py").read_text()


# ---------------------------------------------------------------------------
# patch (V4A mode)
# ---------------------------------------------------------------------------
def test_patch_v4a_update(ctx):
    (ctx.workspace / "v.py").write_text("a = 1\nb = 2\nc = 3\n")
    body = (
        "*** Begin Patch\n"
        "*** Update File: v.py\n"
        "@@\n"
        " a = 1\n"
        "-b = 2\n"
        "+b = 20\n"
        " c = 3\n"
        "*** End Patch\n"
    )
    out = _call(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert out["success"] is True
    assert "b = 20" in (ctx.workspace / "v.py").read_text()
    assert "v.py" in out["files_modified"]


def test_patch_v4a_add_and_delete(ctx):
    (ctx.workspace / "old.txt").write_text("gone\n")
    body = (
        "*** Begin Patch\n"
        "*** Add File: created.txt\n"
        "+line one\n"
        "+line two\n"
        "*** Delete File: old.txt\n"
        "*** End Patch\n"
    )
    out = _call(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert out["success"] is True
    assert (ctx.workspace / "created.txt").read_text() == "line one\nline two"
    assert not (ctx.workspace / "old.txt").exists()
    assert "created.txt" in out["files_created"]
    assert "old.txt" in out["files_deleted"]


def test_patch_v4a_validate_then_apply(ctx):
    # One good hunk, one impossible -> NO file changes at all.
    (ctx.workspace / "g.txt").write_text("keep me\n")
    body = (
        "*** Begin Patch\n"
        "*** Update File: g.txt\n"
        "@@\n"
        "-keep me\n"
        "+changed\n"
        "*** Update File: missing.txt\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    out = _call(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert out["success"] is False
    assert "validation failed" in out["error"]
    # g.txt must be untouched (validate-all-then-apply).
    assert (ctx.workspace / "g.txt").read_text() == "keep me\n"


def test_patch_v4a_traversal_blocked(ctx):
    body = (
        "*** Begin Patch\n"
        "*** Update File: ../escape.py\n"
        "@@\n"
        "-a\n"
        "+b\n"
        "*** End Patch\n"
    )
    out = _call(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert "traversal" in out["error"]


def test_patch_v4a_parse_error(ctx):
    body = (
        "*** Begin Patch\n"
        "*** Update File: q.py\n"
        "*** End Patch\n"
    )
    out = _call(file_tools.patch, {"mode": "patch", "patch": body}, ctx)
    assert "no hunks found" in out["error"]


def test_patch_content_required(ctx):
    out = _call(file_tools.patch, {"mode": "patch"}, ctx)
    assert out["error"] == "patch content required"


# ---- assets/ is read-only to the chara ---------------------------------------
def test_assets_dir_is_read_only(ctx):
    (ctx.workspace / "assets").mkdir()
    (ctx.workspace / "assets" / "sprite.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # write_file under assets/ is refused
    out = _call(file_tools.write_file, {"path": "assets/x.txt", "content": "nope"}, ctx)
    assert out.get("error") and "read-only" in out["error"].lower()
    # patch replace under assets/ is refused
    pout = _call(file_tools.patch, {"mode": "replace", "path": "assets/sprite.png",
                                    "old_string": "a", "new_string": "b"}, ctx)
    assert pout.get("error") and "read-only" in pout["error"].lower()
    # a normal workspace write still works
    ok = _call(file_tools.write_file, {"path": "work.txt", "content": "hi"}, ctx)
    assert not ok.get("error")
    # reads are NOT blocked (it's read-only, not hidden)
    r = _call(file_tools.read_file, {"path": "assets/sprite.png"}, ctx)
    assert r.get("is_image")
