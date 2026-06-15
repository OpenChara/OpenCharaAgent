# Spec: hermes-identical MEMORY & SKILLS for LunaMoth

Goal: REPLACE LunaMoth's current memory/skills with faithful re-implementations of
hermes-agent's, so they become hermes-identical in schema, actions, storage layout,
durability machinery, and prompt injection.

All cites are `file:line`. Hermes paths are under
`reference/hermes-agent/`; LunaMoth paths under `src/lunamoth/`.

Source files read:
- Hermes memory: `reference/hermes-agent/tools/memory_tool.py` (entire file, 731 lines)
- Hermes skills view/list: `reference/hermes-agent/tools/skills_tool.py` (1602 lines)
- Hermes skill mutation: `reference/hermes-agent/tools/skill_manager_tool.py` (1044 lines)
- Hermes skill provenance: `reference/hermes-agent/tools/skill_provenance.py`
- Hermes prompt injection: `reference/hermes-agent/agent/system_prompt.py:300-380`,
  `reference/hermes-agent/agent/prompt_builder.py:1085-1313`
- Hermes home/path: `reference/hermes-agent/hermes_constants.py:50-59`,
  `reference/hermes-agent/utils.py:61` (`atomic_replace`)
- LunaMoth memory: `src/lunamoth/tools/memory.py` (232 lines)
- LunaMoth skills: `src/lunamoth/tools/skills.py` (161 lines)
- LunaMoth wiring: `src/lunamoth/tools/gateway.py:243-285,379-484`,
  `src/lunamoth/core/agent.py:116-160,303-323,423-483`

---

# PART 1 — MEMORY (`tools/memory_tool.py`)

## 1.1 The tool — single `memory` tool, JSON schema

Hermes ships ONE function-calling tool named `memory`
(`memory_tool.py:659-708`, `MEMORY_SCHEMA`). Full schema:

```
name: "memory"
parameters (type object):
  action:  string, enum ["add", "replace", "remove"]   (REQUIRED)
  target:  string, enum ["memory", "user"]              (REQUIRED)
  content: string  — entry text; required for add and replace
  old_text: string — short unique SUBSTRING identifying the entry to replace/remove
required: ["action", "target"]
```

There is NO `read` action in the schema. The docstring at `:20` mentions
`read` as a design note, but the dispatcher (`memory_tool.py:627-645`) only
implements `add`/`replace`/`remove` and explicitly returns
`"Unknown action … Use: add, replace, remove"` (`:645`) for anything else.
The live entries are surfaced instead through the *success response* of every
mutation (`_success_response`, `:465-480` — returns `entries`, `usage`,
`entry_count`), and the prompt snapshot.

Dispatcher arg validation (`memory_tool.py:609-647`):
- `store is None` → tool_error "Memory is not available…" (`:621-622`).
- `target` not in `{memory,user}` → tool_error (`:624-625`).
- `add` requires `content` (`:628-629`); `replace` requires `old_text` AND
  `content` (`:632-636`); `remove` requires `old_text` (`:639-641`).
- Returns `json.dumps(result, ensure_ascii=False)` (`:647`).

Registry registration (`memory_tool.py:714-726`): `name="memory"`,
`toolset="memory"`, `emoji="🧠"`, handler maps `args` → `memory_tool(...)`
with `store=kw.get("store")`.

The schema **description** (`:661-683`) is load-bearing behavioral guidance and
should be ported close to verbatim. Key clauses:
- WHEN TO SAVE proactively: user corrections / "remember this"; preferences,
  habits, personal details; environment discoveries; conventions/API quirks;
  stable reusable facts.
- PRIORITY: user preferences/corrections > environment facts > procedural knowledge.
- Do NOT save task progress, session outcomes, completed-work logs, TODO state
  (use session_search). New how-to → save as a *skill*.
- TWO TARGETS: `user` = who the user is; `memory` = your own notes.
- ACTIONS: add / replace (old_text identifies) / remove (old_text identifies).
- SKIP: trivial/obvious, re-discoverable, raw dumps, temporary task state.

## 1.2 Storage format & location

- Directory: `get_memory_dir()` = `get_hermes_home() / "memories"`
  (`memory_tool.py:55-57`). Resolved DYNAMICALLY per call (NOT cached at import)
  so a profile/home override is always honored (`:51-54`).
- Two files: `MEMORY.md` (agent's own notes) and `USER.md` (user profile)
  (`_path_for`, `:245-250`).
- Entry delimiter: `ENTRY_DELIMITER = "\n§\n"` (`:59`) — section sign on its own
  line. Entries may be multiline; splitting on the full delimiter (not bare `§`)
  lets an entry contain a literal `§` (`:517-519`).
- On read, entries are stripped and empties dropped (`_read_file`, `:500-520`).
- On load, entries are de-duplicated preserving first occurrence via
  `dict.fromkeys` (`:156-158`); reload-under-lock dedups too (`:266`).
- Character limits (NOT tokens — model-independent, `:17`): default
  `memory_char_limit=2200`, `user_char_limit=1375` (`:124`). The limit budgets the
  ENTIRE store (`ENTRY_DELIMITER.join(entries)` length), not per-entry
  (`_char_count`, `:286-290`).

## 1.3 Durability machinery (`_write_file`, `:577-606`)

Atomic temp-file → fsync → replace. Exact sequence:
1. `tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=".mem_")` — same dir
   so the rename is same-filesystem/atomic (`:589-591`).
2. `os.fdopen(fd,"w") … f.write(content); f.flush(); os.fsync(f.fileno())`
   (`:593-596`) — durable on disk BEFORE it becomes visible.
3. `atomic_replace(tmp_path, path)` (`:597`) → `os.replace` under the hood
   (`utils.py:61`), atomic rename.
4. On ANY exception: `os.unlink(tmp_path)` cleanup then re-raise (`:598-603`).
5. OSError/IOError → `RuntimeError(f"Failed to write memory file …")` (`:605-606`).

The point (`:580-584`): old `open("w")+flock` truncated before locking, leaving a
race window where a reader saw an empty file. Atomic rename means readers always
see the complete old or complete new file — so `_read_file` needs **no lock**
(`:502-505`).

Separate from the atomic write, mutations take an **exclusive file lock** for
read-modify-write safety via a sidecar `.lock` file (`_file_lock`, `:208-243`):
`fcntl.flock(LOCK_EX)` on Unix, `msvcrt.locking` on Windows, no-op if neither
available. The lock is on `<file>.lock`, NOT the data file, precisely so the data
file can still be `os.replace`d. Every `add`/`replace`/`remove` runs inside
`with self._file_lock(self._path_for(target))` (`:308,363,420`).

## 1.4 External-edit drift guard (`_detect_external_drift`, `:522-575`; `_drift_error`, `:83-110`)

Before mutating, each op calls `_reload_target` (`:252-268`) which calls
`_detect_external_drift` then re-reads from disk under the lock. Drift means
some external writer (patch tool, shell append, manual edit, sister session) put
content into the file that a tool rewrite would mangle or truncate (scar:
issue #26045, `:538`). Two drift signals (`:556-562`):
1. **Round-trip mismatch** — `raw.strip() != ENTRY_DELIMITER.join(parsed)`; tool
   writes are normalized so always round-trip, anything else flags.
2. **Entry-size overflow** — `max(len(e) for e in parsed) > char_limit`; the tool
   budgets the WHOLE store against the limit, so no single tool-written entry can
   exceed it. A single oversized parsed entry = external free-form append.

On drift: snapshot the file to `<name>.bak.<unix_ts>` via
`path.with_suffix(path.suffix + f".bak.{ts}")` (`:569-570`),
`bak_path.write_text(raw)`. If the backup itself fails, return the path string
plus `" (BACKUP FAILED — file unchanged on disk)"` (`:573-574`). Returns the bak
path string; None when clean.

When `_reload_target` returns a non-None bak, the mutation is REFUSED — every op
checks `if bak: return _drift_error(...)` (`:313-315,364-366,421-423`).
`_drift_error` (`:83-110`) returns `{success:False, error:<full explanation>,
drift_backup:<bak>, remediation:<integrate entries one at a time via
memory(add)>}`.

## 1.5 Over-limit handling — CONSOLIDATE, never truncate (`add`: `:328-341`; `replace`: `:394-406`)

When an `add` would push the store over the char limit, hermes does NOT truncate.
It returns `success:False` with:
- `error`: "Memory at {current}/{limit} chars. Adding this entry ({n} chars)
  would exceed the limit. Consolidate now: use 'replace' to merge overlapping
  entries into shorter ones or 'remove' stale or less important entries (see
  current_entries below), then retry this add — all in this turn." (`:332-338`)
- `current_entries`: the full live list (`:339`)
- `usage`: `"{current}/{limit}"` (`:340`)

`replace` over-budget is symmetric (`:394-406`): same `current_entries` + `usage`,
error tells the model to shorten the new content or remove other entries, then
retry in this turn.

Other `add`/`replace`/`remove` behaviors:
- `add` rejects empty content (`:300-301`) and EXACT duplicates — returns success
  with "Entry already exists (no duplicate added)." (`:321-322`).
- `replace`/`remove` use SUBSTRING match: `[(i,e) for … if old_text in e]`
  (`:369,426`). No match → error. Multiple DISTINCT matches → refuse with
  `matches` previews (80-char) and "Be more specific." (`:374-383,431-440`).
  Multiple IDENTICAL matches (exact dup entries) → operate on the FIRST
  (`:384-386,441-443`). `replace` with empty `new_content` is rejected ("Use
  'remove'…", `:356`); LunaMoth currently treats empty replace as delete (delta).

## 1.6 Threat scanning (injection/exfil guard) — hermes-only, port for parity

- WRITE-TIME: `add`/`replace` scan incoming content with
  `_scan_memory_content` → `first_threat_message(content, scope="strict")`
  (`:78-80,304-306,358-361`). Any hit → `{success:False, error:<msg>}`, write
  refused.
- LOAD-TIME (snapshot): `_sanitize_entries_for_snapshot` (`:172-206`) scans each
  entry with `scan_for_threats(entry, scope="strict")`. A hit replaces the entry
  text IN THE SNAPSHOT ONLY with a `[BLOCKED: <file> entry contained threat
  pattern(s): <ids> …]` placeholder (`:198-203`). Live `memory_entries`/
  `user_entries` keep the RAW text so the user can still `read`+`remove` it
  (`:160-164`). Scanning is deterministic from disk bytes, so the snapshot stays
  byte-stable for the whole session (prefix-cache invariant, `:146-148`).
- Patterns live in `tools/threat_patterns.py` (shared lib): `first_threat_message`
  / `scan_for_threats`, scope `"strict"` (broadest set).

## 1.7 Prompt injection — frozen snapshot (`format_for_system_prompt`, `:450-461`; `_render_block`, `:482-498`)

- Snapshot is captured ONCE at `load_from_disk()` (`:132-170`) into
  `_system_prompt_snapshot = {"memory": <rendered>, "user": <rendered>}`.
  Mid-session writes hit disk + tool response but DO NOT touch this snapshot —
  the system prompt is byte-stable across all turns, preserving the prefix cache
  (`:11-14,452-458`).
- The snapshot refreshes only on next session start, OR on
  `invalidate_system_prompt` after a compression event, which calls
  `memory_store.load_from_disk()` again (`system_prompt.py:372-380`).
- `_render_block` (`:482-498`) format, per store:
  - separator line `"═" * 46`
  - header: `USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]`
    or `MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]`
  - separator again, then `ENTRY_DELIMITER.join(entries)` (entries shown
    §-delimited, raw).
- Injected in the VOLATILE tier of the system prompt (`system_prompt.py:307-318`):
  `memory` block when `_memory_enabled`, `user` block always when
  `_user_profile_enabled`. Empty snapshot → block omitted (`:460-461`).

## 1.8 user-profile vs memory distinction

- `USER.md` / target `user` = facts ABOUT the operator (name, role, prefs,
  comm style, workflow habits) — `:8-9`, schema `:678`. Smaller budget (1375).
  Header "USER PROFILE (who the user is)".
- `MEMORY.md` / target `memory` = the agent's OWN notes (environment facts,
  project conventions, tool quirks, lessons) — `:7-8`, schema `:679`. Larger
  budget (2200). Header "MEMORY (your personal notes)".
- They are separate files, separate budgets, separate prompt blocks, but ONE
  `memory` tool selects between them via `target`.

---

# PART 2 — SKILLS (`skills_list`, `skill_view`, `skill_manage`)

Hermes splits skills across TWO modules: read side in `tools/skills_tool.py`
(`skills_list`, `skill_view`), mutate side in `tools/skill_manager_tool.py`
(`skill_manage`). All three register under `toolset="skills"`.

## 2.1 `skills_list` (`skills_tool.py:680-745`, schema `:1525-1538`)

Progressive-disclosure TIER 1: returns name + description + category only.

```
name: "skills_list"
parameters:
  category: string (optional) — narrow to one category
required: []
```

Behavior: `_find_all_skills()` (`:595-672`) recursively scans `SKILLS_DIR` then
external dirs; first-seen name wins (`seen_names`, `:638`); skips disabled
(`:640`), platform-mismatched (`:631`), environment-irrelevant (`:634`) skills.
Description falls back to first non-`#` body line if frontmatter omits it
(`:643-649`), truncated to `MAX_DESCRIPTION_LENGTH=1024` (`:651-652`). Sorted by
(category, name) (`_sort_skills`, `:675-677`). Returns JSON
`{success, skills:[{name,description,category}], categories, count, hint}`
(`:733-742`). Registered `emoji="📚"` (`:1559-1568`).

## 2.2 `skill_view` (`skills_tool.py:855-1473`, schema `:1540-1557`)

Progressive-disclosure TIER 2-3: load one skill's full SKILL.md, or one linked
file inside it.

```
name: "skill_view"
parameters:
  name: string (REQUIRED) — skill name; or "plugin:skill" qualified; or
        "category/skill" relative path
  file_path: string (optional) — linked file e.g. "references/api.md",
        "templates/config.yaml", "scripts/validate.py"; omit for SKILL.md
required: ["name"]
```

Resolution + safety (port the core, the plugin/gateway layers are hermes-infra):
- `_skill_lookup_path_error` (`:111-135`): name must be RELATIVE, no `..`
  traversal, not an absolute/Windows-drive path (the `:` would be misread as a
  plugin namespace).
- Qualified `plugin:skill` names route to the plugin registry (`:896-959`) —
  LunaMoth has no plugins; this branch is droppable.
- Bare/categorized names: search `SKILLS_DIR` + external dirs with 3 strategies —
  direct path, recursive by parent-dir name, legacy flat `<name>.md`
  (`:1015-1043`). COLLISION DETECTION: >1 candidate across dirs → refuse with
  `matches` + hint, never guess (`:1045-1067`).
- Security warnings (log only, still serves): file outside trusted skills dirs;
  `_INJECTION_PATTERNS` (`:161-171`) substring hit (`:1112-1123`).
- `file_path` request: traversal-guarded (`has_traversal_component` +
  `validate_within_dir`, `:1157-1182`); missing file → list available files
  bucketed into references/templates/assets/scripts/other (`:1184-1227`); binary
  → returns a `[Binary file …]` notice (`:1232-1243`).
- Main SKILL.md result (`:1416-1439`): `{success, name, description, tags,
  related_skills, content, path, skill_dir, linked_files, usage_hint,
  required_environment_variables, missing_*…, setup_needed, readiness_status}`.
  `linked_files` enumerates `references/*.md`, `templates/*`, `assets/*`,
  `scripts/*` (`:1259-1325`).
- On success the registry wrapper `_skill_view_with_bump` (`:1569-1591`) bumps
  view+use telemetry (curator). Droppable for LunaMoth unless porting the curator.

## 2.3 `skill_manage` (`skill_manager_tool.py:825-902`, schema `:909-1021`)

The agent's procedural-memory mutator. Six actions.

```
name: "skill_manage"
parameters:
  action:      string, enum ["create","patch","edit","delete","write_file","remove_file"]  (REQUIRED)
  name:        string (REQUIRED) — kebab-ish, max 64; must exist for patch/edit/delete/write_file/remove_file
  content:     string — full SKILL.md (frontmatter+body); required for create & edit
  old_string:  string — text to find; required for patch (unique unless replace_all)
  new_string:  string — replacement; required for patch (empty string = delete matched text)
  replace_all: boolean — patch all occurrences (default false)
  category:    string — optional single-segment subdir; create only
  file_path:   string — supporting file under references/|templates/|scripts/|assets/
               (write_file/remove_file); patch defaults to SKILL.md if omitted
  file_content:string — content for write_file
  absorbed_into:string — delete only; curator intent (umbrella name | "" | omit)
required: ["action", "name"]
```

Action handlers:
- **create** (`_create_skill`, `:485-539`): validate name (`VALID_NAME_RE =
  ^[a-z0-9][a-z0-9._-]*$`, `:168`, max 64), category (single segment, `:192-214`),
  frontmatter (`:217-253`), content size (`MAX_SKILL_CONTENT_CHARS=100_000`,
  `:164,256-268`). Collision across ALL dirs → refuse (`:506-511`). Writes
  `<dir>/SKILL.md` atomically (`_atomic_write_text`, `:449-478` — mkstemp +
  `atomic_replace`). Security scan, rollback on block (`:521-525`).
- **edit** (`_edit_skill`, `:542-572`): full SKILL.md rewrite of an existing skill
  (any origin). Backs up original, writes, scans, rolls back on block.
- **patch** (`_patch_skill`, `:575-666`): fuzzy find-and-replace
  (`fuzzy_find_and_replace`, `:619-623`) within SKILL.md (default) or a supporting
  file (`file_path`). Unique match unless `replace_all`. Re-validates frontmatter
  if patching SKILL.md (`:646-652`). Atomic write + scan + rollback.
- **delete** (`_delete_skill`, `:669-723`): `shutil.rmtree` the skill dir; cleans
  empty category dir (`:711-714`). `_pinned_guard` blocks deletion of pinned
  skills (`:137-161,685-687`). `absorbed_into` declares curator intent and, when
  non-empty, must name an existing umbrella skill (`:689-705`).
- **write_file** (`_write_file`, `:726-775`): add/overwrite a supporting file;
  must be under `ALLOWED_SUBDIRS={references,templates,scripts,assets}` (`:171`);
  byte cap `MAX_SKILL_FILE_BYTES=1_048_576` (1 MiB, `:165,736-745`).
- **remove_file** (`_remove_file`, `:778-818`): delete a supporting file; cleans
  empty subdir.

On any successful mutation (`:877-900`): clears the skills system-prompt cache
(`clear_skills_system_prompt_cache(clear_snapshot=True)`), and runs curator
telemetry (`bump_patch`/`forget`/`mark_agent_created`). `mark_agent_created` only
fires when `is_background_review()` (`skill_provenance.py`) — i.e. the
self-improvement review fork — so user-directed foreground `create` calls stay
"user-owned" and the curator never auto-prunes them.

Security scan (`_security_scan_skill`, `:78-102`): runs `scan_skill` only when
`skills.guard_agent_created` config is on (default OFF, `:59-76` — the agent can
already run the same code via terminal, so the scan is opt-in).

## 2.4 SKILL.md format hermes expects (`skills_tool.py:28-50`, `_validate_frontmatter`, `skill_manager_tool.py:217-253`)

YAML frontmatter (real `yaml.safe_load`, `:235`) between `---` fences, then a
markdown body. Required: `name` (≤64), `description` (≤1024). Optional: `version`,
`license`, `platforms: [macos|linux|windows]`, `prerequisites.{env_vars,commands}`,
`required_environment_variables`, `required_credential_files`, `compatibility`,
`metadata.hermes.{tags,related_skills}`. Validation requires: starts with `---`,
closing `---`, frontmatter is a mapping with name+description, AND a non-empty
body after the frontmatter (`:222-251`).

## 2.5 Skills storage layout & discovery (`skills_tool.py:87-91`)

- SINGLE root: `SKILLS_DIR = get_hermes_home() / "skills"` (`:90-91`). Bundled,
  hub-installed, and agent-created skills ALL coexist here (seeded from the repo's
  `skills/` on install) — there is no separate "own vs bundled vs user" search
  tier the way LunaMoth has. Plus optional read-only `skills.external_dirs` from
  config (local takes precedence on name collisions, `:1098-1101`).
- A skill = a directory holding `SKILL.md` plus optional `references/`,
  `templates/`, `scripts/`, `assets/` subdirs; categories are one nesting level
  (`<root>/<category>/<skill>/SKILL.md`, `:14-26`).
- Discovery: recursive scan for `SKILL.md` (`iter_skill_index_files`), excluding
  `_EXCLUDED_SKILL_DIRS`; frontmatter `[:4000]` chars parsed for the index
  (`:628-629`).

## 2.6 Skills prompt injection (`prompt_builder.py:1085-1313`, wired at `:185-201`)

- `build_skills_system_prompt` produces a COMPACT index, grouped by category.
  Two-layer cache: in-process LRU keyed by (skills_dir, external dirs, tools,
  toolsets, platform, disabled) + disk snapshot `.skills_prompt_snapshot.json`
  validated by an mtime/size manifest of every `SKILL.md`/`DESCRIPTION.md`
  (`:944-983,1127-1206`) — survives restarts. `skill_manage` invalidates it on
  every mutation.
- Output format (`:1259-1307`): a `## Skills (mandatory)` header with strong
  "scan before replying, load with skill_view if even partially relevant, err on
  the side of loading, patch if broken, offer to save after hard tasks"
  guidance, then `<available_skills>` … `</available_skills>` listing each
  category and its `- name: description` lines.
- It sits in the STABLE tier of the system prompt (cache-friendly) when any of
  `skills_list`/`skill_view`/`skill_manage` is enabled (`system_prompt`-side
  `:185-201`).

---

# PART 3 — LunaMoth's CURRENT state (the baseline to replace)

## 3.1 Current memory (`src/lunamoth/tools/memory.py`)

- Already a "frozen-snapshot two-store" mirroring hermes in spirit. Delimiter
  `ENTRY_DELIM="\n§\n"` (`:25`), targets `("memory","user")` (`:26`).
- Storage: `MemoryStore(SANDBOX_ROOT / "memory", …)` (`agent.py:121`), files
  `<root>/memory.md` and `<root>/user.md` (`_path`, `memory.py:46-49`) — **lower-case
  filenames**, under the per-chara **sandbox**, not `~/.hermes/memories/`.
- Limits: `MemoryLimits(memory_chars=4000, user_chars=2000)` (`:30-32`) — bigger
  than hermes (2200/1375); card-settable via `extensions.lunamoth.{memory_chars,
  user_chars}` (`agent.py:268-273`).
- Drift guard PRESENT and hermes-faithful: `_detect_drift` (`:58-91`), same two
  signals, `<name>.bak.<ts>` backup, refuse-and-raise on `_write` (`:102-110`).
- Atomic write PRESENT: mkstemp → write → flush → `os.fsync` → `os.replace`,
  unlink-on-failure, RuntimeError on OSError (`:132-147`).
- Over-limit: whole-store backstop drops OLDEST entries while >1 (`:118-120`),
  then a SINGLE over-cap entry is REJECTED with consolidate-style guidance
  (`:121-127`) — close to hermes's intent but the message and the drop-oldest
  backstop differ from hermes (which never drops silently; it refuses the add and
  returns `current_entries`).
- `tool_memory` (`gateway.py:243-262`): actions add/replace/remove, target
  default "memory"; **`replace` with empty content deletes** (`memory.py:166-169`)
  — hermes REJECTS empty replace.
- NO threat scanning anywhere (confirmed: no `threat`/`scan_for_threats` import in
  `src/lunamoth/tools/`).
- Prompt injection: `agent._freeze_memory()` snapshots `memory.snapshot()`
  (`agent.py:303-307`); `_memory_text()` renders a BILINGUAL block with
  "Your memory (notes you've kept for yourself):" / "About the operator:" headers
  and `- entry` bullets (`agent.py:309-323`), injected in `_volatile_tail`
  (`agent.py:476-477`). NO `═`-bordered header, NO `§` join, NO usage % in prompt.

## 3.2 Current skills (`src/lunamoth/tools/skills.py`)

- THREE search tiers, first-hit-wins: `<sandbox>/workspace/skills/` (own) →
  `~/.lunamoth/skills/` (user) → `<repo>/skills/` (bundled) (`:53-60`).
- Tools: `read_skill(name)` and `create_skill(name, description, content)` ONLY
  (`gateway.py:276-285,461-484`). No `skills_list`, no `skill_view` file_path, no
  `skill_manage`, no patch/edit/delete/write_file/remove_file.
- `create` writes ONLY to the OWN dir; engine owns the frontmatter
  (`name`+`description`, two fields), tiny regex parser, no real YAML (`:39-50,
  111-143`). Name regex `^[a-z0-9][a-z0-9-]{0,63}$` (kebab only, no `.`/`_`).
- Cap `MAX_SKILL_CHARS=24_000` (`:36`); over-cap create is REJECTED (`:134-139`);
  over-cap read returns head + explicit notice (`:99-105`).
- No categories, no `references/templates/scripts/assets`, no linked files, no
  collision refusal, no platform/disabled/env gating, no security scan, no
  external_dirs.
- Prompt injection: `render_block()` (`:147-160`) → "Skills available to you
  (read_skill(name) …)" + `  name (yours) — desc` lines + a create_skill nudge;
  frozen via `agent._freeze_skills()` (`agent.py:423-424`), injected
  (`agent.py:480-483`). No `## Skills (mandatory)` / `<available_skills>` block,
  no category grouping, no snapshot cache.

---

# PART 4 — THE DELTA (what changes to become hermes-identical)

## 4.1 Memory delta

| Aspect | LunaMoth now | Hermes target | Action |
|---|---|---|---|
| Dir | `SANDBOX_ROOT/memory/` | `~/.hermes/memories/` | Decide: keep per-chara sandbox dir (recommended for LunaMoth's one-process-one-chara model) OR move to a `LUNAMOTH_HOME/memories`. Resolve dir DYNAMICALLY per call, not at import (`memory_tool.py:51-57`). |
| Filenames | `memory.md`, `user.md` | `MEMORY.md`, `USER.md` | Rename to upper-case. Migration: rename existing files. |
| Limits | 4000 / 2000 | 2200 / 1375 | Lower defaults to hermes values (keep card override hook). |
| `replace` empty content | deletes entry | rejected ("Use 'remove'") | Reject empty `new_content`. |
| Over-limit | drop-oldest backstop then reject | NEVER drop; reject add/replace with `current_entries`+`usage`, "Consolidate now… all in this turn" | Drop the drop-oldest loop; return hermes's consolidate payload (`memory_tool.py:328-341,394-406`). |
| Duplicate add | appends | returns success "Entry already exists (no duplicate added)" | Add the exact-dup short-circuit. |
| Multi-match replace/remove | first match | refuse if matches DISTINCT (80-char previews); first if identical dups | Add multi-match refusal. |
| File lock | none | `.lock` sidecar flock per RMW | Add `_file_lock`. |
| Reload-under-lock | none | re-read fresh under lock before mutate | Add `_reload_target`. |
| Threat scan | none | write-time `first_threat_message(strict)` refuse; load-time `scan_for_threats` → `[BLOCKED:…]` placeholder in snapshot, raw kept in live | Port `tools/threat_patterns.py` (strict scope) + both scan points. Biggest new surface. |
| Tool schema | `required:["action"]`, target default "memory" | `required:["action","target"]` | Make `target` required (or keep LunaMoth default for ergonomics — note the divergence). |
| Success response | `{ok,target,entries,usage}` | `{success,target,entries,usage:"{pct}% — {c}/{l} chars",entry_count,message}` | Match hermes response shape. |
| Prompt block | bilingual bullets, no header rule | `═`×46 border + `MEMORY (your personal notes) [pct% — c/l chars]` / `USER PROFILE (who the user is) […]` + `§`-joined entries | Replace `_memory_text()` with hermes `_render_block`. (LunaMoth may keep bilingual headers as a localized variant — note as an allowed divergence under the "language is a card property" rule.) |
| Drift guard, atomic write | already faithful | same | KEEP as-is (already cites `:522-575` and `:577-606`). |

## 4.2 Skills delta

| Aspect | LunaMoth now | Hermes target | Action |
|---|---|---|---|
| Tools | `read_skill`, `create_skill` | `skills_list`, `skill_view`, `skill_manage` | Replace the two tools with the three hermes tools (schemas in 2.1-2.3). |
| Storage tiers | own / user / bundled (3 dirs, sandbox+home+repo) | SINGLE `~/.hermes/skills/` (+ read-only external_dirs) | Collapse to one writable root (e.g. `LUNAMOTH_HOME/skills` or per-chara sandbox skills dir) + optional external_dirs. Migration: merge the 3 tiers into the one root (own-first wins, mirroring current shadow order). |
| SKILL.md format | name+desc only, regex parser | full YAML frontmatter (yaml.safe_load), body required | Adopt real-YAML frontmatter + `_validate_frontmatter`. |
| Name regex | `[a-z0-9][a-z0-9-]{0,63}` | `[a-z0-9][a-z0-9._-]*` ≤64 | Allow `.`/`_`. |
| Categories | none | single-segment subdir | Add `category` param + `<root>/<cat>/<skill>/`. |
| Supporting files | none | references/templates/scripts/assets + linked_files | Add subdir support + `skill_view(file_path)` + `write_file`/`remove_file`. |
| Mutations | create (own only) | create/edit/patch/delete/write_file/remove_file | Add the full action set incl. fuzzy patch, atomic write+rollback. |
| Collision | first-hit silent | refuse with `matches` | Add collision refusal in `skill_view`. |
| Caps | 24_000 / SKILL.md | 100_000 SKILL.md, 1 MiB file | Raise to hermes caps. |
| Prompt block | "Skills available to you…" flat | `## Skills (mandatory)` + `<available_skills>` category-grouped + snapshot cache | Replace `render_block()` with `build_skills_system_prompt` output + the disk snapshot cache. |
| Security scan / curator / provenance | none | opt-in scan, telemetry, background-review provenance | OPTIONAL — port only if LunaMoth wants the curator. Note as deferrable. |

## 4.3 Migration for an existing chara on disk

Memory:
1. Move/rename `<SANDBOX_ROOT>/memory/memory.md` → `MEMORY.md`,
   `user.md` → `USER.md` (under whichever root is chosen). Content already
   `§`-delimited and round-trip clean (current `_write` normalizes the same way),
   so no re-parse needed.
2. If an entry exceeds the NEW (lower) 2200/1375 cap, the drift guard's
   entry-size signal will fire on first write — expected; the operator merges via
   `memory(add)` per the drift remediation. Alternatively run a one-time re-cap
   (current `set_limits(trust_disk=True)`) before switching defaults.

Skills:
1. Copy the three current tiers into the single hermes root, own-first so the
   chara's own skills win on name collision (preserves today's shadow order).
2. Existing SKILL.md files already use `---\nname:\ndescription:\n---\nbody`
   frontmatter — valid under the new YAML parser as-is. No body-less skills exist
   (current `create` rejects empty body), so `_validate_frontmatter` passes.
3. Drop the per-chara `workspace/skills/` write target in favor of the single
   root; update `agent.py` SkillStore construction accordingly.

---

# 8-LINE SUMMARY

1. Hermes MEMORY = one `memory` tool, schema `{action:add|replace|remove, target:memory|user, content, old_text}` (required action+target; NO read action), two `§`-delimited files `~/.hermes/memories/{MEMORY.md,USER.md}` budgeted by CHAR limit (2200/1375).
2. Durability = `.lock` sidecar flock for RMW + mkstemp→write→flush→fsync→`os.replace` atomic write (`:577-606`); reads are lockless because rename is atomic.
3. Drift guard (`:522-575`) refuses any mutation when the on-disk file fails round-trip OR has an entry over the cap (external append), backing it up to `<name>.bak.<ts>`; over-limit adds CONSOLIDATE (return `current_entries`+`usage`, never truncate); plus strict threat-scan at write (refuse) and load (`[BLOCKED:…]` placeholder in the frozen snapshot only).
4. Memory injects as a FROZEN snapshot (`═`×46 header `MEMORY (your personal notes)`/`USER PROFILE (who the user is)` + usage%) captured once per session for prefix-cache stability.
5. Hermes SKILLS = `skills_list` (tier-1 name+desc+category), `skill_view` (tier-2/3 full SKILL.md or linked file, collision-refusing), `skill_manage` (create/edit/patch/delete/write_file/remove_file) over ONE root `~/.hermes/skills/<cat>/<skill>/SKILL.md` with real-YAML frontmatter (name≤64, description≤1024) + references/templates/scripts/assets; injected as a cached `## Skills (mandatory)` `<available_skills>` category index.
6. LunaMoth memory DELTA: already a faithful two-store with drift guard + atomic write, but lower-case `memory.md`/`user.md` under the sandbox, 4000/2000 limits, empty-`replace`=delete, drop-oldest backstop, no file-lock/reload/threat-scan, and a bilingual bullet prompt block — change to UPPER-case `MEMORY.md`/`USER.md`, 2200/1375, reject empty replace, hermes consolidate payload, add lock+reload+threat-scan, and the `═`/usage% block.
7. LunaMoth skills DELTA: only `read_skill`+`create_skill` over three sandbox/home/repo tiers with a two-field regex-parsed frontmatter, 24k cap, no categories/linked-files/patch/edit/delete — replace with the three hermes tools, one root, real-YAML frontmatter, categories + supporting subdirs, full mutation set, 100k/1MiB caps, collision refusal, and the `## Skills (mandatory)` cached index.
8. Migration: rename memory files to upper-case (content is already `§`-clean; re-cap or let the drift guard catch over-cap entries), and merge the three skill tiers into the single root own-first (existing SKILL.md frontmatter already validates under YAML); curator/provenance/security-scan are optional deferrable ports.
