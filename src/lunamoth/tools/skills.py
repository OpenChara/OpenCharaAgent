"""Skills — hermes-identical procedural memory the chara reads AND writes.

Apple-to-apple port of hermes-agent's skills layout: ONE writable root (plus
optional read-only external dirs) holding skill directories, each a ``SKILL.md``
with real-YAML frontmatter (``name`` ≤64, ``description`` ≤1024) and optional
``references/`` ``templates/`` ``scripts/`` ``assets/`` subdirs. Categories are a
single nesting level (``<root>/<category>/<skill>/SKILL.md``).

LunaMoth divergence (per-chara, one-process-one-chara): the writable root is the
chara's sandbox skills dir, not a global ``~/.hermes/skills``. The previous
three-tier (own/user/bundled) shadow search collapses to one writable root +
read-only external dirs (local wins on name collision), mirroring hermes.

Progressive disclosure: the system prompt carries only the COMPACT index
(``## Skills (mandatory)`` + ``<available_skills>``, grouped by category); full
text is fetched on demand via the skill_view tool.

Public API kept stable for the agent + /skills command:
  SkillStore() · scan() (dicts with name/description/origin/category/path) ·
  read(name) · create(name, description, content) · render_block()
The new builtin skill tools reach the store's dirs/helpers directly.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import ROOT, SANDBOX_ROOT

logger = logging.getLogger("lunamoth.tools.skills")

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000

# Filesystem-safe, URL-friendly skill names (hermes VALID_NAME_RE).
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))


def is_excluded_skill_path(path: Path) -> bool:
    try:
        parts = path.parts
    except AttributeError:  # pragma: no cover
        parts = Path(str(path)).parts
    return any(part in EXCLUDED_SKILL_DIRS for part in parts)


def iter_skill_index_files(skills_dir: Path, filename: str = "SKILL.md"):
    """Walk *skills_dir* yielding sorted paths matching *filename*, excluding
    VCS / virtualenv / cache directories so dependencies can't register skills."""
    matches: List[Path] = []
    for root, dirs, files in os.walk(skills_dir, followlinks=True):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string (real ``yaml.safe_load``,
    with a key:value fallback for malformed YAML). Returns (frontmatter, body)."""
    frontmatter: Dict[str, Any] = {}
    body = content
    if not content.startswith("---"):
        return frontmatter, body
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body
    yaml_content = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]
    try:
        import yaml
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()
    return frontmatter, body


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """True when the skill is compatible with the current OS (absent ``platforms``
    = all platforms). LunaMoth is macOS/Linux."""
    import sys
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    mapping = {"macos": "darwin", "linux": "linux", "windows": "win32"}
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = mapping.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


def _parse_tags(tags_value) -> List[str]:
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


class SkillStore:
    """Single writable skills root + optional read-only external dirs."""

    def __init__(self, skills_dir: Path | None = None, external_dirs: "list[Path] | None" = None):
        # The ONE writable root: per-chara sandbox skills dir.
        self.skills_dir = Path(skills_dir) if skills_dir is not None else (SANDBOX_ROOT / "skills")
        if external_dirs is not None:
            self.external_dirs = [Path(d) for d in external_dirs]
        else:
            home = Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser()
            # Read-only external dirs (local takes precedence on name collisions):
            # the user's global library, then bundled examples.
            self.external_dirs = [home / "skills", ROOT / "skills"]
        # Back-compat alias some callers used.
        self.own_dir = self.skills_dir

    # ---- search dirs ----------------------------------------------------------

    def all_dirs(self) -> List[Path]:
        """The writable root first, then external dirs (precedence order)."""
        return [self.skills_dir] + list(self.external_dirs)

    def _origin(self, skill_md: Path) -> str:
        try:
            skill_md.resolve().relative_to(self.skills_dir.resolve())
            return "own"
        except (ValueError, OSError):
            return "external"

    def _category_from_path(self, skill_md: Path) -> Optional[str]:
        for base in self.all_dirs():
            try:
                rel = skill_md.relative_to(base)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) >= 3:  # <category>/<skill>/SKILL.md
                return parts[0]
            return None
        return None

    # ---- discovery ------------------------------------------------------------

    def find_all(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """All skills, first-hit-wins by name across the search order. Each dict:
        ``{name, description, category, origin, path}``. Mirrors hermes
        ``_find_all_skills`` (skip platform-mismatched; description falls back to
        the first non-``#`` body line; truncate to MAX_DESCRIPTION_LENGTH)."""
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for base in self.all_dirs():
            if not base.is_dir():
                continue
            for skill_md in iter_skill_index_files(base):
                if is_excluded_skill_path(skill_md):
                    continue
                skill_dir = skill_md.parent
                try:
                    content = skill_md.read_text(encoding="utf-8")[:4000]
                    frontmatter, body = parse_frontmatter(content)
                except (OSError, UnicodeDecodeError):
                    continue
                if not skill_matches_platform(frontmatter):
                    continue
                name = str(frontmatter.get("name", skill_dir.name))[:MAX_NAME_LENGTH]
                if not name or name in seen:
                    continue
                description = str(frontmatter.get("description", "") or "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break
                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."
                cat = self._category_from_path(skill_md)
                seen.add(name)
                out.append({
                    "name": name,
                    "description": description,
                    "category": cat,
                    "origin": self._origin(skill_md),
                    "path": str(skill_md),
                })
        if category:
            out = [s for s in out if s.get("category") == category]
        return sorted(out, key=lambda s: (s.get("category") or "", s["name"]))

    # Back-compat name used by /skills and tests.
    def scan(self) -> List[Dict[str, Any]]:
        return self.find_all()

    def find_skill(self, name: str) -> Optional[Path]:
        """Return the skill directory for *name* (parent-dir-name match across all
        dirs, writable root first). None when not found."""
        for base in self.all_dirs():
            if not base.is_dir():
                continue
            for skill_md in iter_skill_index_files(base):
                if is_excluded_skill_path(skill_md):
                    continue
                if skill_md.parent.name == name:
                    return skill_md.parent
        return None

    def read(self, name: str) -> str:
        """Full SKILL.md text for one skill (frontmatter included)."""
        for skill in self.find_all():
            if skill["name"] == name:
                try:
                    return Path(skill["path"]).read_text(encoding="utf-8")
                except OSError as e:
                    raise ValueError(f"skill {name!r} unreadable: {e}") from e
        raise ValueError(f"no skill named {name!r} — see the skill index in your context")

    # ---- self-improvement (kept for back-compat; the skill_manage tool is the
    #      full hermes mutation surface) ---------------------------------------

    def create(self, name: str, description: str, content: str) -> Path:
        """Write one of the chara's OWN skills into the writable root."""
        name = (name or "").strip().lower()
        if len(name) > MAX_NAME_LENGTH or not VALID_NAME_RE.match(name):
            raise ValueError(
                "skill name must be lowercase letters/digits/hyphens/dots/underscores, "
                "start with a letter or digit, max 64 chars"
            )
        description = " ".join((description or "").split())
        if not description:
            raise ValueError("a one-line description is required (it is the index entry)")
        body = (content or "").strip()
        if not body:
            raise ValueError("content is empty")
        meta, stripped = parse_frontmatter(body)
        if meta:
            body = stripped.strip()
        text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
        if len(text) > MAX_SKILL_CONTENT_CHARS:
            raise ValueError(
                f"skill {name!r} is {len(text)} chars but a SKILL.md is capped at "
                f"{MAX_SKILL_CONTENT_CHARS}. Trim it, or split across two named skills."
            )
        path = self.skills_dir / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # ---- prompt block (hermes ## Skills (mandatory) / <available_skills>) -----

    def render_block(self) -> str:
        """The compact skills index for the system prompt ('' when there are none).

        Mirrors hermes ``build_skills_system_prompt`` output: a ``## Skills
        (mandatory)`` header with scan-before-replying guidance, then an
        ``<available_skills>`` block grouped by category with ``- name:
        description`` lines.
        """
        skills = self.find_all()
        if not skills:
            return ""
        lines = [
            "## Skills (mandatory)",
            "",
            "You have skills — reusable procedures for specific tasks. Before you "
            "reply, scan this list: if any skill is even partially relevant to what "
            "you're about to do, load it with skill_view(name) first and follow it. "
            "Err on the side of loading. If a skill is wrong or incomplete, fix it "
            "with skill_manage(action='patch'). After a hard or iterative task, "
            "consider saving what you learned as a new skill.",
            "",
            "<available_skills>",
        ]
        # Group by category (None last, under a generic heading).
        by_cat: Dict[str, List[Dict[str, Any]]] = {}
        for s in skills:
            by_cat.setdefault(s.get("category") or "", []).append(s)
        for cat in sorted(by_cat.keys()):
            entries = by_cat[cat]
            if cat:
                lines.append(f"### {cat}")
            for s in entries:
                lines.append(f"- {s['name']}: {s['description']}")
        lines.append("</available_skills>")
        return "\n".join(lines)
