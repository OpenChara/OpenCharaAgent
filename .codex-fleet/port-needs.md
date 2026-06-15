
## [terminal+process group] BLOCKER: shared tools/memory.py has a broken import

`src/lunamoth/tools/memory.py:36` (modified by the memory/skills group) does:
    from ..builtin._threat_patterns import first_threat_message, scan_for_threats
`..builtin` resolves to `lunamoth.builtin` which does NOT exist. The helper is at
`lunamoth/tools/builtin/_threat_patterns.py`, so the correct import is the
single-dot relative form:
    from .builtin._threat_patterns import first_threat_message, scan_for_threats

This breaks `import lunamoth.tools` ENTIRELY (tools/__init__.py -> gateway ->
memory), so EVERY tool group's tests fail at collection. The memory/skills group
must fix this one character in their file. (My terminal/process modules import
cleanly once this is fixed — verified via a sys.modules shim.)

## web group (web_search + web_extract)
- BLOCKER (sibling, not ours): `src/lunamoth/tools/memory.py:36` does
  `from ..builtin._threat_patterns import first_threat_message, scan_for_threats`,
  which resolves to `lunamoth.builtin._threat_patterns` (one level too high — the
  package is `lunamoth.tools.builtin`) AND that module does not exist on disk yet.
  This makes the ENTIRE `lunamoth.tools` package unimportable, so `pytest`
  collection and `discover_builtin_tools()` both crash for unrelated groups.
  The memory/skills group must (a) create `tools/builtin/_threat_patterns.py`
  and (b) fix the import to `from ._threat_patterns import ...` (or
  `from ..builtin._threat_patterns` from within `tools/memory.py` -> should be
  `from .builtin._threat_patterns`). Our test file installs a temporary no-op
  stub (only when the real module is absent) so our 27 tests pass in isolation;
  remove nothing — the stub self-disables once the real module ships.
- web tools register fine and discovery sees them (verified with the stub):
  registry.get_all_tool_names() includes web_search, web_extract.

## [file tools group] tools/memory.py broken import blocks `import lunamoth.tools`
`src/lunamoth/tools/memory.py:36` does `from ..builtin._threat_patterns import ...`
but `_threat_patterns.py` lives at `tools/builtin/_threat_patterns.py`, so the
correct path is `.builtin._threat_patterns` (one dot, not two — `..builtin`
resolves to the non-existent `lunamoth.builtin`). This raises
`ModuleNotFoundError: No module named 'lunamoth.builtin'` on ANY
`import lunamoth.tools` (gateway -> memory), which breaks tool discovery and
every tool group's tests that import through the package. Owner of the memory
group must fix this one dot. (Not editing it here — sibling-owned file.)

## search_files group (search.py)
- SAME shared-file blocker as above: `tools/memory.py:36` imports
  `from ..builtin._threat_patterns import ...` which resolves to the
  non-existent `lunamoth.builtin` (should be `.builtin._threat_patterns`).
  Importing `lunamoth.tools.builtin.search` triggers tools/__init__ → gateway →
  memory and raises ModuleNotFoundError. tests/test_search.py installs the same
  no-op stub guard (only when `lunamoth.builtin` is absent) so the 23 search
  tests pass in isolation; it self-disables once memory.py is fixed.
- No other shared-file change needed: search.py imports only from ..registry +
  the leading-underscore helper _search_shell.py. Registered as toolset "files"
  (same toolset patch/read_file/write_file use). Discovery sees `search_files`.

## [execute_code + delegate_task group]
- NOT a shared-file change request — a flag: `src/lunamoth/tools/memory.py`
  (sibling memory/skills group, currently untracked) imports
  `from ..builtin._threat_patterns import ...`. From `lunamoth/tools/memory.py`
  that resolves to `lunamoth.builtin._threat_patterns` (does NOT exist). The
  helper actually lives at `lunamoth/tools/builtin/_threat_patterns.py`, so the
  import should be `from .builtin._threat_patterns import ...`. Because
  `tools/__init__.py` eagerly imports gateway -> memory, this typo currently
  breaks importing ANYTHING under `lunamoth.tools` (and thus full
  `discover_builtin_tools()`). My test self-heals with a sys.modules shim that
  only activates when the module is genuinely absent; once the sibling fixes the
  one-character import path the shim becomes a no-op. No action needed from the
  integrator beyond confirming that fix lands.

## browser_* group

- BLOCKER (shared file, not mine): `src/lunamoth/tools/memory.py:36` has
  `from ..builtin._threat_patterns import ...` which resolves to
  `lunamoth.builtin._threat_patterns` (does not exist). The module lives at
  `lunamoth.tools.builtin._threat_patterns` — the import should be
  `from .builtin._threat_patterns import ...` (single dot). As written it
  raises `ModuleNotFoundError: No module named 'lunamoth.builtin'` on
  `import lunamoth.tools`, breaking the whole `lunamoth.tools` package import
  (gateway → memory) and therefore the brief's discovery verification command
  `python -c "from lunamoth.tools.registry import registry, discover_builtin_tools; ..."`.
  My tests pass via a file-loader shim that bypasses the broken parent
  `__init__`; once memory.py is fixed the normal import path works unchanged.
  (memory.py + gateway.py are the memory/skills group's in-progress edits.)
- SHARED-FILE NEEDS for the browser toolpack to be reachable by a chara:
  - `core/state.py` DEFAULT_STATUS["tool_access"] + migration: add the 12
    `browser_*` names.
  - A `toolpacks/browser.json` pack (card opts in via
    `extensions.lunamoth.toolpack`) listing the 12 `browser_*` tools; realistically
    requires `isolation: dir` or `docker` + `--no-sandbox` (a real Chromium will
    not launch under the default sandbox-exec/bwrap jail — open R&D, flagged in
    `_browser_driver.py`).
  - `lunamoth doctor`/`setup` step to install Node + `agent-browser` +
    `agent-browser install` Chromium into a pinned `PLAYWRIGHT_BROWSERS_PATH`
    (cannot be a `uv` extra). Until present, `is_browser_available()` returns
    False and the tools are simply hidden (clean degrade).
- DEVIATION from hermes (intentional, per brief): all 12 share the `"browser"`
  toolset under one `is_browser_available` gate. hermes split
  browser_cdp/browser_dialog into a `browser-cdp` toolset behind a separate
  reachable-CDP-endpoint check and routed them through a persistent CDP
  WebSocket supervisor (+ the `websockets` package). LunaMoth has no CDP
  supervisor, so browser_cdp/browser_dialog drive agent-browser's own `cdp` /
  `dialog` verbs over the same daemon. Schemas remain byte-identical to hermes.

## memory + skills group (THIS group)

### RESOLVED: the `tools/memory.py` import the other groups flagged
`src/lunamoth/tools/memory.py` now imports `from .builtin._threat_patterns
import ...` (single dot, correct). The helper module
`tools/builtin/_threat_patterns.py` exists. `import lunamoth.tools` and
`discover_builtin_tools()` work; siblings can drop their temporary stubs.

### 1. Obsolete pre-hermes tests must be removed/rewritten (integration layer)
The hermes-identical port intentionally changes behavior the OLD tests assert.
`tests/test_memory.py` and the memory/skills cases in `tests/test_skills_mcp.py`
test the deprecated pre-hermes contract and now fail BY DESIGN (the seam doc
already says tool tests "get rewritten against the registry + new tool
behaviors"). The new contract is fully covered by `tests/test_memory_skills.py`.
Divergences they assert (all now hermes-correct):
- `MemoryStore.add/replace/remove` return a hermes dict, not a `list[str]`.
- empty-content `replace` is REJECTED ("Use 'remove'"), no longer deletes.
- default limits are 2200/1375 (was 4000/2000).
- over-limit `add`/`replace` returns a CONSOLIDATE payload, drops NOTHING (no
  drop-oldest backstop); drift/over-cap return `{success:False, drift_backup}`
  instead of raising.
- `set_limits` warns instead of truncating; `trust_disk` + lowercase
  `memory.md`/`user.md` are gone (files are `MEMORY.md`/`USER.md`; the store
  migrates old lowercase files once on a case-sensitive FS).
- `SkillStore.__init__` is now `SkillStore(skills_dir=…, external_dirs=[…])` (was
  `own_dir=/dirs=`); single writable root + read-only external dirs.
- `create_skill`/`read_skill` gateway tools are gone → `skills_list`/`skill_view`/
  `skill_manage`; cap 24k → 100k; over-cap create rejected (no head+notice read).
Action: delete `tests/test_memory.py` and the memory/skills cases in
`tests/test_skills_mcp.py`, or port them to mirror `tests/test_memory_skills.py`.

### 2. PyYAML is not a project dependency
Hermes SKILL.md frontmatter uses `yaml.safe_load`. PyYAML is NOT in
`pyproject.toml`, so `skills.parse_frontmatter` and the builtin skills
`_validate_frontmatter` fall back to a key:value parser on
`ModuleNotFoundError`. The common flat-frontmatter SKILL.md (all LunaMoth writes)
works fully; nested YAML (lists, `metadata.hermes.tags`) parses only when PyYAML
is present. For full nested-frontmatter parity, add `pyyaml` to deps (owner
sign-off — new runtime dependency).

### 3. agent.py memory/skills injection (no change required — parity note)
`agent._memory_text()` still renders the frozen snapshot as bilingual bullets,
not the hermes `═`×46 / usage-% block. The store now ALSO exposes
`format_for_system_prompt(target)` + `_render_block` + a frozen
`_system_prompt_snapshot`, so an owner who wants the hermes block can switch
`agent._memory_text()` to `self.memory.format_for_system_prompt(...)`. Left as an
allowed localized divergence ("language is a card property"); agent.py is a
shared file outside this group, so NOT changed here.
`agent._skills_snapshot = self.skills.render_block()` still works unchanged —
`render_block()` now emits the hermes `## Skills (mandatory)` / `<available_skills>`
block instead of the old flat list.
