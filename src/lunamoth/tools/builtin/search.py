"""search_files — unified grep + glob/find (apple-to-apple port of hermes-agent
``search_files``: reference/hermes-agent/tools/file_tools.py:1345-1412,1511-1580 +
tools/file_operations.py:1864-2292).

Re-anchored to LunaMoth's runtime: paths confine under ``ctx.workspace``, shell
commands run through ``ctx.run_terminal`` (sandbox/admin isolation). The
*behaviors* (rg flag-building per output_mode, context lines, file_glob filter,
``set -o pipefail``, exit==2-only error guard, mtime-desc file sort, 500-char
content truncation, JSON ``line`` key, loop guard, truncation hint) are preserved
verbatim. A pure-Python ``re``/``glob`` fallback degrades (with a clear note) when
neither ripgrep nor grep/find is present in the sandbox.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
from pathlib import Path

from ..registry import registry, tool_error
from ._pathsec import map_virtual_assets
from ._search_shell import (
    escape_shell_arg,
    parse_search_context_line,
    run_capturing_rc,
    split_tool_diagnostics,
)

# ---------------------------------------------------------------------------
# Schema (hermes file_tools.py:1511-1528, verbatim)
# ---------------------------------------------------------------------------
SEARCH_FILES_SCHEMA = {
    "description": (
        "Search file contents or find files by name. Use this instead of "
        "grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell "
        "equivalents.\n\nContent search (target='content'): Regex search inside "
        "files. Output modes: full matches with line numbers, file paths only, "
        "or match counts.\n\nFile search (target='files'): Find files by glob "
        "pattern (e.g., '*.py', '*config*'). Also use this instead of ls — "
        "results sorted by modification time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "description": "'content' searches inside file contents, 'files' searches for files by name", "default": "content"},
            "path": {"type": "string", "description": "Directory or file to search in (default: current working directory)", "default": "."},
            "file_glob": {"type": "string", "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"},
            "limit": {"type": "integer", "description": "Maximum number of results to return (default: 50)", "default": 50},
            "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)", "default": 0},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", "default": "content"},
            "context": {"type": "integer", "description": "Number of context lines before and after each match (grep mode only)", "default": 0},
        },
        "required": ["pattern"],
    },
}

_RG_INSTALL = "https://github.com/BurntSushi/ripgrep#installation"

# ---------------------------------------------------------------------------
# Pagination normalizer (hermes file_operations.py:631-663)
# ---------------------------------------------------------------------------

def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_search_pagination(offset=0, limit=50) -> tuple[int, int]:
    return max(0, _coerce_int(offset, 0)), max(1, _coerce_int(limit, 50))


# ---------------------------------------------------------------------------
# Consecutive-search loop guard (hermes file_tools.py:1356-1402). Per-process
# (one chara per process, hermes keys by task_id — there is one).
# ---------------------------------------------------------------------------
_loop_lock = threading.Lock()
_loop_state: dict = {"last_key": None, "consecutive": 0}


def _bump_loop(key) -> int:
    with _loop_lock:
        if _loop_state["last_key"] == key:
            _loop_state["consecutive"] += 1
        else:
            _loop_state["last_key"] = key
            _loop_state["consecutive"] = 1
        return _loop_state["consecutive"]


# ---------------------------------------------------------------------------
# SearchResult / SearchMatch (hermes file_operations.py:228-261)
# ---------------------------------------------------------------------------

class SearchMatch:
    __slots__ = ("path", "line_number", "content")

    def __init__(self, path: str, line_number: int, content: str):
        self.path = path
        self.line_number = line_number
        self.content = content


class SearchResult:
    def __init__(self, matches=None, files=None, counts=None, total_count=0,
                 truncated=False, error=None):
        self.matches = matches or []
        self.files = files or []
        self.counts = counts or {}
        self.total_count = total_count
        self.truncated = truncated
        self.error = error

    def to_dict(self) -> dict:
        result: dict = {"total_count": self.total_count}
        if self.matches:
            result["matches"] = [
                {"path": m.path, "line": m.line_number, "content": m.content}
                for m in self.matches
            ]
        if self.files:
            result["files"] = self.files
        if self.counts:
            result["counts"] = self.counts
        if self.truncated:
            result["truncated"] = True
        if self.error:
            result["error"] = self.error
        return result


# ---------------------------------------------------------------------------
# Path confinement — re-anchored from hermes $TERMINAL_CWD to ctx.workspace.
# A model-supplied path resolves under the workspace; an escape (or a path under
# no writable allowlist entry) is an error.
# ---------------------------------------------------------------------------

def _confine(ctx, path: str) -> tuple[str | None, str | None]:
    """Resolve *path* under ctx.workspace (or the read-only assets sibling).

    Search is read-only, so it may also scan the ``assets/`` reference shelf: a
    leading ``assets`` component maps to the sibling assets dir (the same virtual
    prefix the file tools use). Returns (resolved_abs, error). NOTE: under Linux
    sandbox isolation the OS jail binds only the workspace, so a shelled rg/grep
    won't see assets there — read_file and MEDIA:<path> surfacing (direct, unjailed I/O) remain
    the portable way to reach assets; assets scanning works under macOS sandbox
    (reads are unrestricted) and is bound read-only for bwrap (isolation.py)."""
    workspace = Path(ctx.workspace).resolve()
    assets_dir = getattr(ctx, "assets", None)
    try:
        assets_dir = Path(assets_dir).resolve() if assets_dir else None
    except Exception:  # noqa: BLE001
        assets_dir = None
    raw = (path or ".").strip()
    # Same virtual-prefix mapping the file tools use (leading `assets/` → the
    # read-only sibling, else workspace-relative, absolute as-is). One source of
    # truth for the convention; the read-allowed containment check stays here.
    p = Path(raw).expanduser()
    resolved = map_virtual_assets(p, workspace, assets_dir).resolve()
    if resolved == workspace or workspace in resolved.parents:
        return str(resolved), None
    if assets_dir is not None and (resolved == assets_dir or assets_dir in resolved.parents):
        return str(resolved), None
    # Allow paths under any runtime writable allowlist entry.
    for w in ctx.writable_paths():
        try:
            wr = Path(w).resolve()
        except Exception:  # noqa: BLE001
            continue
        if resolved == wr or wr in resolved.parents:
            return str(resolved), None
    return None, (
        f"Path escapes the workspace: {path!r}. Search paths must resolve under "
        f"the working directory ({workspace})."
    )


# ---------------------------------------------------------------------------
# Command availability (hermes _has_command, cached per process).
# ---------------------------------------------------------------------------
_cmd_cache: dict = {}


def _has_command(ctx, cmd: str) -> bool:
    if cmd not in _cmd_cache:
        res = run_capturing_rc(ctx, f"command -v {cmd} >/dev/null 2>&1 && echo yes")
        if not res.completed:
            # Terminal timed out / refused: fall back to the pure-Python path
            # this probe gates, but DON'T cache — the next call re-probes.
            return False
        _cmd_cache[cmd] = res.stdout.strip().endswith("yes")
    return _cmd_cache[cmd]


def _incomplete(res) -> "SearchResult | None":
    """A run whose RC sentinel never came back must surface as an ERROR — an
    empty stdout from a timed-out/refused command is not '0 matches'."""
    if res.completed:
        return None
    return SearchResult(error=f"Search did not complete: {res.note}", total_count=0)


# ---------------------------------------------------------------------------
# search() dispatch + path-not-found hint (hermes file_operations.py:1864-1925)
# ---------------------------------------------------------------------------

def _search(ctx, pattern, resolved_path, target, file_glob, limit, offset,
            output_mode, context) -> SearchResult:
    p = Path(resolved_path)
    if not p.exists():
        hint_parts = [f"Path not found: {resolved_path}"]
        parent = p.parent
        basename_query = p.name
        if parent.is_dir() and basename_query:
            try:
                entries = sorted(os.listdir(parent))[:20]
            except OSError:
                entries = []
            lower_q = basename_query.lower()
            candidates = []
            for entry in entries:
                le = entry.lower()
                if lower_q in le or le in lower_q or le.startswith(lower_q[:3]):
                    candidates.append(str(parent / entry))
            if candidates:
                hint_parts.append("Similar paths: " + ", ".join(candidates[:5]))
        return SearchResult(error=". ".join(hint_parts), total_count=0)

    if target == "files":
        return _search_files(ctx, pattern, resolved_path, limit, offset)
    return _search_content(ctx, pattern, resolved_path, file_glob, limit, offset,
                           output_mode, context)


# ---------------------------------------------------------------------------
# File search — target=files (hermes file_operations.py:1927-2050)
# ---------------------------------------------------------------------------

def _search_files(ctx, pattern, path, limit, offset) -> SearchResult:
    if not pattern.startswith("**/") and "/" not in pattern:
        search_pattern = pattern
    else:
        search_pattern = pattern.split("/")[-1]

    if _has_command(ctx, "rg"):
        return _search_files_rg(ctx, search_pattern, path, limit, offset)
    if not _has_command(ctx, "find"):
        return _search_files_py(pattern, path, limit, offset, degraded=True)

    fetch_limit = limit + offset
    glob = search_pattern
    cmd = (
        f"find {escape_shell_arg(path)} -type f -name {escape_shell_arg(glob)} "
        f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn | head -n {fetch_limit}"
    )
    res = run_capturing_rc(ctx, cmd, timeout=60)
    if (bad := _incomplete(res)) is not None:
        return bad
    if not res.stdout.strip():
        cmd_simple = (
            f"find {escape_shell_arg(path)} -type f -name {escape_shell_arg(glob)} "
            f"2>/dev/null | sort -rn | head -n {fetch_limit}"
        )
        res = run_capturing_rc(ctx, cmd_simple, timeout=60)
        if (bad := _incomplete(res)) is not None:
            return bad
    files = []
    for line in res.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0].replace(".", "").isdigit():
            files.append(parts[1])
        else:
            files.append(line)
    all_files = files
    page = all_files[offset:offset + limit]
    return SearchResult(files=page, total_count=len(all_files),
                        truncated=len(all_files) >= fetch_limit)


def _search_files_rg(ctx, pattern, path, limit, offset) -> SearchResult:
    if "/" not in pattern and not pattern.startswith("*"):
        glob_pattern = f"*{pattern}"
    else:
        glob_pattern = pattern
    fetch_limit = limit + offset
    cmd_sorted = (
        f"rg --files --sortr=modified -g {escape_shell_arg(glob_pattern)} "
        f"{escape_shell_arg(path)} 2>/dev/null | head -n {fetch_limit}"
    )
    res = run_capturing_rc(ctx, cmd_sorted, timeout=60)
    if (bad := _incomplete(res)) is not None:
        return bad
    all_files = [f for f in res.stdout.strip().split("\n") if f]
    if not all_files:
        cmd_plain = (
            f"rg --files -g {escape_shell_arg(glob_pattern)} "
            f"{escape_shell_arg(path)} 2>/dev/null | head -n {fetch_limit}"
        )
        res = run_capturing_rc(ctx, cmd_plain, timeout=60)
        if (bad := _incomplete(res)) is not None:
            return bad
        all_files = [f for f in res.stdout.strip().split("\n") if f]
    page = all_files[offset:offset + limit]
    return SearchResult(files=page, total_count=len(all_files),
                        truncated=len(all_files) >= fetch_limit)


def _search_files_py(pattern, path, limit, offset, degraded=False) -> SearchResult:
    """Pure-Python file search fallback (no rg/find). Glob by name, mtime-desc.
    Degrades visibly — NOT a fake success."""
    if not pattern.startswith("**/") and "/" not in pattern:
        name_glob = pattern
    else:
        name_glob = pattern.split("/")[-1]
    matches: list[tuple[float, str]] = []
    for root, dirs, fnames in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in fnames:
            if fnmatch.fnmatch(fn, name_glob):
                full = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(full)
                except OSError:
                    mt = 0.0
                matches.append((mt, full))
    matches.sort(key=lambda t: t[0], reverse=True)
    all_files = [f for _, f in matches]
    page = all_files[offset:offset + limit]
    res = SearchResult(files=page, total_count=len(all_files),
                       truncated=len(all_files) > offset + limit)
    if degraded and not res.error:
        res.error = ("[degraded: neither ripgrep nor find available in the "
                     "sandbox; used a Python glob fallback (no .gitignore "
                     "awareness, slower). Install ripgrep: " + _RG_INSTALL + "]")
    return res


# ---------------------------------------------------------------------------
# Content search — target=content (hermes file_operations.py:2052-2292)
# ---------------------------------------------------------------------------

def _search_content(ctx, pattern, path, file_glob, limit, offset,
                    output_mode, context) -> SearchResult:
    if _has_command(ctx, "rg"):
        return _search_with_tool(ctx, "rg", pattern, path, file_glob, limit,
                                 offset, output_mode, context)
    if _has_command(ctx, "grep"):
        return _search_with_tool(ctx, "grep", pattern, path, file_glob, limit,
                                 offset, output_mode, context)
    return _search_content_py(pattern, path, file_glob, limit, offset,
                              output_mode, context)


def _build_cmd(tool, pattern, path, file_glob, output_mode, context):
    if tool == "rg":
        cmd_parts = ["rg", "--line-number", "--no-heading", "--with-filename"]
        if context > 0:
            cmd_parts += ["-C", str(context)]
        if file_glob:
            cmd_parts += ["--glob", escape_shell_arg(file_glob)]
    else:  # grep
        cmd_parts = ["grep", "-rnH", "--exclude-dir='.*'"]
        if context > 0:
            cmd_parts += ["-C", str(context)]
        if file_glob:
            cmd_parts += ["--include", escape_shell_arg(file_glob)]
    if output_mode == "files_only":
        cmd_parts.append("-l")
    elif output_mode == "count":
        cmd_parts.append("-c")
    cmd_parts.append(escape_shell_arg(pattern))
    cmd_parts.append(escape_shell_arg(path))
    return cmd_parts


def _search_with_tool(ctx, tool, pattern, path, file_glob, limit, offset,
                      output_mode, context) -> SearchResult:
    cmd_parts = _build_cmd(tool, pattern, path, file_glob, output_mode, context)
    if tool == "rg":
        fetch_limit = limit + offset + 200 if context > 0 else limit + offset
    else:
        fetch_limit = limit + offset + (200 if context > 0 else 0)
    cmd_parts += ["|", "head", "-n", str(fetch_limit)]
    cmd = "set -o pipefail; " + " ".join(cmd_parts)
    res = run_capturing_rc(ctx, cmd, timeout=60)
    if (bad := _incomplete(res)) is not None:
        return bad

    diagnostics, payload = split_tool_diagnostics(res.stdout)
    # rg/grep exit 2 == error. Surface an error ONLY when exit==2 AND no usable
    # payload (a partial error that still matched keeps its matches).
    if res.exit_code == 2 and not payload.strip():
        error_msg = diagnostics.strip() or res.stdout.strip() or "Search error"
        return SearchResult(error=f"Search failed: {error_msg}", total_count=0)

    stdout = payload
    if output_mode == "files_only":
        all_files = [f for f in stdout.strip().split("\n") if f]
        return SearchResult(files=all_files[offset:offset + limit],
                            total_count=len(all_files))
    if output_mode == "count":
        counts: dict = {}
        for line in stdout.strip().split("\n"):
            if ":" in line:
                parts = line.rsplit(":", 1)
                if len(parts) == 2:
                    try:
                        counts[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
        return SearchResult(counts=counts, total_count=sum(counts.values()))

    match_re = re.compile(r"^([A-Za-z]:)?(.*?):(\d+):(.*)$")
    matches = []
    for line in stdout.strip().split("\n"):
        if not line or line == "--":
            continue
        m = match_re.match(line)
        if m:
            matches.append(SearchMatch(
                path=(m.group(1) or "") + m.group(2),
                line_number=int(m.group(3)),
                content=m.group(4)[:500],
            ))
            continue
        if context > 0:
            parsed = parse_search_context_line(line)
            if parsed:
                matches.append(SearchMatch(path=parsed[0], line_number=parsed[1],
                                           content=parsed[2][:500]))
    total = len(matches)
    return SearchResult(matches=matches[offset:offset + limit], total_count=total,
                        truncated=total > offset + limit)


def _search_content_py(pattern, path, file_glob, limit, offset, output_mode,
                       context) -> SearchResult:
    """Pure-Python content search fallback (no rg/grep). Degrades visibly."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return SearchResult(error=f"Search failed: regex parse error: {e}",
                            total_count=0)
    root = Path(path)
    targets: list[Path] = []
    if root.is_file():
        targets = [root]
    else:
        for r, dirs, fnames in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in fnames:
                if file_glob and not fnmatch.fnmatch(fn, file_glob):
                    continue
                targets.append(Path(r) / fn)
    matches: list[SearchMatch] = []
    counts: dict = {}
    files_hit: list[str] = []
    for fp in targets:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.split("\n")
        hit_count = 0
        for i, line in enumerate(lines, start=1):
            if rx.search(line):
                hit_count += 1
                if output_mode == "content":
                    matches.append(SearchMatch(path=str(fp), line_number=i,
                                               content=line[:500]))
        if hit_count:
            counts[str(fp)] = hit_count
            files_hit.append(str(fp))
    note = ("[degraded: neither ripgrep nor grep available in the sandbox; used "
            "a Python re fallback (no .gitignore awareness, context lines "
            "unsupported). Install ripgrep: " + _RG_INSTALL + "]")
    if output_mode == "files_only":
        return SearchResult(files=files_hit[offset:offset + limit],
                            total_count=len(files_hit), error=note)
    if output_mode == "count":
        return SearchResult(counts=counts, total_count=sum(counts.values()),
                            error=note)
    total = len(matches)
    return SearchResult(matches=matches[offset:offset + limit], total_count=total,
                        truncated=total > offset + limit, error=note)


# ---------------------------------------------------------------------------
# Tool entrypoint (hermes search_tool, file_tools.py:1345-1412)
# ---------------------------------------------------------------------------

def search_files(args: dict, ctx) -> str:
    try:
        pattern = args.get("pattern", "")
        # Legacy aliases (hermes _handle_search_files:1574-1576).
        raw_target = args.get("target", "content")
        target = {"grep": "content", "find": "files"}.get(raw_target, raw_target)
        path = args.get("path", ".")
        file_glob = args.get("file_glob")
        output_mode = args.get("output_mode", "content")
        context = _coerce_int(args.get("context", 0), 0)
        offset, limit = normalize_search_pagination(
            args.get("offset", 0), args.get("limit", 50))

        resolved, err = _confine(ctx, path)
        if err:
            return tool_error(err)

        search_key = ("search", pattern, target, str(path), file_glob or "",
                      limit, offset)
        count = _bump_loop(search_key)
        if count >= 4:
            return json.dumps({
                "error": (
                    f"BLOCKED: You have run this exact search {count} times in a "
                    "row. The results have NOT changed. You already have this "
                    "information. STOP re-searching and proceed with your task."
                ),
                "pattern": pattern,
                "already_searched": count,
            }, ensure_ascii=False)

        result = _search(ctx, pattern, resolved, target, file_glob, limit, offset,
                         output_mode, context)
        result_dict = result.to_dict()
        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )
        result_json = json.dumps(result_dict, ensure_ascii=False)
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_json += (
                f"\n\n[Hint: Results truncated. Use offset={next_offset} to see "
                "more, or narrow with a more specific pattern or file_glob.]"
            )
        return result_json
    except Exception as e:  # noqa: BLE001
        return tool_error(str(e))


registry.register(
    "search_files", "files", SEARCH_FILES_SCHEMA, search_files,
    emoji="🔎", max_result_size_chars=100_000,
)
