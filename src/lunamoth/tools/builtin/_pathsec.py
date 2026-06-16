"""Workspace path confinement + ``~`` expansion for the file/search tools.

Self-contained and minimal so the file and search groups can both import it.
hermes resolves model-supplied paths against a live terminal cwd / ``$TERMINAL_CWD``
and shells ``echo $HOME`` to expand ``~``. LunaMoth runs one chara per process
inside a sandbox; every model-supplied path is anchored under the chara's
``workspace`` directory and is NOT allowed to escape it (except writable_paths
the operator opted into). The mandatory check is the sandbox-escape guard.

Public surface:
- ``has_traversal_component(path_str) -> bool`` — quick ``..`` check (hermes
  ``path_security.has_traversal_component`` parity; used on V4A headers only).
- ``expand_user(path_str, workspace) -> str`` — expand ``~`` / ``~/...`` to the
  workspace (the chara's home is its workspace; ``~user`` is left literal —
  there are no other users inside the sandbox, so an injection vector is closed).
- ``resolve_in_workspace(path_str, workspace, writable_paths=()) -> Path`` —
  resolve a model path to an absolute Path confined under ``workspace`` (or an
  explicitly opted-in writable path); raises ``PathEscape`` on escape.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


class PathEscape(ValueError):
    """Raised when a model-supplied path resolves outside the workspace."""


def has_traversal_component(path_str: str) -> bool:
    """Return True if *path_str* contains a ``..`` traversal component."""
    return ".." in Path(path_str).parts


def map_virtual_assets(p: Path, workspace: Path, assets_dir: Path | None) -> Path:
    """Anchor a relative model path, honoring the virtual ``assets/`` prefix.

    The chara addresses its read-only reference shelf as ``assets/...`` even
    though that shelf physically lives in a SIBLING dir of the workspace
    (``sandbox/assets``, not ``sandbox/workspace/assets``). A leading ``assets``
    path component is therefore re-anchored under *assets_dir*; everything else
    is workspace-relative. An absolute path is returned unchanged (confinement
    is enforced later by the caller). When *assets_dir* is ``None`` the virtual
    prefix is inert and ``assets/...`` is treated as an ordinary workspace path.
    """
    if p.is_absolute():
        return p
    parts = p.parts
    if assets_dir is not None and parts and parts[0] == "assets":
        rest = Path(*parts[1:]) if len(parts) > 1 else Path()
        return assets_dir / rest
    return workspace / p


def expand_user(path_str: str, workspace: Path) -> str:
    """Expand a leading ``~`` to the chara's workspace (its home).

    ``~`` / ``~/...`` map to the workspace root. ``~user/...`` is left literal
    (no other users exist inside the sandbox — expanding it would be a no-op at
    best and an injection vector at worst).
    """
    if not path_str:
        return path_str
    if path_str == "~":
        return str(workspace)
    if path_str.startswith("~/"):
        return str(workspace) + path_str[1:]
    return path_str


def resolve_in_workspace(
    path_str: str,
    workspace: Path,
    writable_paths: Iterable[str] = (),
    *,
    assets_dir: Path | None = None,
    readable: bool = False,
) -> Path:
    """Resolve a model-supplied path to an absolute Path confined to the workspace.

    Resolution order (hermes ``_resolve_path_for_task`` re-anchored to the
    sandbox): expand ``~``; a leading ``assets/`` component is re-anchored to the
    read-only reference sibling (*assets_dir*); an absolute path is taken as-is;
    any other relative path is anchored under ``workspace``. The resolved path
    must live under ``workspace`` (or an operator-opted-in ``writable_path``);
    otherwise ``PathEscape`` is raised so a sandbox escape is visible, never
    silent.

    The ``assets`` sibling is a READ-only root: it is an allowed destination only
    when *readable* is true (read_file/search/send_file). Write paths
    (``readable=False``, the default) deliberately EXCLUDE it from the allowed
    roots, so a write that resolves into ``assets/`` fails confinement — the
    file-tool wrapper turns that into a friendly read-only refusal first, but the
    resolver is the hard backstop. The virtual ``assets/`` prefix is mapped in
    both cases so the refusal/escape points at the real sibling location.
    """
    if not path_str:
        raise PathEscape("empty path")

    workspace = Path(workspace).resolve()
    assets_dir = Path(assets_dir).resolve() if assets_dir else None
    expanded = expand_user(path_str, workspace)
    p = Path(expanded)
    candidate = map_virtual_assets(p, workspace, assets_dir)

    # Resolve symlinks + ``..`` without requiring the file to exist.
    resolved = Path(_resolve_nonexistent(candidate))

    roots = [workspace] + [Path(w).resolve() for w in writable_paths if w]
    if readable and assets_dir is not None:
        roots.append(assets_dir)
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PathEscape(
        f"Path escapes the workspace: {path_str!r} resolves to {resolved} "
        f"which is outside {workspace}."
    )


def _resolve_nonexistent(path: Path) -> str:
    """``Path.resolve()`` that works for paths whose tail does not exist yet
    (so write_file/patch on a new file still get a normalized absolute path).
    ``strict=False`` resolves the existing prefix and normalizes the rest."""
    return str(path.resolve())
