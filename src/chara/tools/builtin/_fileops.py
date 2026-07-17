"""File-operation machinery for the file tools (ported from hermes-agent
``tools/file_operations.py``, apple-to-apple behavior, re-anchored to OpenCharaAgent).

hermes' ``ShellFileOperations`` shells every read/write/stat through a terminal
backend so it can run on remote/docker/ssh environments. OpenCharaAgent runs one chara
per process with the workspace on the local filesystem (under an OS jail), so
``FileOps`` re-implements the exact same behaviors using direct Python I/O,
confined to the workspace via ``_pathsec``. The output shapes, error strings,
line-ending/BOM preservation, fuzzy chain, V4A flow, and lint-delta semantics are
preserved verbatim — only the path/exec plumbing is re-anchored.

Path confinement: every model-supplied path passes through
``resolve_in_workspace`` (workspace root + opted-in writable paths only).
"""
from __future__ import annotations

import ast
import difflib
import json as _json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._pathsec import (
    PathEscape,
    _resolve_nonexistent,
    expand_user,
    map_virtual_assets,
    resolve_in_workspace,
)

# ---------------------------------------------------------------------------
# Extension sets (hermes binary_extensions.py + file_operations.IMAGE_EXTENSIONS)
# ---------------------------------------------------------------------------
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    ".swf", ".fla",
    ".lockb", ".dat", ".data",
})
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}

MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_FILE_SIZE = 50 * 1024  # 50KB
DEFAULT_READ_OFFSET = 1
DEFAULT_READ_LIMIT = 500
DEFAULT_SEARCH_OFFSET = 0
DEFAULT_SEARCH_LIMIT = 50

_UTF8_BOM = "﻿"


# ---------------------------------------------------------------------------
# Line-ending + BOM helpers (hermes file_operations.py:76-143)
# ---------------------------------------------------------------------------
def _detect_line_ending(sample: str) -> Optional[str]:
    if not sample:
        return None
    head = sample[:4096]
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    lf_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return lf_normalized
    if target == "\r\n":
        return lf_normalized.replace("\n", "\r\n")
    return text


def _strip_bom(text: str) -> tuple[str, bool]:
    if text and text.startswith(_UTF8_BOM):
        return text[len(_UTF8_BOM):], True
    return text, False


def _has_bom(text: Optional[str]) -> bool:
    return bool(text) and text.startswith(_UTF8_BOM)


# ---------------------------------------------------------------------------
# Pagination normalizers (hermes file_operations.py:631-663)
# ---------------------------------------------------------------------------
def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_read_pagination(offset: Any = DEFAULT_READ_OFFSET,
                              limit: Any = DEFAULT_READ_LIMIT) -> tuple[int, int]:
    normalized_offset = max(1, _coerce_int(offset, DEFAULT_READ_OFFSET))
    normalized_limit = _coerce_int(limit, DEFAULT_READ_LIMIT)
    normalized_limit = max(1, min(normalized_limit, MAX_LINES))
    return normalized_offset, normalized_limit


def normalize_search_pagination(offset: Any = DEFAULT_SEARCH_OFFSET,
                                limit: Any = DEFAULT_SEARCH_LIMIT) -> tuple[int, int]:
    normalized_offset = max(0, _coerce_int(offset, DEFAULT_SEARCH_OFFSET))
    normalized_limit = max(1, _coerce_int(limit, DEFAULT_SEARCH_LIMIT))
    return normalized_offset, normalized_limit


# ---------------------------------------------------------------------------
# In-process linters (hermes file_operations.py:542-619)
# ---------------------------------------------------------------------------
def _lint_json_inproc(content: str) -> tuple[bool, str]:
    try:
        _json.loads(content)
        return True, ""
    except _json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg} (line {e.lineno}, column {e.colno})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_yaml_inproc(content: str) -> tuple[bool, str]:
    try:
        import yaml as _yaml
    except ImportError:
        return True, "__SKIP__"
    try:
        _yaml.safe_load(content)
        return True, ""
    except _yaml.YAMLError as e:
        return False, f"YAMLError: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_toml_inproc(content: str) -> tuple[bool, str]:
    try:
        import tomllib as _toml
    except ImportError:
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except ImportError:
            return True, "__SKIP__"
    try:
        _toml.loads(content)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _lint_python_inproc(content: str) -> tuple[bool, str]:
    try:
        ast.parse(content)
        return True, ""
    except SyntaxError as e:
        loc = f" (line {e.lineno}, column {e.offset})" if e.lineno else ""
        return False, f"{type(e).__name__}: {e.msg}{loc}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


LINTERS_INPROC = {
    '.py': _lint_python_inproc,
    '.json': _lint_json_inproc,
    '.yaml': _lint_yaml_inproc,
    '.yml': _lint_yaml_inproc,
    '.toml': _lint_toml_inproc,
}


# ---------------------------------------------------------------------------
# Result data classes (hermes file_operations.py:155-278)
# ---------------------------------------------------------------------------
@dataclass
class ReadResult:
    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: Optional[str] = None
    is_binary: bool = False
    is_image: bool = False
    error: Optional[str] = None
    similar_files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != [] and v is not False}


@dataclass
class WriteResult:
    bytes_written: int = 0
    dirs_created: bool = False
    lint: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class PatchResult:
    success: bool = False
    diff: str = ""
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    lint: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        result = {"success": self.success}
        if self.diff:
            result["diff"] = self.diff
        if self.files_modified:
            result["files_modified"] = self.files_modified
        if self.files_created:
            result["files_created"] = self.files_created
        if self.files_deleted:
            result["files_deleted"] = self.files_deleted
        if self.lint:
            result["lint"] = self.lint
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class LintResult:
    success: bool = True
    skipped: bool = False
    output: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "message": self.message}
        result = {"status": "ok" if self.success else "error", "output": self.output}
        if self.message:
            result["message"] = self.message
        return result


# ---------------------------------------------------------------------------
# FileOps — direct-IO re-implementation of ShellFileOperations
# ---------------------------------------------------------------------------
class FileOps:
    """Workspace-confined file operations.

    All model-supplied paths resolve through ``_resolve`` (workspace +
    writable paths). hermes' ``_expand_path`` (``echo $HOME``) is folded into
    ``_pathsec.expand_user`` and applied at resolution time.
    """

    def __init__(self, workspace: Path, writable_paths: Optional[List[str]] = None,
                 assets_dir: Optional[Path] = None):
        self.workspace = Path(workspace)
        self.writable_paths = list(writable_paths or [])
        # The read-only reference sibling (``sandbox/assets``). Reads may resolve
        # into it (and the virtual ``assets/`` prefix maps here); writes never.
        self.assets_dir = Path(assets_dir) if assets_dir else None

    # ---- path resolution ----
    def _resolve(self, path: str, *, readable: bool = False) -> Path:
        """Resolve a model path. ``readable=True`` (read_file/read_raw) admits the
        read-only assets sibling as a valid root; the default (writes) does not,
        so a write into assets/ fails confinement."""
        return resolve_in_workspace(
            path, self.workspace, self.writable_paths,
            assets_dir=self.assets_dir, readable=readable,
        )

    def _map(self, path: str) -> Path:
        """The virtual-mapped absolute candidate for *path* WITHOUT confinement —
        used only to detect whether a path targets the read-only assets shelf
        (for the friendly write-refusal). Honors the ``assets/`` prefix and
        ``~`` expansion; resolves symlinks/.. so the check can't be dodged."""
        ws = Path(self.workspace).resolve()
        ad = Path(self.assets_dir).resolve() if self.assets_dir else None
        p = Path(expand_user(path, ws))
        return Path(_resolve_nonexistent(map_virtual_assets(p, ws, ad)))

    # ---- detection helpers ----
    @staticmethod
    def _is_image(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS

    @staticmethod
    def _is_likely_binary(path: str, content_sample: Optional[bytes] = None) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True
        if content_sample:
            sample = content_sample[:1000]
            non_printable = sum(
                1 for b in sample
                if b < 32 and b not in (9, 10, 13)
            )
            if not sample:
                return False
            return non_printable / min(len(sample), 1000) > 0.30
        return False

    @staticmethod
    def _add_line_numbers(content: str, start_line: int = 1) -> str:
        lines = content.split('\n')
        numbered = []
        for i, line in enumerate(lines, start=start_line):
            if len(line) > MAX_LINE_LENGTH:
                line = line[:MAX_LINE_LENGTH] + "... [truncated]"
            numbered.append(f"{i}|{line}")
        return '\n'.join(numbered)

    @staticmethod
    def _unified_diff(old_content: str, new_content: str, filename: str) -> str:
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
        return ''.join(diff)

    # ---- similar-file suggestion (hermes _suggest_similar_files) ----
    def _suggest_similar_files(self, resolved: Path, orig_path: str) -> ReadResult:
        dir_path = resolved.parent
        filename = resolved.name
        basename_no_ext = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        lower_name = filename.lower()

        scored: list = []
        try:
            entries = sorted(p.name for p in dir_path.iterdir())[:50]
        except OSError:
            entries = []
        for f in entries:
            lf = f.lower()
            score = 0
            if lf == lower_name:
                score = 100
            elif os.path.splitext(f)[0].lower() == basename_no_ext.lower():
                score = 90
            elif lf.startswith(lower_name) or lower_name.startswith(lf):
                score = 70
            elif lower_name in lf:
                score = 60
            elif lf in lower_name and len(lf) > 2:
                score = 40
            elif ext and os.path.splitext(f)[1].lower() == ext:
                common = set(lower_name) & set(lf)
                if len(common) >= max(len(lower_name), len(lf)) * 0.4:
                    score = 30
            if score > 0:
                scored.append((score, str(dir_path / f)))

        scored.sort(key=lambda x: -x[0])
        similar = [p for _, p in scored[:5]]
        err = f"File not found: {orig_path}"
        return ReadResult(error=err, similar_files=similar)

    # ---- READ ----
    def read_file(self, path: str, offset: int = 1, limit: int = 500) -> ReadResult:
        try:
            resolved = self._resolve(path, readable=True)
        except PathEscape as e:
            return ReadResult(error=str(e))

        offset, limit = normalize_read_pagination(offset, limit)

        if not resolved.exists() or not resolved.is_file():
            return self._suggest_similar_files(resolved, path)

        try:
            file_size = resolved.stat().st_size
        except OSError:
            file_size = 0

        if self._is_image(str(resolved)):
            return ReadResult(
                is_image=True,
                is_binary=True,
                file_size=file_size,
                hint=(
                    "Image file — cannot be read as text. You can show it to your user "
                    "by writing a line MEDIA:<path> in your reply, but cannot inspect "
                    "its pixels here."
                ),
            )

        try:
            raw_bytes = resolved.read_bytes()
        except OSError as e:
            return ReadResult(error=f"Failed to read file: {e}")

        if self._is_likely_binary(str(resolved), raw_bytes[:1000]):
            return ReadResult(
                is_binary=True,
                file_size=file_size,
                error="Binary file - cannot display as text. Use appropriate tools to handle this file type.",
            )

        text = raw_bytes.decode("utf-8", errors="replace")
        text, _ = _strip_bom(text)
        all_lines = text.split('\n')
        # ``wc -l`` counts newline chars; a file ending in \n has a trailing
        # empty split element. Match hermes total_lines semantics.
        total_lines = text.count('\n')

        end_line = offset + limit - 1
        page = all_lines[offset - 1:end_line]
        read_output = '\n'.join(page)
        if offset == 1:
            read_output, _ = _strip_bom(read_output)

        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = f"Use offset={end_line + 1} to continue reading (showing {offset}-{end_line} of {total_lines} lines)"

        return ReadResult(
            content=self._add_line_numbers(read_output, offset),
            total_lines=total_lines,
            file_size=file_size,
            truncated=truncated,
            hint=hint,
        )

    def read_file_raw(self, path: str) -> ReadResult:
        try:
            resolved = self._resolve(path, readable=True)
        except PathEscape as e:
            return ReadResult(error=str(e))

        if not resolved.exists() or not resolved.is_file():
            return self._suggest_similar_files(resolved, path)

        try:
            file_size = resolved.stat().st_size
        except OSError:
            file_size = 0

        if self._is_image(str(resolved)):
            return ReadResult(is_image=True, is_binary=True, file_size=file_size)

        try:
            raw_bytes = resolved.read_bytes()
        except OSError as e:
            return ReadResult(error=f"Failed to read file: {e}")

        if self._is_likely_binary(str(resolved), raw_bytes[:1000]):
            return ReadResult(
                is_binary=True, file_size=file_size,
                error="Binary file — cannot display as text.",
            )

        text = raw_bytes.decode("utf-8", errors="replace")
        raw_content, _ = _strip_bom(text)
        return ReadResult(content=raw_content, file_size=file_size)

    # ---- WRITE ----
    def _detect_file_line_ending(self, resolved: Path, pre_content: Optional[str]) -> Optional[str]:
        if pre_content is not None:
            return _detect_line_ending(pre_content)
        try:
            sample = resolved.read_bytes()[:4096].decode("utf-8", errors="replace")
        except OSError:
            return None
        return _detect_line_ending(sample)

    def _file_has_bom(self, resolved: Path, pre_content: Optional[str]) -> bool:
        if pre_content is not None:
            return _has_bom(pre_content)
        try:
            head = resolved.read_bytes()[:3]
        except OSError:
            return False
        return head == b"\xef\xbb\xbf"

    def write_file(self, path: str, content: str) -> WriteResult:
        try:
            resolved = self._resolve(path)
        except PathEscape as e:
            return WriteResult(error=f"Write denied: {e}")

        ext = resolved.suffix.lower()
        pre_content: Optional[str] = None
        if ext in LINTERS_INPROC and resolved.exists():
            try:
                pre_content = resolved.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                pre_content = None

        original_ending = self._detect_file_line_ending(resolved, pre_content)
        if original_ending == "\r\n":
            content = _normalize_line_endings(content, "\r\n")

        if self._file_has_bom(resolved, pre_content) and not _has_bom(content):
            content = _UTF8_BOM + content

        dirs_created = False
        parent = resolved.parent
        if parent and not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
                dirs_created = True
            except OSError as e:
                return WriteResult(error=f"Failed to write file: {e}")
        elif parent:
            dirs_created = True

        # Atomic write: temp file beside target, then rename.
        tmp = resolved.with_name(f".{resolved.name}.chara-tmp")
        try:
            data = content.encode("utf-8")
            tmp.write_bytes(data)
            if resolved.exists():
                try:
                    os.chmod(tmp, resolved.stat().st_mode)
                except OSError:
                    pass
            os.replace(tmp, resolved)
        except OSError as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return WriteResult(error=f"Failed to write file: {e}")

        try:
            bytes_written = resolved.stat().st_size
        except OSError:
            bytes_written = len(content.encode("utf-8"))

        lint_result = self._check_lint_delta(str(resolved), pre_content=pre_content, post_content=content)

        return WriteResult(
            bytes_written=bytes_written,
            dirs_created=dirs_created,
            lint=lint_result.to_dict() if lint_result else None,
        )

    def delete_file(self, path: str) -> WriteResult:
        try:
            resolved = self._resolve(path)
        except PathEscape as e:
            return WriteResult(error=f"Delete denied: {e}")
        try:
            if resolved.is_dir() and not resolved.is_symlink():
                return WriteResult(error=f"is a directory: {resolved}")
            resolved.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            return WriteResult(error=f"Failed to delete {resolved}: {e}")
        return WriteResult()

    def move_file(self, src: str, dst: str) -> WriteResult:
        try:
            src_r = self._resolve(src)
            dst_r = self._resolve(dst)
        except PathEscape as e:
            return WriteResult(error=f"Move denied: {e}")
        try:
            dst_r.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_r, dst_r)
        except OSError as e:
            return WriteResult(error=f"Failed to move {src_r} -> {dst_r}: {e}")
        return WriteResult()

    # ---- PATCH (replace) ----
    def patch_replace(self, path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> PatchResult:
        from ._fuzzy_match import fuzzy_find_and_replace, format_no_match_hint

        try:
            resolved = self._resolve(path)
        except PathEscape as e:
            return PatchResult(error=f"Write denied: {e}")

        if not resolved.exists() or not resolved.is_file():
            return PatchResult(error=f"Failed to read file: {path}")

        try:
            content = resolved.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return PatchResult(error=f"Failed to read file: {path}")
        content, _ = _strip_bom(content)

        new_content, match_count, _strategy, error = fuzzy_find_and_replace(
            content, old_string, new_string, replace_all
        )

        if error or match_count == 0:
            err_msg = error or f"Could not find match for old_string in {path}"
            try:
                err_msg += format_no_match_hint(err_msg, match_count, old_string, content)
            except Exception:
                pass
            return PatchResult(error=err_msg)

        file_ending = _detect_line_ending(content)
        if file_ending:
            new_content = _normalize_line_endings(new_content, file_ending)

        write_result = self.write_file(str(resolved), new_content)
        if write_result.error:
            return PatchResult(error=f"Failed to write changes: {write_result.error}")

        # Post-write verification
        try:
            verify = resolved.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return PatchResult(error=f"Post-write verification failed: could not re-read {path}")
        _verify_bomless, _ = _strip_bom(verify)
        _verify_norm = _verify_bomless.replace("\r\n", "\n").replace("\r", "\n")
        _new_norm = new_content.replace("\r\n", "\n").replace("\r", "\n")
        if _verify_norm != _new_norm:
            return PatchResult(error=(
                f"Post-write verification failed for {path}: on-disk content "
                f"differs from intended write "
                f"(wrote {len(_new_norm)} chars, read back "
                f"{len(_verify_norm)} chars after normalizing line endings). "
                "The patch did not persist. Re-read the file and try again."
            ))

        diff = self._unified_diff(content, new_content, path)
        lint_result = self._check_lint_delta(str(resolved), pre_content=content, post_content=new_content)

        return PatchResult(
            success=True,
            diff=diff,
            files_modified=[path],
            lint=lint_result.to_dict() if lint_result else None,
        )

    # ---- PATCH (V4A) ----
    def patch_v4a(self, patch_content: str) -> PatchResult:
        from ._patch_parser import parse_v4a_patch, apply_v4a_operations

        operations, parse_error = parse_v4a_patch(patch_content)
        if parse_error:
            return PatchResult(error=f"Failed to parse patch: {parse_error}")
        return apply_v4a_operations(operations, self)

    # ---- LINT ----
    def _check_lint(self, path: str, content: Optional[str] = None) -> LintResult:
        ext = os.path.splitext(path)[1].lower()

        inproc = LINTERS_INPROC.get(ext)
        if inproc is not None:
            if content is None:
                try:
                    resolved = self._resolve(path)
                    content = resolved.read_bytes().decode("utf-8", errors="replace")
                except (PathEscape, OSError):
                    return LintResult(skipped=True, message=f"Failed to read {path} for lint")
            ok, err = inproc(content)
            if err == "__SKIP__":
                return LintResult(skipped=True, message=f"No linter available for {ext} (missing dependency)")
            return LintResult(success=ok, output="" if ok else err)

        # No in-process linter and no shell linter table in this port (the
        # compiled-language shell linters are OpenCharaAgent-optional; only the
        # in-process syntax tier is portable). Treat as "no linter".
        return LintResult(skipped=True, message=f"No linter for {ext} files")

    def _check_lint_delta(self, path: str, pre_content: Optional[str],
                          post_content: Optional[str] = None) -> LintResult:
        post = self._check_lint(path, content=post_content)

        if post.success or post.skipped:
            return post

        if pre_content is None:
            return post

        pre = self._check_lint(path, content=pre_content)
        if pre.success or pre.skipped or not pre.output:
            return post

        pre_lines = {ln.strip() for ln in pre.output.splitlines() if ln.strip()}
        post_lines = [ln for ln in post.output.splitlines() if ln.strip() and ln.strip() not in pre_lines]

        if not post_lines:
            return LintResult(
                success=False,
                output=post.output,
                message="Pre-existing lint errors — this edit didn't introduce new ones but the file is still broken.",
            )

        return LintResult(
            success=False,
            output=(
                "New lint errors introduced by this edit "
                "(pre-existing errors filtered out):\n" + "\n".join(post_lines)
            ),
        )
