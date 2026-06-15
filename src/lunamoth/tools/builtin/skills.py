"""Skills tools — hermes-identical: skills_list, skill_view, skill_manage.

Apple-to-apple port of hermes-agent ``tools/skills_tool.py`` (read side) and
``tools/skill_manager_tool.py`` (mutate side), re-implemented against LunaMoth's
per-chara ``SkillStore`` (``ctx.skills``). Plugin/curator/provenance/security-scan
layers are hermes-infra and are dropped (noted in port-needs); the resolution,
collision refusal, traversal guards, frontmatter validation, atomic
write+rollback, and the six mutation actions are preserved verbatim.

All three register under toolset ``skills``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from ..registry import registry, tool_error
from ..skills import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_SKILL_CONTENT_CHARS,
    VALID_NAME_RE,
    is_excluded_skill_path,
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_platform,
    _parse_tags,
)

logger = logging.getLogger("lunamoth.tools.builtin.skills")

MAX_SKILL_FILE_BYTES = 1_048_576  # 1 MiB per supporting file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}

_INJECTION_PATTERNS = [
    "ignore previous instructions", "ignore all previous", "you are now",
    "disregard your", "forget your instructions", "new instructions:",
    "system prompt:", "<system>", "]]>",
]


# ---------------------------------------------------------------------------
# Shared path helpers (hermes tools/path_security.py)
# ---------------------------------------------------------------------------

def _has_traversal_component(path_str: str) -> bool:
    return ".." in Path(path_str).parts


def _validate_within_dir(path: Path, root: Path) -> Optional[str]:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def _skill_lookup_path_error(name: str) -> Optional[str]:
    if not isinstance(name, str):
        return "Skill name must be a string."
    candidate = name.strip()
    if (
        PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
        or PureWindowsPath(candidate).drive
    ):
        return "Skill name must be a relative path within the skills directory."
    if _has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def _err(msg: str, **extra) -> str:
    payload = {"success": False, "error": msg}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ===========================================================================
# skills_list
# ===========================================================================

def skills_list(args: dict, ctx) -> str:
    store = getattr(ctx, "skills", None)
    if store is None:
        return tool_error("Skills are not available in this environment.", success=False)
    category = args.get("category")
    try:
        store.skills_dir.mkdir(parents=True, exist_ok=True)
        all_skills = store.find_all(category=category)
        if not all_skills:
            return json.dumps({
                "success": True,
                "skills": [],
                "categories": [],
                "message": "No skills found.",
            }, ensure_ascii=False)
        # Tier-1: name + description + category only.
        listed = [
            {"name": s["name"], "description": s["description"], "category": s.get("category")}
            for s in all_skills
        ]
        categories = sorted({s["category"] for s in listed if s.get("category")})
        return json.dumps({
            "success": True,
            "skills": listed,
            "categories": categories,
            "count": len(listed),
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return tool_error(str(e), success=False)


SKILLS_LIST_SCHEMA = {
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}


# ===========================================================================
# skill_view
# ===========================================================================

def _find_view_candidates(store, name: str) -> List[Tuple[Optional[Path], Path]]:
    """Resolve *name* to (skill_dir, skill_md) candidates across all dirs using
    the three hermes strategies: direct path, recursive by parent-dir name,
    legacy flat ``<name>.md``."""
    candidates: List[Tuple[Optional[Path], Path]] = []
    seen_md: set = set()

    def _record(sd: Optional[Path], smd: Path) -> None:
        try:
            key = smd.resolve()
        except OSError:
            key = smd
        if key in seen_md:
            return
        seen_md.add(key)
        candidates.append((sd, smd))

    for search_dir in store.all_dirs():
        if not search_dir.exists():
            continue
        # Strategy 1: direct path.
        direct_path = search_dir / name
        if direct_path.is_dir() and (direct_path / "SKILL.md").exists():
            _record(direct_path, direct_path / "SKILL.md")
        elif direct_path.with_suffix(".md").exists():
            _record(None, direct_path.with_suffix(".md"))
        # Strategy 2: recursive by directory name.
        for found_skill_md in iter_skill_index_files(search_dir):
            if is_excluded_skill_path(found_skill_md):
                continue
            if found_skill_md.parent.name == name:
                _record(found_skill_md.parent, found_skill_md)
        # Strategy 3: legacy flat <name>.md files.
        for found_md in search_dir.rglob(f"{name}.md"):
            if is_excluded_skill_path(found_md):
                continue
            if found_md.name != "SKILL.md":
                _record(None, found_md)
    return candidates


def skill_view(args: dict, ctx) -> str:
    store = getattr(ctx, "skills", None)
    if store is None:
        return tool_error("Skills are not available in this environment.", success=False)
    name = str(args.get("name") or "")
    file_path = args.get("file_path")

    if not name:
        return _err("Skill name is required.")

    lookup_error = _skill_lookup_path_error(name)
    if lookup_error:
        return _err(lookup_error, hint="Use a skill name or relative path within the skills directory.")

    candidates = _find_view_candidates(store, name)

    if len(candidates) > 1:
        paths = [str(smd) for _, smd in candidates]
        logger.warning("Skill name collision for '%s': %d candidates", name, len(candidates))
        return _err(
            f"Ambiguous skill name '{name}': {len(candidates)} skills match across "
            "your skills dir and external_dirs. Refusing to guess — load one "
            "explicitly by its categorized path.",
            matches=paths,
            hint="Pass the full relative path (e.g., 'category/skill-name'), or rename one of the colliding skills.",
        )

    skill_dir: Optional[Path] = None
    skill_md: Optional[Path] = None
    if candidates:
        skill_dir, skill_md = candidates[0]

    if not skill_md or not skill_md.exists():
        available = [s["name"] for s in store.find_all()[:20]]
        return _err(
            f"Skill '{name}' not found.",
            available_skills=available,
            hint="Use skills_list to see all available skills",
        )

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        return _err(f"Failed to read skill '{name}': {e}")

    # Security warnings (log only, still serve).
    _trusted = [store.skills_dir.resolve()] + [d.resolve() for d in store.external_dirs]
    outside = True
    for td in _trusted:
        try:
            skill_md.resolve().relative_to(td)
            outside = False
            break
        except ValueError:
            continue
    lowered = content.lower()
    if outside or any(p in lowered for p in _INJECTION_PATTERNS):
        logger.warning("Skill security warning for '%s' (outside=%s)", name, outside)

    frontmatter, _ = parse_frontmatter(content)
    if not skill_matches_platform(frontmatter):
        return _err(f"Skill '{name}' is not supported on this platform.", readiness_status="unsupported")

    # A specific linked file was requested.
    if file_path and skill_dir:
        if _has_traversal_component(file_path):
            return _err("Path traversal ('..') is not allowed.", hint="Use a relative path within the skill directory")
        target_file = skill_dir / file_path
        traversal_error = _validate_within_dir(target_file, skill_dir)
        if traversal_error:
            return _err(traversal_error, hint="Use a relative path within the skill directory")
        if not target_file.exists():
            available_files: Dict[str, List[str]] = {
                "references": [], "templates": [], "assets": [], "scripts": [], "other": []
            }
            for f in skill_dir.rglob("*"):
                if f.is_file() and f.name != "SKILL.md":
                    rel = str(f.relative_to(skill_dir))
                    if rel.startswith("references/"):
                        available_files["references"].append(rel)
                    elif rel.startswith("templates/"):
                        available_files["templates"].append(rel)
                    elif rel.startswith("assets/"):
                        available_files["assets"].append(rel)
                    elif rel.startswith("scripts/"):
                        available_files["scripts"].append(rel)
                    elif f.suffix in {".md", ".py", ".yaml", ".yml", ".json", ".tex", ".sh"}:
                        available_files["other"].append(rel)
            available_files = {k: v for k, v in available_files.items() if v}
            return _err(
                f"File '{file_path}' not found in skill '{name}'.",
                available_files=available_files,
                hint="Use one of the available file paths listed above",
            )
        try:
            file_content = target_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return json.dumps({
                "success": True,
                "name": name,
                "file": file_path,
                "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                "is_binary": True,
            }, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "name": name,
            "file": file_path,
            "content": file_content,
            "file_type": target_file.suffix,
        }, ensure_ascii=False)

    # Main SKILL.md result — enumerate linked files.
    reference_files: List[str] = []
    template_files: List[str] = []
    asset_files: List[str] = []
    script_files: List[str] = []
    if skill_dir:
        references_dir = skill_dir / "references"
        if references_dir.exists():
            reference_files = [str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")]
        templates_dir = skill_dir / "templates"
        if templates_dir.exists():
            for ext in ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"]:
                template_files.extend(str(f.relative_to(skill_dir)) for f in templates_dir.rglob(ext))
        assets_dir = skill_dir / "assets"
        if assets_dir.exists():
            for f in assets_dir.rglob("*"):
                if f.is_file():
                    asset_files.append(str(f.relative_to(skill_dir)))
        scripts_dir = skill_dir / "scripts"
        if scripts_dir.exists():
            for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
                script_files.extend(str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext))

    hermes_meta = {}
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        hermes_meta = metadata.get("hermes", {}) or {}
    tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
    related_skills = _parse_tags(hermes_meta.get("related_skills") or frontmatter.get("related_skills", ""))

    linked_files: Dict[str, List[str]] = {}
    if reference_files:
        linked_files["references"] = reference_files
    if template_files:
        linked_files["templates"] = template_files
    if asset_files:
        linked_files["assets"] = asset_files
    if script_files:
        linked_files["scripts"] = script_files

    try:
        rel_path = str(skill_md.relative_to(store.skills_dir))
    except ValueError:
        rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
    skill_name = frontmatter.get("name", skill_md.stem if not skill_dir else skill_dir.name)

    result = {
        "success": True,
        "name": skill_name,
        "description": frontmatter.get("description", ""),
        "tags": tags,
        "related_skills": related_skills,
        "content": content,
        "path": rel_path,
        "skill_dir": str(skill_dir) if skill_dir else None,
        "linked_files": linked_files if linked_files else None,
        "usage_hint": (
            "To view linked files, call skill_view(name, file_path) where file_path "
            "is e.g. 'references/api.md' or 'assets/config.yaml'"
        ) if linked_files else None,
        "readiness_status": "available",
    }
    return json.dumps(result, ensure_ascii=False)


SKILL_VIEW_SCHEMA = {
    "description": (
        "Skills allow for loading information about specific tasks and workflows, as "
        "well as scripts and templates. Load a skill's full content or access its "
        "linked files (references, templates, scripts). First call returns SKILL.md "
        "content plus a 'linked_files' dict showing available references/templates/"
        "scripts. To access those, call again with file_path parameter."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "The skill name (use skills_list to see available skills). For "
                    "plugin-provided skills, use the qualified form 'plugin:skill' "
                    "(e.g. 'superpowers:writing-plans')."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "OPTIONAL: Path to a linked file within the skill (e.g., "
                    "'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). "
                    "Omit to get the main SKILL.md content."
                ),
            },
        },
        "required": ["name"],
    },
}


# ===========================================================================
# skill_manage
# ===========================================================================

def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    import re as _re
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."
    end_match = _re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
    yaml_content = content[3:end_match.start() + 3]
    try:
        import yaml
        parsed = yaml.safe_load(yaml_content)
    except ModuleNotFoundError:
        # PyYAML is not installed in this environment — fall back to the same
        # key:value parser parse_frontmatter() uses so SKILL.md validation still
        # works for the common flat-frontmatter shape (see port-needs.md).
        parsed, _ = parse_frontmatter(content)
    except Exception as e:  # noqa: BLE001  (real YAML syntax error)
        return f"YAML frontmatter parse error: {e}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _validate_file_path(file_path: str) -> Optional[str]:
    if not file_path:
        return "file_path is required."
    normalized = Path(file_path)
    if _has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."
    if normalized.parts and normalized.name == "SKILL.md":
        if len(normalized.parts) in (1, 2):
            return None
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"
    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    target = skill_dir / file_path
    error = _validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(file_path.parent), prefix=f".{file_path.name}.tmp.", suffix="")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temp file %s during atomic write", temp_path, exc_info=True)
        raise


def _resolve_skill_dir(store, name: str, category: str = None) -> Path:
    if category:
        return store.skills_dir / category / name
    return store.skills_dir / name


def _skill_not_found_error(store, name: str, suffix: str = "") -> str:
    base = f"Skill '{name}' not found."
    base += " Use skills_list() to see available skills."
    if suffix:
        base += suffix
    return base


def _create_skill(store, name: str, content: str, category: str = None) -> Dict[str, Any]:
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}
    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}
    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}
    existing = store.find_skill(name)
    if existing:
        return {"success": False, "error": f"A skill named '{name}' already exists at {existing}."}
    skill_dir = _resolve_skill_dir(store, name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)
    result: Dict[str, Any] = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(store.skills_dir)),
        "skill_md": str(skill_md),
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        f"skill_manage(action='write_file', name='{name}', file_path='references/example.md', file_content='...')"
    )
    return result


def _edit_skill(store, name: str, content: str) -> Dict[str, Any]:
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}
    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}
    existing = store.find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(store, name)}
    skill_md = existing / "SKILL.md"
    _atomic_write_text(skill_md, content)
    return {"success": True, "message": f"Skill '{name}' updated.", "path": str(existing)}


def _patch_skill(store, name: str, old_string: str, new_string: str,
                 file_path: str = None, replace_all: bool = False) -> Dict[str, Any]:
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}
    existing = store.find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(store, name)}
    skill_dir = existing
    if file_path:
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        target = skill_dir / "SKILL.md"
    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}
    content = target.read_text(encoding="utf-8")

    from ._skill_fuzzy import fuzzy_find_and_replace, format_no_match_hint
    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:  # noqa: BLE001
            pass
        return {"success": False, "error": err_msg, "file_preview": preview}

    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"Patch would break SKILL.md structure: {err}"}
    _atomic_write_text(target, new_content)
    return {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' "
                   f"({match_count} replacement{'s' if match_count > 1 else ''}).",
    }


def _delete_skill(store, name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    existing = store.find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(store, name)}
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {"success": False, "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted."}
        if not store.find_skill(target_name):
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }
    skill_dir = existing
    shutil.rmtree(skill_dir)
    parent = skill_dir.parent
    skills_root = store.skills_dir
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    message = f"Skill '{name}' deleted."
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        message += f" Content absorbed into '{absorbed_into.strip()}'."
    return {"success": True, "message": message}


def _write_file_action(store, name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}
    if file_content is None:
        return {"success": False, "error": "file_content is required."}
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}
    existing = store.find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(store, name, " Create it first with action='create'.")}
    target, err = _resolve_skill_target(existing, file_path)
    if err:
        return {"success": False, "error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, file_content)
    return {"success": True, "message": f"File '{file_path}' written to skill '{name}'.", "path": str(target)}


def _remove_file_action(store, name: str, file_path: str) -> Dict[str, Any]:
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}
    existing = store.find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(store, name)}
    skill_dir = existing
    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }
    target.unlink()
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return {"success": True, "message": f"File '{file_path}' removed from skill '{name}'."}


def skill_manage(args: dict, ctx) -> str:
    store = getattr(ctx, "skills", None)
    if store is None:
        return tool_error("Skills are not available in this environment.", success=False)

    action = str(args.get("action") or "")
    name = str(args.get("name") or "")
    content = args.get("content")
    category = args.get("category")
    file_path = args.get("file_path")
    file_content = args.get("file_content")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))
    absorbed_into = args.get("absorbed_into")

    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(store, name, content, category)
    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(store, name, content)
    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(store, name, old_string, new_string, file_path, replace_all)
    elif action == "delete":
        result = _delete_skill(store, name, absorbed_into=absorbed_into)
    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file_action(store, name, file_path, file_content)
    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file_action(store, name, file_path)
    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

    return json.dumps(result, ensure_ascii=False)


SKILL_MANAGE_SCHEMA = {
    "description": (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        "New skills go to your skills directory; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. The target you name in "
        "`absorbed_into` must already exist — create/patch the umbrella first, "
        "then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                ),
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'). Can be empty string "
                    "to delete the matched text."
                ),
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false).",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                ),
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'.",
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so consolidation can be told "
                    "from pruning. Pass the umbrella skill name when this skill's "
                    "content was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and being "
                    "pruned with no forwarding target."
                ),
            },
        },
        "required": ["action", "name"],
    },
}


def _check_skills_requirements() -> bool:
    """Skills are always available — the directory is created on first use."""
    return True


registry.register(
    "skills_list", "skills", SKILLS_LIST_SCHEMA, skills_list,
    check_fn=_check_skills_requirements, emoji="📚",
)
registry.register(
    "skill_view", "skills", SKILL_VIEW_SCHEMA, skill_view,
    check_fn=_check_skills_requirements, emoji="📚",
)
registry.register(
    "skill_manage", "skills", SKILL_MANAGE_SCHEMA, skill_manage,
    emoji="📝",
)
