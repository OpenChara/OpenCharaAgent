"""Tests for builtin/search.py — the unified search_files tool (grep + glob)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lunamoth.tools.builtin import search as search_mod
from lunamoth.tools.builtin.search import search_files
from lunamoth.tools.registry import registry, discover_builtin_tools


HAS_RG = shutil.which("rg") is not None


class FakeCtx:
    """Minimal ToolContext stub: real shell via runner in 'dir' isolation."""

    def __init__(self, workspace: Path):
        self._workspace = workspace.resolve()
        self._writable: list[str] = []

    @property
    def workspace(self) -> Path:
        return self._workspace

    def writable_paths(self) -> list[str]:
        return list(self._writable)

    def run_terminal(self, command: str, *, timeout: int = 60, workdir=None) -> str:
        from lunamoth.tools.runner import run_terminal as _run
        return _run(command, self._workspace, isolation="dir",
                    allow_network=False, writable_paths=self._writable,
                    timeout=timeout)


@pytest.fixture()
def ws(tmp_path) -> Path:
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "a.py").write_text("import os\ndef foo():\n    return 42\n# TODO fix\n")
    (work / "b.py").write_text("class Bar:\n    pass\n# TODO later\n")
    (work / "readme.md").write_text("hello world\nTODO docs\n")
    sub = work / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("x = 1\nTODO nested\n")
    return work


@pytest.fixture()
def ctx(ws) -> FakeCtx:
    # reset per-process loop guard between tests
    search_mod._loop_state["last_key"] = None
    search_mod._loop_state["consecutive"] = 0
    search_mod._cmd_cache.clear()
    return FakeCtx(ws)


# --- registration / discovery ------------------------------------------------

def test_registers():
    discover_builtin_tools()
    assert "search_files" in registry.get_all_tool_names()
    entry = registry.get_entry("search_files")
    assert entry.toolset == "files"
    assert entry.max_result_size_chars == 100_000
    assert entry.schema["parameters"]["required"] == ["pattern"]


def test_schema_matches_hermes():
    sch = search_mod.SEARCH_FILES_SCHEMA
    props = sch["parameters"]["properties"]
    assert props["target"]["enum"] == ["content", "files"]
    assert props["output_mode"]["enum"] == ["content", "files_only", "count"]
    assert props["path"]["default"] == "."
    assert props["limit"]["default"] == 50
    assert "Ripgrep-backed" in sch["description"]


# --- content search ----------------------------------------------------------

def test_content_search_basic(ctx):
    out = json.loads(_strip_hint(search_files({"pattern": "TODO"}, ctx)))
    assert out["total_count"] >= 3
    assert "matches" in out
    for m in out["matches"]:
        assert set(m) == {"path", "line", "content"}
        assert "TODO" in m["content"]


def test_content_files_only(ctx):
    out = json.loads(search_files(
        {"pattern": "TODO", "output_mode": "files_only"}, ctx))
    assert "files" in out
    assert out["total_count"] == len(out["files"])
    assert all(p.endswith((".py", ".md")) for p in out["files"])


def test_content_count_mode(ctx):
    out = json.loads(search_files(
        {"pattern": "TODO", "output_mode": "count"}, ctx))
    assert "counts" in out
    assert out["total_count"] == sum(out["counts"].values())


def test_content_file_glob_filter(ctx):
    out = json.loads(search_files(
        {"pattern": "TODO", "file_glob": "*.md", "output_mode": "files_only"}, ctx))
    assert all(p.endswith(".md") for p in out["files"])


def test_content_no_match_not_error(ctx):
    out = json.loads(search_files({"pattern": "ZZZ_NO_SUCH_TOKEN"}, ctx))
    assert out["total_count"] == 0
    assert "error" not in out


def test_content_context_lines(ctx):
    out = json.loads(_strip_hint(search_files(
        {"pattern": "return 42", "context": 1}, ctx)))
    # context yields neighboring lines too
    assert out["total_count"] >= 1


def test_content_truncation_and_hint(ctx):
    # Content mode caps the fetch at limit+offset rows (head -n), so the
    # truncation flag only fires for FILE search (files use >= fetch_limit).
    # Create enough .py files to exceed a small limit and trip the hint.
    for i in range(5):
        (ctx.workspace / f"extra{i}.py").write_text("z = 1\n")
    raw = search_files(
        {"pattern": "*.py", "target": "files", "limit": 2, "offset": 0}, ctx)
    assert "[Hint: Results truncated. Use offset=2" in raw
    out = json.loads(_strip_hint(raw))
    assert out["truncated"] is True
    assert len(out["files"]) == 2


def test_legacy_alias_grep(ctx):
    out = json.loads(_strip_hint(search_files({"pattern": "TODO", "target": "grep"}, ctx)))
    assert "matches" in out


# --- file search -------------------------------------------------------------

def test_file_search_glob(ctx):
    out = json.loads(_strip_hint(search_files(
        {"pattern": "*.py", "target": "files"}, ctx)))
    assert "files" in out
    assert all(p.endswith(".py") for p in out["files"])
    assert any("a.py" in p for p in out["files"])
    assert any("c.py" in p for p in out["files"])  # nested found


def test_file_search_legacy_find_alias(ctx):
    out = json.loads(_strip_hint(search_files(
        {"pattern": "*.md", "target": "find"}, ctx)))
    assert all(p.endswith(".md") for p in out["files"])


def test_file_search_mtime_desc(ctx):
    # touch b.py to be newest; rg (--sortr=modified) / GNU find (-printf %T@)
    # put it first. BSD/macOS find lacks -printf and falls back to unsorted,
    # so only assert ordering when an mtime-aware backend is available.
    import os
    import time
    bp = ctx.workspace / "b.py"
    os.utime(bp, (time.time() + 10, time.time() + 10))
    out = json.loads(_strip_hint(search_files(
        {"pattern": "*.py", "target": "files"}, ctx)))
    assert out["files"], out
    assert any(p.endswith("b.py") for p in out["files"])
    has_rg = search_mod._has_command(ctx, "rg")
    printf_ok = "1." in ctx.run_terminal(
        "find . -maxdepth 0 -printf '%T@\\n' 2>/dev/null")
    if has_rg or printf_ok:
        assert out["files"][0].endswith("b.py")


# --- path handling -----------------------------------------------------------

def test_path_not_found_hint(ctx):
    out = json.loads(search_files(
        {"pattern": "x", "path": "nope_dir"}, ctx))
    assert "error" in out
    assert "Path not found" in out["error"]
    assert out["total_count"] == 0


def test_path_escape_rejected(ctx):
    out = json.loads(search_files(
        {"pattern": "x", "path": "../../../etc"}, ctx))
    assert "error" in out
    assert "escapes the workspace" in out["error"]


def test_subdir_path_ok(ctx):
    out = json.loads(_strip_hint(search_files(
        {"pattern": "TODO", "path": "sub"}, ctx)))
    assert out["total_count"] >= 1
    assert all("sub" in m["path"] for m in out.get("matches", []))


# --- loop guard --------------------------------------------------------------

def test_loop_guard_blocks_at_4(ctx):
    args = {"pattern": "TODO"}
    for _ in range(3):
        search_files(args, ctx)
    out = json.loads(search_files(args, ctx))
    assert out.get("already_searched") == 4
    assert "BLOCKED" in out["error"]


def test_loop_guard_warns_at_3(ctx):
    args = {"pattern": "TODO"}
    search_files(args, ctx)
    search_files(args, ctx)
    out = json.loads(_strip_hint(search_files(args, ctx)))
    assert "_warning" in out


def test_pagination_resets_loop(ctx):
    # different offset -> different key -> no consecutive bump to block
    search_files({"pattern": "TODO"}, ctx)
    search_files({"pattern": "TODO"}, ctx)
    search_files({"pattern": "TODO"}, ctx)
    out = json.loads(_strip_hint(search_files(
        {"pattern": "TODO", "offset": 0, "limit": 5}, ctx)))
    # limit change makes a fresh key; should not be BLOCKED
    assert "BLOCKED" not in json.dumps(out)


# --- content 500-char truncation ---------------------------------------------

def test_content_truncated_to_500(ctx):
    long_line = "MARK" + "x" * 900
    (ctx.workspace / "long.txt").write_text(long_line + "\n")
    out = json.loads(_strip_hint(search_files({"pattern": "MARK"}, ctx)))
    hit = [m for m in out["matches"] if "long.txt" in m["path"]]
    assert hit
    assert len(hit[0]["content"]) <= 500


# --- python fallbacks (no rg/grep) -------------------------------------------

def test_python_content_fallback(ctx, monkeypatch):
    monkeypatch.setattr(search_mod, "_has_command", lambda c, n: False)
    out = json.loads(_strip_hint(search_files({"pattern": "TODO"}, ctx)))
    assert out["total_count"] >= 3
    assert "degraded" in out.get("error", "")
    assert any("TODO" in m["content"] for m in out["matches"])


def test_python_file_fallback(ctx, monkeypatch):
    monkeypatch.setattr(search_mod, "_has_command", lambda c, n: False)
    out = json.loads(_strip_hint(search_files(
        {"pattern": "*.py", "target": "files"}, ctx)))
    assert "files" in out
    assert all(p.endswith(".py") for p in out["files"])
    assert "degraded" in out.get("error", "")


def test_python_content_bad_regex(ctx, monkeypatch):
    monkeypatch.setattr(search_mod, "_has_command", lambda c, n: False)
    out = json.loads(search_files({"pattern": "[unterminated"}, ctx))
    assert "error" in out and "regex parse error" in out["error"]


# --- helpers -----------------------------------------------------------------

def _strip_hint(raw: str) -> str:
    """Drop the trailing non-JSON truncation hint, if present."""
    idx = raw.rfind("\n\n[Hint:")
    return raw[:idx] if idx != -1 else raw
