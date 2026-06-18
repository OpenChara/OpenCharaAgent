"""File tools: ``read_file``, ``write_file``, ``patch`` (ported from
hermes-agent ``tools/file_tools.py``, apple-to-apple schema/behavior).

Re-anchored to LunaMoth: paths resolve under ``ctx.workspace`` (the chara's
sandbox workspace) plus any operator-opted-in ``writable_paths``; hermes'
multi-agent file-state registry / cross-profile / worktree-cwd machinery is
dropped (one chara per process), but the report-the-resolved-absolute-path
behavior and the per-tool result cap (100K, registry-level) are preserved.

Loop guards: this port keeps the patch consecutive-failure escalating hint and
the read-loop block (≥4 identical reads), per-ctx ephemeral state stashed on
``ctx._scratch`` (no cross-agent registry needed for one process).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..registry import registry, tool_error
from ._fileops import FileOps
from ._pathsec import has_traversal_component


# ---------------------------------------------------------------------------
# ctx helpers
# ---------------------------------------------------------------------------
def _fileops(ctx) -> FileOps:
    writable = []
    try:
        writable = ctx.writable_paths()
    except Exception:
        writable = []
    assets_dir = getattr(ctx, "assets", None)
    return FileOps(ctx.workspace, writable, assets_dir=assets_dir)


def _read_tracker(ctx) -> dict:
    return ctx._scratch.setdefault("_file_read_tracker", {"last_key": None, "consecutive": 0})


def _patch_failures(ctx) -> dict:
    return ctx._scratch.setdefault("_patch_failures", {})


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})

_DEFAULT_MAX_READ_CHARS = 100_000


def read_file(args: dict, ctx) -> str:
    path = args.get("path", "")
    offset = args.get("offset", 1)
    limit = args.get("limit", 500)

    if path in _BLOCKED_DEVICE_PATHS:
        return json.dumps({
            "error": (
                f"Cannot read '{path}': this is a device file that would "
                "block or produce infinite output."
            ),
        }, ensure_ascii=False)

    fops = _fileops(ctx)
    result = fops.read_file(path, offset, limit)
    result_dict = result.to_dict()

    # An image can't be read as text, and there is no in-context image vision for
    # the chara yet (no vision_analyze tool exists — pointing at one was a dead
    # end). Tell the truth about what IS possible instead of sending the model
    # chasing a phantom tool.
    if result_dict.get("is_image"):
        return json.dumps({
            "is_image": True,
            "path": path,
            "file_size": result_dict.get("file_size", 0),
            "note": (
                "This is an image — it can't be read as text, and you can't inspect "
                "its pixels here. You CAN show it to your user by writing a line "
                "MEDIA:<path> in your reply. "
                "(Images under assets/ are your card's reference visuals, already "
                "described in your visual set.)"
            ),
        }, ensure_ascii=False)

    content_len = len(result.content or "")
    if content_len > _DEFAULT_MAX_READ_CHARS:
        total_lines = result_dict.get("total_lines", "unknown")
        return json.dumps({
            "error": (
                f"Read produced {content_len:,} characters which exceeds "
                f"the safety limit ({_DEFAULT_MAX_READ_CHARS:,} chars). "
                "Use offset and limit to read a smaller range. "
                f"The file has {total_lines} lines total."
            ),
            "path": path,
            "total_lines": total_lines,
            "file_size": result_dict.get("file_size", 0),
        }, ensure_ascii=False)

    # Consecutive-loop detection: ≥4 identical reads in a row → hard block.
    read_key = ("read", path, offset, limit)
    tracker = _read_tracker(ctx)
    if tracker["last_key"] == read_key:
        tracker["consecutive"] += 1
    else:
        tracker["last_key"] = read_key
        tracker["consecutive"] = 1
    count = tracker["consecutive"]
    if count >= 4 and not result_dict.get("error"):
        return json.dumps({
            "error": (
                f"BLOCKED: You have read this exact file region {count} times in a row. "
                "The content has NOT changed. You already have this information. "
                "Proceed with your task using the earlier read_file result."
            ),
            "path": path,
            "already_read": count,
        }, ensure_ascii=False)

    return json.dumps(result_dict, ensure_ascii=False)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
def _assets_readonly_error(ctx, fops, path: str):
    """assets/ is the read-only reference shelf (card art + operator-dropped
    reference material), a SIBLING of the workspace. Refuse writes/edits that
    target it (reads and MEDIA:<path> surfacing still work). Returns a tool_error string when
    blocked, else None. The mapping honors the virtual ``assets/`` prefix, so a
    write to ``assets/x`` is caught here before the resolver's hard PathEscape."""
    try:
        assets = getattr(fops, "assets_dir", None)
        if assets is None:
            return None
        assets = Path(assets).resolve()
        mapped = fops._map(path)
        if mapped == assets or assets in mapped.parents:
            return tool_error(
                "assets/ is your read-only reference shelf (your card's visuals plus "
                "any reference material your user placed there) — it can't be written "
                "to or modified. Keep your own work in your workspace (put things to "
                "show your user under works/); you can still read these files and show "
                "them by writing a line MEDIA:<path> in your reply."
            )
    except Exception:
        return None
    return None


def write_file(args: dict, ctx) -> str:
    path = args.get("path")
    if not path or not isinstance(path, str):
        return tool_error(
            "write_file: missing required field 'path'. Re-emit the tool call with "
            "both 'path' and 'content' set."
        )
    if "content" not in args:
        return tool_error(
            "write_file: missing required field 'content'. The tool call included a "
            "path but no content argument — this is almost always a dropped-arg bug "
            "under context pressure. Re-emit the tool call with the full content "
            "payload, or use execute_code for very large files."
        )
    content = args["content"]
    if not isinstance(content, str):
        return tool_error(
            f"write_file: 'content' must be a string, got {type(content).__name__}."
        )

    fops = _fileops(ctx)
    blocked = _assets_readonly_error(ctx, fops, path)
    if blocked:
        return blocked
    result = fops.write_file(path, content)
    result_dict = result.to_dict()
    # Report the absolute path actually written (mis-resolution is visible).
    try:
        if not result_dict.get("error"):
            resolved = str(fops._resolve(path))
            result_dict["resolved_path"] = resolved
            result_dict["files_modified"] = [resolved]
    except Exception:
        pass
    return json.dumps(result_dict, ensure_ascii=False)


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------
import re as _re

_V4A_HEADER_RE = _re.compile(r'^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$', _re.MULTILINE)
# Move headers name TWO paths (`*** Move File: src -> dst`); both must pass the
# same traversal + assets-readonly checks as the single-path headers.
_V4A_MOVE_RE = _re.compile(r'^\*\*\*\s+Move\s+File:\s*(.+?)\s*->\s*(.+)$', _re.MULTILINE)


def _v4a_header_paths(patch_body: str) -> list[str]:
    """Every file path named in a V4A header: Update/Add/Delete (one path each)
    plus Move (both src and dst). One source of truth so the traversal guard and
    the assets-readonly pre-check cover Move exactly like the others."""
    paths = [m.group(1).strip() for m in _V4A_HEADER_RE.finditer(patch_body)]
    for m in _V4A_MOVE_RE.finditer(patch_body):
        paths.append(m.group(1).strip())
        paths.append(m.group(2).strip())
    return paths


def patch(args: dict, ctx) -> str:
    mode = args.get("mode", "replace")
    path = args.get("path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = args.get("replace_all", False)
    patch_body = args.get("patch")

    # V4A header traversal guard (V4A headers only; explicit path= exempt).
    if mode == "patch" and patch_body:
        for v4a_path in _v4a_header_paths(patch_body):
            if has_traversal_component(v4a_path):
                return tool_error(
                    f"V4A patch header contains '..' traversal: {v4a_path!r}. "
                    "Use the agent's cwd-relative path (no '..') or an absolute path "
                    "in '*** Update/Add/Delete/Move File:' headers."
                )

    try:
        fops = _fileops(ctx)

        if mode == "replace":
            if not path:
                return tool_error("path required")
            if old_string is None or new_string is None:
                return tool_error("old_string and new_string required")
            blocked = _assets_readonly_error(ctx, fops, path)
            if blocked:
                return blocked
            result = fops.patch_replace(path, old_string, new_string, replace_all)
        elif mode == "patch":
            if not patch_body:
                return tool_error("patch content required")
            for p in _v4a_header_paths(patch_body):
                blocked = _assets_readonly_error(ctx, fops, p)
                if blocked:
                    return blocked
            result = fops.patch_v4a(patch_body)
        else:
            return tool_error(f"Unknown mode: {mode}")

        result_dict = result.to_dict()

        # Report resolved absolute path(s) for replace mode.
        if not result_dict.get("error") and mode == "replace" and path:
            try:
                resolved = str(fops._resolve(path))
                result_dict["files_modified"] = [resolved]
                result_dict["resolved_path"] = resolved
            except Exception:
                pass

        # No-match escalation hint (consecutive-failure counter per path).
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            failure_count = 0
            if mode == "replace" and path:
                failures = _patch_failures(ctx)
                failures[path] = failures.get(path, 0) + 1
                failure_count = failures[path]

            if failure_count >= 3:
                result_dict["_hint"] = (
                    f"This is failure #{failure_count} patching {path!r}. "
                    "Stop retrying with variations of the same old_string. "
                    "Either: (1) re-read the file fresh to verify current "
                    "content, (2) use a longer / more unique old_string with "
                    "surrounding context lines, or (3) use write_file to "
                    "replace the entire file if the targeted region is hard "
                    "to anchor."
                )
            elif "Did you mean one of these sections?" not in str(result_dict["error"]):
                result_dict["_hint"] = (
                    "old_string not found. Use read_file to verify the current "
                    "content, or search_files to locate the text."
                )
        elif not result_dict.get("error") and mode == "replace" and path:
            # Successful patch resets the per-path failure counter.
            _patch_failures(ctx).pop(path, None)

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


# ---------------------------------------------------------------------------
# Schemas (verbatim from hermes file_tools.py — the model is post-trained on these)
# ---------------------------------------------------------------------------
READ_FILE_SCHEMA = {
    "description": "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are rejected; use offset and limit to read specific sections of large files. NOTE: For an IMAGE, when your model can see images it is shown to you directly (you can then describe or use it); otherwise the image is left on disk and you can still show it to your user by writing a line MEDIA:<path> in your reply. Cannot read other binary files as text.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 500, max: 2000)", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA = {
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
        },
        "required": ["path", "content"],
    },
}

PATCH_SCHEMA = {
    "description": (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
        "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
        "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
        "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
        "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
        "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
        "REQUIRED PARAMETERS: mode, patch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace", "patch"],
                "description": "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.",
                "default": "replace",
            },
            "path": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. File path to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness.",
            },
            "new_string": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. Replacement text. Pass empty string '' to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match (default: false)",
                "default": False,
            },
            "patch": {
                "type": "string",
                "description": "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch",
            },
        },
        "required": ["mode"],
    },
}


def _check_file_reqs() -> bool:
    """Preflight gate: the workspace FS seam is always available in-process."""
    return True


registry.register(
    "read_file", "file", READ_FILE_SCHEMA, read_file,
    check_fn=_check_file_reqs, emoji="📖", max_result_size_chars=100_000,
)
registry.register(
    "write_file", "file", WRITE_FILE_SCHEMA, write_file,
    check_fn=_check_file_reqs, emoji="✍️", max_result_size_chars=100_000,
)
registry.register(
    "patch", "file", PATCH_SCHEMA, patch,
    check_fn=_check_file_reqs, emoji="🔧", max_result_size_chars=100_000,
)
