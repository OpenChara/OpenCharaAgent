# Re-implementation spec: `patch` and `search_files` (hermes-agent → LunaMoth)

Faithful, apple-to-apple contract for re-building two hermes-agent file tools against a
different runtime. **Source tree:** `reference/hermes-agent/`. Every behavior below is cited
`file:line`. Three source files matter:

- `tools/file_tools.py` — tool entrypoints, JSON schemas, registry wiring, loop-guards, path
  resolution, sensitive-path guards, the dropped-arg guard.
- `tools/file_operations.py` — `ShellFileOperations`: the actual replace/V4A/search/lint
  machinery (shells out through a terminal-env `execute(cmd, cwd)` seam).
- `tools/fuzzy_match.py` — the 9-strategy fuzzy matcher used by `mode=replace` and V4A.
- `tools/patch_parser.py` — V4A grammar parser + two-phase apply.

> **Porting note for LunaMoth.** Hermes resolves paths against a *live terminal cwd* /
> `$TERMINAL_CWD` (worktree discipline). LunaMoth runs one chara per process inside an
> OS jail (`SANDBOX_ROOT`), executing through `tools/runner.py` + `tools/gateway.py`. When
> porting, replace the hermes `_resolve_path_for_task` / `$TERMINAL_CWD` machinery with
> resolution against the sandbox root, and replace `ShellFileOperations._exec` (which shells
> to the terminal backend) with LunaMoth's gateway/runner. The *behaviors* (fuzzy chain,
> V4A grammar, output shapes, error strings) must be preserved verbatim; the *path/exec
> plumbing* is the part you re-anchor.

---

## 0. Shared infrastructure both tools rely on

### 0.1 Output cap — 100K chars (registry-level)
Both tools (and `read_file`/`write_file`) are registered with
`max_result_size_chars=100_000` (`file_tools.py:1583-1586`). This is a **registry-level cap
on the JSON tool result string**, enforced by the dispatcher via
`registry.get_max_result_size_chars` (`tools/registry.py:425-426`), *not* inside the tool
body. The tool returns its full JSON; the registry truncates the serialized result to 100K
chars before it enters context. Re-implement as a post-serialization truncation on the
returned string keyed per-tool.

Separately, `read_file` has its own *input-side* char guard (`_DEFAULT_MAX_READ_CHARS =
100_000`, `file_tools.py:35`, configurable via `file_read_max_chars`) — that is a read-only
concern, **not** part of `patch`/`search_files`. Mentioned only to disambiguate: the 100K
that touches these two tools is the registry result cap.

### 0.2 `_check_file_reqs` (the `check_fn`)
`file_tools.py:1423-1426`. A lazy wrapper that defers to `tools.check_file_requirements()`
(avoids a circular import with `tools/__init__.py`). Registered as the `check_fn` for all
four file tools (`:1583-1586`). It is a **preflight gate**: the registry calls it before
dispatch to confirm file-tool prerequisites are met (e.g. a terminal/file backend is
available); on failure the tool is reported unavailable rather than dispatched. For the port:
a no-op/availability check that returns truthy when the sandbox FS seam is wired.

### 0.3 Path normalization (absolute / relative / `~`)
Two layers, applied at different stages:

**Tool layer** (`file_tools.py`): `_resolve_path_for_task(filepath, task_id)`
(`:199-208`): `Path(filepath).expanduser()`; if absolute → `.resolve()` unanchored; else
resolved against `_resolve_base_dir(task_id)` (`:165-196`) which is **always absolute** —
live terminal cwd > sentinel-free absolute `$TERMINAL_CWD` > process cwd. Sentinel
`$TERMINAL_CWD` values `{"", ".", "./", "auto", "cwd"}` are rejected as anchors
(`:96`, `_configured_terminal_cwd` `:99-113`). A relative path resolving *outside* the
workspace root yields a non-blocking `_warning` (`_path_resolution_warning` `:211-245`).
**For LunaMoth: anchor against the sandbox root; the worktree-divergence warning is
optional but the sandbox-escape check is mandatory.**

**Shell layer** (`file_operations.py`): `_expand_path` (`:797-832`) expands `~`, `~/...`,
and validated `~user/...` by shelling `echo $HOME` / `echo ~user` (username must match
`[a-zA-Z0-9._-]+`, else left literal — injection guard). `_escape_shell_arg` (`:834-837`)
single-quotes args, escaping embedded single quotes as `'"'"'`. **`_expand_path` runs
BEFORE escaping** because `~` does not expand inside single quotes.

### 0.4 Per-path locking, staleness, cross-agent registry (tool layer)
Both `patch_replace` callers and the V4A path acquire `file_state.lock_path(resolved)` so
concurrent sub-agents can't interleave on the same file; multi-file V4A locks **all** target
paths in **sorted order** via `ExitStack` to avoid deadlock (`file_tools.py:1216-1237`).
Warnings (non-blocking) are attached as `result_dict["_warning"]`, priority:
cross-agent registry stale (`file_state.check_stale`) > per-task staleness
(`_check_file_staleness`) > workspace-divergence (`_path_resolution_warning`)
(`:1239-1256`, `:1280-1281`). The response always reports the **absolute** path(s) actually
written (`resolved_path` / `files_modified`, `:1282-1293`). **These are hermes
multi-agent-hygiene features; LunaMoth (one chara/process) can simplify but should keep the
"report the absolute resolved path" behavior so a mis-resolved write is visible.**

---

## 1. `patch` — str-replace + V4A apply-patch

Entrypoints: schema `PATCH_SCHEMA` (`file_tools.py:1460-1509`); handler `_handle_patch`
(`:1562-1569`); core `patch_tool` (`:1174-1342`); replace impl
`ShellFileOperations.patch_replace` (`file_operations.py:1367-1488`); V4A impl `patch_v4a`
(`:1490-1518`) → `tools/patch_parser.py`. Registry: `name="patch"`, emoji `🔧`,
`max_result_size_chars=100_000` (`:1585`).

### 1.1 JSON schema (every param)

`required: ["mode"]` (`file_tools.py:1507`). All params (`:1471-1506`):

| param | type | enum | default | required-when |
|---|---|---|---|---|
| `mode` | string | `["replace","patch"]` | `"replace"` | always (in `required`) |
| `path` | string | — | none | **replace** mode |
| `old_string` | string | — | none | **replace** mode |
| `new_string` | string | — | none (`""` = delete matched text) | **replace** mode |
| `replace_all` | boolean | — | `false` | optional (replace mode) |
| `patch` | string | — | none | **patch** mode |
| `cross_profile` | boolean | — | `false` | optional (opt out of cross-profile soft guard) |

Schema-declared requirements are **prose only** (`mode` is the sole JSON-`required` entry);
the actual per-mode requirements are enforced in `patch_tool` body (§1.2). Description string
(`:1462-1470`) advertises "Uses fuzzy matching (9 strategies)", "Returns a unified diff",
"Auto-runs syntax checks after editing".

### 1.2 `patch_tool` control flow (`file_tools.py:1174-1342`)

1. **Collect paths to check** (`:1184-1206`): always `path` if set; in `mode=="patch"`,
   regex-scan the patch body for V4A headers
   `^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$` (MULTILINE) and add each.
   - **Traversal guard on V4A headers only** (`:1200-1205`): if a V4A header path contains a
     `..` component (`path_security.has_traversal_component`, `path_security.py:37-43`), return
     `tool_error("V4A patch header contains '..' traversal: {path!r}. Use the agent's
     cwd-relative path (no '..') or an absolute path in '*** Update File:' / '*** Add File:' /
     '*** Delete File:' headers.")`. The explicit `path=` arg is intentionally **exempt**
     (agents use `../` legitimately).
2. **Sensitive-path + cross-profile guards per collected path** (`:1207-1214`):
   `_check_sensitive_path` (`:315-342`) blocks `/etc/ /boot/ /usr/lib/systemd/ /private/etc/
   /private/var/`, the docker sockets, and the hermes config file; `_check_cross_profile_path`
   (`:379-436`) is a soft guard (skipped when `cross_profile=True`). Both return `tool_error(...)`
   on hit. **LunaMoth: replace with sandbox-escape + allowlist checks; cross-profile is hermes-specific.**
3. **Resolve + sort + lock** all paths (`:1216-1237`, see §0.4).
4. **Gather staleness/divergence warnings** (`:1239-1256`).
5. **Dispatch on mode** (`:1260-1277`):
   - `mode=="replace"`: require `path` else `tool_error("path required")`; require
     `old_string is not None and new_string is not None` else
     `tool_error("old_string and new_string required")`; call
     `file_ops.patch_replace(resolved_abs_path, old_string, new_string, replace_all)`
     (passes the **resolved absolute** path so tool & shell layers agree).
   - `mode=="patch"`: require `patch` else `tool_error("patch content required")`; call
     `file_ops.patch_v4a(patch)`.
   - else: `tool_error(f"Unknown mode: {mode}")`.
6. **Post-success bookkeeping** (`:1280-1304`): attach `_warning` (joined with ` | ` if
   multiple), set `files_modified` to resolved abs paths, `resolved_path` when single,
   refresh read-timestamps, `file_state.note_write`, and `_reset_patch_failures` for touched
   paths.
7. **No-match escalation hint** (`:1305-1339`): if `result_dict["error"]` contains
   `"Could not find"`, in replace mode bump a per-`(task_id, resolved_path)` consecutive
   failure counter (`_record_patch_failure`, `:478-492`, cap 64 paths/task).
   - `failure_count >= 3` → attach the escalating `_hint` ("This is failure #N patching
     {path!r}. Stop retrying with variations… (1) re-read … (2) longer/unique old_string …
     (3) write_file …", `:1326-1334`).
   - else, **unless** the error already contains `"Did you mean one of these sections?"`,
     attach generic `_hint` ("old_string not found. Use read_file to verify the current
     content, or search_files to locate the text.", `:1336-1339`).
8. Return `json.dumps(result_dict, ensure_ascii=False)` (`:1340`). Top-level
   `except Exception as e: return tool_error(str(e))` (`:1341-1342`).

### 1.3 Dropped-arg guard
**`patch` has NO explicit dropped-arg guard in its handler.** `_handle_patch`
(`:1562-1569`) passes args straight through (`args.get(...)`), and the **missing-arg errors
are produced inside `patch_tool`** (`"path required"`, `"old_string and new_string
required"`, `"patch content required"`, §1.2 step 5). Contrast `write_file` (`_handle_write_file`,
`:1536-1559`) which *does* have an explicit dropped-arg guard returning the verbose
`"write_file: missing required field 'content'… almost always a dropped-arg bug under context
pressure…"` message. **Port: replicate `write_file`'s richer message style if desired, but
the as-built `patch` contract is the three terse strings above.** Note `cross_profile` is
coerced `bool(args.get("cross_profile", False))` in the handler (`:1568`).

### 1.4 `mode=replace` behavior — `patch_replace` (`file_operations.py:1367-1488`)

1. `_expand_path(path)` (`:1382`).
2. `_is_write_denied(path)` → `PatchResult(error=f"Write denied: '{path}' is a protected
   system/credential file.")` (`:1385-1386`).
3. **Read current content**: `cat {escaped} 2>/dev/null` (`:1389-1390`); on non-zero exit →
   `PatchResult(error=f"Failed to read file: {path}")` (`:1392-1393`). **Strip leading
   UTF-8 BOM** before matching (`_strip_bom`, `:1401`) — `write_file` restores the BOM later.
4. `fuzzy_find_and_replace(content, old_string, new_string, replace_all)` (`:1404-1408`,
   §1.6).
5. **On error or `match_count == 0`** (`:1410-1417`): `err_msg = error or f"Could not find
   match for old_string in {path}"`, then append `format_no_match_hint(...)` (a
   `"\n\nDid you mean one of these sections?\n…"` snippet, gated to genuine not-found errors;
   `fuzzy_match.py:842-860`). Return `PatchResult(error=err_msg)`.
6. **Line-ending preservation** (`:1419-1429`): detect file's ending (`_detect_line_ending`),
   normalize whole `new_content` back to it (model sends LF in JSON; file may be CRLF).
7. **Write back** via `self.write_file` (`:1432`); on error →
   `PatchResult(error=f"Failed to write changes: {write_result.error}")`.
8. **Post-write verification** (`:1436-1466`): re-`cat`, fail with
   `"Post-write verification failed: could not re-read {path}"` if unreadable; else
   normalize line endings + drop BOM on both sides and compare. On mismatch return
   `PatchResult(error=f"Post-write verification failed for {path}: on-disk content differs
   from intended write (wrote N chars, read back M chars after normalizing line endings).
   The patch did not persist. Re-read the file and try again.")`.
9. **Unified diff** `_unified_diff(content, new_content, path)` (`:1469`, see §1.8).
10. **Syntax check (delta)** `_check_lint_delta(path, pre_content=content,
    post_content=new_content)` (`:1474`, §1.7).
11. Return `PatchResult(success=True, diff=diff, files_modified=[path], lint=...to_dict(),
    lsp_diagnostics=write_result.lsp_diagnostics)` (`:1476-1488`).

`replace_all` semantics (`fuzzy_match.py:90-94`): if a strategy yields **>1 match and
`replace_all` is False** → fail immediately (no fall-through to looser strategies) with
`f"Found {len(matches)} matches for old_string. Provide more context to make it unique, or
use replace_all=True."`. With `replace_all=True`, all matches in the **first strategy that
matches** are replaced; `match_count` = number replaced.

### 1.5 PatchResult shape (`file_operations.py:195-224`, `to_dict` `:208-224`)
Always `{"success": bool}`. Conditionally adds (only when truthy): `diff` (unified diff
string), `files_modified` (list), `files_created` (list, V4A only), `files_deleted` (list,
V4A only), `lint` (dict, §1.7), `lsp_diagnostics` (string; hermes LSP layer — **omit for
LunaMoth**), `error` (string). The `patch_tool` wrapper additionally injects
`_warning`/`_hint`/`resolved_path`/`files_modified` (resolved abs) at the JSON layer (§1.2).

### 1.6 Fuzzy-match strategy chain — `fuzzy_find_and_replace` (`fuzzy_match.py:50-144`)

Preconditions (`:66-70`): empty `old_string` → `(content,0,None,"old_string cannot be
empty")`; `old_string == new_string` → `(content,0,None,"old_string and new_string are
identical")`.

The chain is **9 strategies tried in order** (`:73-83`; the module docstring's "8-strategy"
header at `:9` is stale — unicode_normalized was added, making 9). Each strategy returns a
list of `(start,end)` char-offset matches; the **first strategy returning a non-empty list
wins** and the rest are skipped:

1. **`exact`** (`_strategy_exact`, `:343-353`) — literal `str.find` for all non-overlapping
   occurrences (advances `start = pos+1`).
2. **`line_trimmed`** (`_strategy_line_trimmed`, `:356-373`) — strip leading/trailing
   whitespace from **each line** of pattern and content, match block-for-block, map back to
   original positions (`_find_normalized_matches`, `:669-701`).
3. **`whitespace_normalized`** (`_strategy_whitespace_normalized`, `:376-394`) — collapse
   runs of spaces/tabs to a single space (newlines preserved: `re.sub(r'[ \t]+',' ',s)`),
   exact-match on normalized copies, map back (`_map_normalized_positions`, `:704-777`).
4. **`indentation_flexible`** (`_strategy_indentation_flexible`, `:397-410`) — `lstrip` every
   line (ignore indentation entirely), block-match, map back.
5. **`escape_normalized`** (`_strategy_escape_normalized`, `:413-429`) — unescape `\n→NL`,
   `\t→tab`, `\r→CR` **in the pattern only**; if unchanged (no escapes present) **returns []
   (falls through)**; else exact-match the unescaped pattern against raw content.
6. **`trimmed_boundary`** (`_strategy_trimmed_boundary`, `:432-471`) — strip only the
   **first and last** lines of the pattern; slide a window of `len(pattern_lines)` over
   content lines, strip each window's first/last line, compare; record all matches.
7. **`unicode_normalized`** (`_strategy_unicode_normalized`, `:524-552`) — normalize smart
   quotes / em–en dashes / ellipsis / nbsp to ASCII (`UNICODE_MAP`, `:36-41`) in **both**
   content and pattern; if neither changed → `[]` (fall through); run `exact` then
   `line_trimmed` on normalized copies; map positions back via an expand-aware char map
   (`_build_orig_to_norm_map` `:474-492`, because some replacements expand 1→many chars).
8. **`block_anchor`** (`_strategy_block_anchor`, `:555-608`) — requires **≥2 pattern lines**
   (else `[]`). Anchor on first+last (unicode-normalized, stripped) lines; for each candidate
   window compute middle-section similarity via `difflib.SequenceMatcher.ratio()`. Threshold
   **0.50 if exactly one candidate, 0.70 if multiple**; pattern ≤2 lines → similarity forced
   `1.0`. Positions computed from **original** (un-normalized) lines to keep offsets correct.
9. **`context_aware`** (`_strategy_context_aware`, `:611-643`) — slide window of pattern
   length; per line-pair compute `SequenceMatcher(...).ratio()` on stripped lines, count
   lines with `ratio >= 0.80`; match the window when `count >= 0.5 * len(pattern_lines)`
   (50% of lines highly similar).

If **no strategy** matches: `(content,0,None,"Could not find a match for old_string in the
file")` (`:144`). Note `patch_replace` rewrites this to `"Could not find match for
old_string in {path}"` when `error` is falsy (`file_operations.py:1411`); both contain the
substring `"Could not find"` that `patch_tool` keys its hint on (§1.2 step 7).

**Post-match guards/transforms (applied after a strategy wins, `:90-141`):**
- **Ambiguity** (`:90-94`): >1 match & not `replace_all` → fail (§1.4).
- **Escape-drift guard** (only when strategy ≠ `exact`, `:106-109`; `_detect_escape_drift`
  `:147-184`): if `new_string` (and `old_string`) contain literal `\'` or `\"` that are
  **absent from the matched file region**, abort with the long "Escape-drift detected: …
  re-read the file … without backslash-escaping …" error (transport added spurious
  backslashes around a quote/apostrophe).
- **`\t`/`\r` conditional unescape** (`_maybe_unescape_new_string` `:271-304`): when strategy
  ≠ exact, convert `\t`→tab / `\r`→CR in `new_string` **only if** the matched file region
  actually contains that real control char. `\n` is intentionally **excluded**.
- **Re-indentation** (`_reindent_replacement` `:206-268`, applied inside `_apply_replacements`
  `:307-336` when `old_string` is passed, i.e. non-exact strategies): shift each non-blank
  `new_string` line so its indent base matches the file region's base indent (Roo Code-style:
  swap the LLM's base-indent prefix for the file's, preserving relative nesting). No-op when
  bases equal or text is empty.
- **Replacement application** (`_apply_replacements` `:307-336`): replace matches **end→start
  (descending offset)** so earlier offsets stay valid.

`format_no_match_hint` (`:842-860`) → `find_closest_lines` (`:780-839`): only fires when
`match_count==0` AND error startswith `"Could not find"` (so ambiguous/escape-drift/identical
errors get **no** "did you mean"); anchors on the first non-blank line of `old_string`, scores
content lines by `SequenceMatcher` ratio > 0.3, returns up to 3 numbered snippets with ±2
context lines joined by `\n---\n`.

### 1.7 Post-write syntax check — `_check_lint` / `_check_lint_delta` (`file_operations.py:1520-1691`)

**Which file types** (by extension): in-process linters `LINTERS_INPROC` (`:613-619`):
`.py`(ast.parse), `.json`(json.loads), `.yaml`/`.yml`(yaml.safe_load, skips if PyYAML
absent), `.toml`(tomllib/tomli, skips if absent). Shell linters `LINTERS` (`:446-452`):
`.js`(`node --check`), `.ts`(`npx tsc --noEmit`), `.go`(`go vet`), `.rs`(`rustfmt --check`),
`.py`(`python -m py_compile` — only used if the in-proc python check weren't preferred; it
always is). **In-process always preferred when ext matches** (`:1543-1555`).

**Skip cases** → `LintResult(skipped=True, message=...)` (rendered `{"status":"skipped",
"message":...}` by `to_dict` `:272-278`): missing dependency (`"__SKIP__"`); ext not in any
table (`"No linter for {ext} files"`); shell base command absent (`"{base_cmd} not
available"`); LSP-redundant ext (`.ts/.go/.rs`) when an LSP server claims the file
(hermes-only, `:1568-1572`, **drop for LunaMoth**); linter present but unusable (npx/rustfmt/go
tooling gap → `_looks_like_linter_unusable` `:526-539`, message `"{base_cmd} not usable:
…"`).

**Result** (`LintResult.to_dict` `:272-278`): skipped → `{"status":"skipped","message":...}`;
else `{"status":"ok"|"error","output":...}` plus optional `"message"`.

**New-vs-existing error distinction** (`_check_lint_delta` `:1608-1691`) — the key behavior:
1. Lint post-content. If `success` or `skipped` → return as-is (hot path, `:1646-1650`).
2. If `pre_content is None` (new file) → return full post errors (all are new, `:1654-1655`).
3. Lint pre-content. If pre was clean/skipped/empty → all post errors are new, return full
   post (`:1657-1661`).
4. Both broke: set-difference on stripped non-empty lines. `post_lines` = post error lines
   **not** present in pre (`:1672-1673`).
   - If `post_lines` empty (every post error pre-existed) → `LintResult(success=False,
     output=post.output, message="Pre-existing lint errors — this edit didn't introduce new
     ones but the file is still broken.")` (`:1675-1683`).
   - Else → `LintResult(success=False, output="New lint errors introduced by this edit
     (pre-existing errors filtered out):\n" + "\n".join(post_lines))` (`:1685-1691`).

(`lsp_diagnostics` `_maybe_lsp_diagnostics`/`_snapshot_lsp_baseline`/`_lsp_*` `:1693-1858`
are the hermes LSP semantic tier — **entirely omit for LunaMoth**; keep only the syntax/lint
delta above.)

### 1.8 Unified diff (`_unified_diff` `file_operations.py:931-940`)
`difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
fromfile=f"a/{filename}", tofile=f"b/{filename}")`, joined with `''`. Standard `--- a/… /
+++ b/… / @@ …` unified format. Returned in `PatchResult.diff`.

### 1.9 `mode=patch` — V4A format & application

`patch_v4a(patch_content)` (`file_operations.py:1490-1518`):
`parse_v4a_patch` → on error `PatchResult(error=f"Failed to parse patch: {parse_error}")`;
else `apply_v4a_operations(operations, self)`.

#### 1.9.1 Grammar & parsing (`patch_parser.py:69-224`)
Envelope/headers:
```
*** Begin Patch                         (or ***Begin Patch — space optional, :89)
*** Update File: <path>                 (re ^\*\*\*\s*Update\s+File:\s*(.+), :111)
@@ <optional context hint> @@           (hunk marker, :170; hint via @@\s*(.+?)\s*@@, :177)
 <context line>        (leading space → HunkLine(' ', rest), :190-191)
-<removed line>        (leading '-'   → HunkLine('-', rest), :188-189)
+<added line>          (leading '+'   → HunkLine('+', rest), :186-187)
*** Add File: <path>                    (re Add\s+File, :112; subsequent + lines = content)
*** Delete File: <path>                 (re Delete\s+File, :113)
*** Move File: <src> -> <dst>           (re Move\s+File:\s*(.+?)\s*->\s*(.+), :114)
*** End Patch                           (or ***End Patch, :91)
```
Parser details:
- Scans for begin/end markers (`:88-93`). **Missing `*** Begin Patch` → `start_idx=-1`**
  (parse from top, `:95-97`). **Missing `*** End Patch` → `end_idx=len(lines)`** (`:99-100`).
- Iterates `start_idx+1 .. end_idx`, tracking `current_op`/`current_hunk`. A new file header
  flushes the previous op (appending a non-empty current hunk first, `:117-168`).
- **Update**: starts an op, `current_hunk=None` (hunks open lazily, `:116-127`).
- **Add**: starts op with `current_hunk=Hunk()`; later `+` lines accrue (`:129-139`).
- **Delete** / **Move**: appended immediately, `current_op=None` (`:141-168`).
- `@@` line: flush current hunk, open a new `Hunk(context_hint=…)` (`:170-179`).
- Other non-empty lines under an op (`:181-197`): `+`/`-`/` ` prefixes → HunkLine; `\` lines
  ("\ No newline at end of file") **skipped**; **any other line → implicit context line**
  `HunkLine(' ', line)` (`:195-197`).
- Flush trailing op at end (`:202-205`).
- **Empty patch is NOT an error** → `([], None)` (`:208-210`).
- **Validation** (`:212-224`): collect errors — empty file path; UPDATE with no hunks
  (`"UPDATE {path!r}: no hunks found"`); MOVE with no `new_path`
  (`"MOVE {path!r}: missing destination path (expected 'src -> dst')"`). Any →
  `([], "Parse error: " + "; ".join(errors))`.

#### 1.9.2 Two-phase apply (`apply_v4a_operations` `:331-452`)
**Phase 1 — validate (no writes)** `_validate_operations` (`:240-328`): for each op,
simulate against current content:
- **UPDATE**: `read_file_raw` (full content, no line numbers); per hunk build search pattern
  from `' '`+`'-'` lines, replacement from `' '`+`'+'` lines; run `fuzzy_find_and_replace`
  (replace_all=False) and **advance the simulated content** so later hunks validate against
  earlier hunks' results (`:264-306`). `count==0` → error `"{path}: hunk '{hint}'|(no hint)
  not found — {match_error}"` + did-you-mean hint. **Addition-only hunk** (no ` `/`-` lines):
  validate context-hint uniqueness — not found → `"…context hint '…' not found"`; >1 →
  `"…context hint '…' is ambiguous (N occurrences)"` (`:267-282`).
- **DELETE**: `read_file_raw`; error → `"{path}: {error}"` (file-not-found).
- **MOVE**: missing dst → error; source not found → `"{path}: source file not found for
  move"`; **dst already exists → `"{dst}: destination already exists — move would
  overwrite"`** (`:313-324`).
- **ADD**: no pre-check (parent dirs created by write_file).
- **Any validation errors → return immediately, NO filesystem changes**:
  `PatchResult(success=False, error="Patch validation failed (no files were modified):\n" +
  "\n".join("  • "+e))` (`:353-359`).

**Phase 2 — apply** (`:361-452`): per op dispatch `_apply_add` / `_apply_delete` /
`_apply_move` / `_apply_update`; accumulate `files_created/deleted/modified`, per-op diffs,
LSP blocks (omit), errors. Then run `_check_lint` on each modified+created file, store in
`lint_results[path]` (`:415-420`). `combined_diff = "\n".join(all_diffs)`. If any phase-2
errors → `PatchResult(success=False, …, error="Apply phase failed (state may be
inconsistent — run \`git diff\` to assess):\n" + "  • "…)` (`:431-442`). Else
`PatchResult(success=True, diff, files_modified, files_created, files_deleted, lint=...)`
(`:444-452`).

Per-op apply:
- **`_apply_add`** (`:455-481`): join all `+`-line contents, `write_file`; diff
  `"--- /dev/null\n+++ b/{path}\n" + "\n".join("+"+l ...)`.
- **`_apply_delete`** (`:483-501`): `read_file_raw` then `delete_file`; diff via
  `unified_diff(removed_lines, [], a/path, /dev/null)` or `"# Deleted: {path}"`.
- **`_apply_move`** (`:504-511`): `move_file`; diff `"# Moved: {src} -> {dst}"`.
- **`_apply_update`** (`:514-621`): `read_file_raw`; per hunk build search/replacement from
  prefixes; `fuzzy_find_and_replace` over the **running** `new_content`. On no-match **and**
  a context hint, retry inside a window `[hint-500, hint+2000]` around `new_content.find(hint)`
  and splice the result back (`:557-573`). Still failing → `"Could not apply hunk: {error}"` +
  did-you-mean. **Addition-only hunk** (`:583-606`): insert `+` lines — at end if no hint;
  if hint, 0 occ → append at EOF (safe fallback), >1 occ → fail `"…context hint '…' is
  ambiguous (N occurrences) — provide a more unique hint"`, exactly 1 → insert **after the
  line containing the hint**. Then `write_file`; diff via `unified_diff(current, new,
  a/path, b/path)`.

> **Cross-file note:** a single V4A patch may carry any mix of Update/Add/Delete/Move across
> multiple files; `patch_tool` locks every target path (sorted) and reports all resolved
> abs paths (§0.4, §1.2).

### 1.10 `patch` error-string catalog (verbatim — preserve exactly)
- `"path required"` · `"old_string and new_string required"` · `"patch content required"` ·
  `f"Unknown mode: {mode}"` (`file_tools.py:1262-1277`).
- `f"V4A patch header contains '..' traversal: {v4a_path!r}. …"` (`:1201-1205`).
- `"old_string cannot be empty"` · `"old_string and new_string are identical"`
  (`fuzzy_match.py:67,70`).
- `f"Found {N} matches for old_string. Provide more context to make it unique, or use
  replace_all=True."` (`fuzzy_match.py:91-94`).
- `"Could not find a match for old_string in the file"` (`fuzzy_match.py:144`) /
  `f"Could not find match for old_string in {path}"` (`file_operations.py:1411`).
- `f"Write denied: '{path}' is a protected system/credential file."` (`:1386`).
- `f"Failed to read file: {path}"` (`:1393`) · `f"Failed to write changes: {…}"` (`:1434`).
- `f"Post-write verification failed: could not re-read {path}"` (`:1444`) ·
  the long `"Post-write verification failed for {path}: …"` (`:1460-1466`).
- Escape-drift: `"Escape-drift detected: …"` (`fuzzy_match.py:175-183`).
- V4A: `"Failed to parse patch: {…}"`, `"Parse error: …"`, `"Patch validation failed (no
  files were modified):\n  • …"`, `"Apply phase failed (state may be inconsistent — run
  \`git diff\` to assess):\n  • …"`, plus per-op `"{path}: hunk … not found"`, `"…context
  hint '…' is ambiguous (N occurrences)"`, `"…destination already exists — move would
  overwrite"`.
- `_hint` strings (non-fatal, attached to result): generic `"old_string not found. Use
  read_file …"` and escalating `"This is failure #N patching {path!r}. …"`.

---

## 2. `search_files` — unified grep + glob/find

Entrypoints: schema `SEARCH_FILES_SCHEMA` (`file_tools.py:1511-1528`); handler
`_handle_search_files` (`:1572-1580`); core `search_tool` (`:1345-1412`); impl
`ShellFileOperations.search` (`file_operations.py:1864-1925`) → `_search_files`/`_search_content`.
Registry: `name="search_files"`, emoji `🔎`, `max_result_size_chars=100_000` (`:1586`).

### 2.1 JSON schema (every param)
`required: ["pattern"]` (`file_tools.py:1526`). Params (`:1516-1525`):

| param | type | enum | default | meaning |
|---|---|---|---|---|
| `pattern` | string | — | (required) | regex (content) or glob like `*.py` (files) |
| `target` | string | `["content","files"]` | `"content"` | search inside contents vs by filename |
| `path` | string | — | `"."` | dir or file to search |
| `file_glob` | string | — | none | filter files in content mode (e.g. `*.py`) |
| `limit` | integer | — | `50` | max results returned |
| `offset` | integer | — | `0` | skip first N (pagination) |
| `output_mode` | string | `["content","files_only","count"]` | `"content"` | content-mode output shape |
| `context` | integer | — | `0` | lines of context before+after each match (content mode) |

### 2.2 Legacy aliases (`_handle_search_files` `file_tools.py:1572-1580`)
`target` is mapped through `{"grep":"content", "find":"files"}` (`:1574-1576`) before
dispatch — so a caller may pass `target="grep"` or `target="files"`/`"find"` and it
normalizes. Any other value passes through unchanged. **This is the only alias handling;
there are no separate `grep`/`find` tool names — `search_files` is the single unified tool.**

### 2.3 `search_tool` control flow (`file_tools.py:1345-1412`)
1. `normalize_search_pagination(offset, limit)` → `offset>=0`, `limit>=1`
   (`file_operations.py:658-663`; non-int coerced to defaults 0/50).
2. **Consecutive-search loop guard** (`:1356-1402`): key =
   `("search", pattern, target, str(path), file_glob or "", limit, offset)` (pagination
   included so paging doesn't trip it). `count>=4` → **hard block** JSON
   `{"error":"BLOCKED: You have run this exact search N times in a row. The results have NOT
   changed. …","pattern":…,"already_searched":N}` (`:1376-1385`). `count>=3` → attach
   `_warning` (`:1398-1402`).
3. Dispatch `file_ops.search(pattern, path, target, file_glob, limit, offset, output_mode,
   context)` (`:1387-1391`).
4. **Redact secrets** in each match's `content` via `redact_sensitive_text(..., code_file=True)`
   (`:1392-1395`). **(LunaMoth: keep if you have a redactor; otherwise drop.)**
5. `result_dict = result.to_dict()`; if `count>=3` attach `_warning`.
6. `result_json = json.dumps(result_dict, ensure_ascii=False)`; **truncation hint**: if
   `result_dict.get("truncated")`, append plain-text
   `"\n\n[Hint: Results truncated. Use offset={offset+limit} to see more, or narrow with a
   more specific pattern or file_glob.]"` (`:1404-1409`).
7. `except Exception: return tool_error(str(e))` (`:1411-1412`).

### 2.4 `search` dispatch & path-not-found (`file_operations.py:1864-1925`)
`_expand_path(path)`; verify existence with `test -e … && echo exists || echo not_found`.
**Not found** (`:1890-1919`): build a hint — `f"Path not found: {path}"`; if the parent dir
exists, `ls -1 parent | head -20` and collect entries where the query is a case-insensitive
substring either way or shares the first 3 chars; append `"Similar paths: a, b, …"` (≤5).
Return `SearchResult(error=". ".join(hint_parts), total_count=0)`. Then branch:
`target=="files"` → `_search_files`; else → `_search_content` (`:1921-1925`).

### 2.5 File search — `target=files` (`_search_files` `:1927-2050`)
Pattern handling (`:1929-1933`): bare name (no `/`) used as-is; otherwise only the last path
segment is used as the name pattern.

**Preferred: ripgrep `--files`** (`_search_files_rg` `:2009-2050`) when `rg` available
(respects `.gitignore`, excludes hidden dirs, parallel). Glob: bare non-`*` pattern wrapped to
`*{pattern}` (match at any depth); else used literally (`:2017-2022`). Command:
```
rg --files --sortr=modified -g '<glob>' '<path>' 2>/dev/null | head -n {limit+offset}
```
(`:2026-2030`). **Sort = modification time, most-recent first** (`--sortr=modified`). If
empty (older rg lacking `--sortr`), retry without it (`rg --files -g … | head -n …`,
`:2034-2042`). Page = `all_files[offset:offset+limit]`; `truncated = len(all_files) >=
limit+offset` (`:2044-2050`).

**Fallback: `find`** (`:1947-2007`) when neither rg available → no rg/find →
`SearchResult(error="File search requires 'rg' (ripgrep) or 'find'. Install ripgrep …
https://github.com/BurntSushi/ripgrep#installation")`. Else:
```
find '<path>' [-not -path '*/.*'] -type f -name '<pat>' -printf '%T@ %p\n' 2>/dev/null
  | sort -rn [| tail -n +{offset+1} | head -n {limit}]
```
(`:1966-1967`) — sorted by mtime desc via `%T@`. Hidden dirs excluded unless the search root
itself is under a hidden path (`has_hidden_path_ancestor`, `:1936-1939`,`:1955-1957`). If
`-printf` yields nothing (BSD/macOS find lacks it) retry without it (`:1971-1975`). Lines
parsed: split once on space, keep path part if the first token is numeric mtime, else keep
whole line (`:1977-1985`). For explicit hidden roots, descendant hidden filtering + slicing
done in Python (`:1990-2001`). Returns `SearchResult(files=…, total_count=len(files))`.

### 2.6 Content search — `target=content` (`_search_content` `:2052-2067`)
`rg` preferred (`_search_with_rg`), else `grep` (`_search_with_grep`), else
`SearchResult(error="Content search requires ripgrep (rg) or grep. Install ripgrep: …")`.

**ripgrep** (`_search_with_rg` `:2069-2181`): base flags
`rg --line-number --no-heading --with-filename` (`:2072`); `-C {context}` if `context>0`
(`:2075-2076`); `--glob '<file_glob>'` if set (`:2079-2080`);
**output_mode**: `files_only`→`-l`, `count`→`-c` (`:2083-2086`); then escaped `pattern`,
escaped `path`. Pipe `| head -n {fetch_limit}` where `fetch_limit = limit+offset+200` if
context else `limit+offset` (`:2092-2096`). Whole command prefixed `set -o pipefail;` so
rg's exit code survives the `head` pipe (`:2098-2103`).

**grep fallback** (`_search_with_grep` `:2183-2292`): `grep -rnH` + `--exclude-dir='.*'`
(hide dotdirs) + `-C` + `--include '<file_glob>'` + `-l`/`-c`; same `head` + `pipefail`.

**Exit handling** (`:2106-2118`, `:2222-2234`): `_exec` merges stderr into stdout, so
`_split_tool_diagnostics` (`:288-334`) separates tool diagnostics (lines starting `rg: `/
`grep: `, regex-parse-error blocks) from real payload by **output shape** (`_SEARCH_OUTPUT_RE`
`:342`). **Error only when `exit_code==2` AND no usable payload** → `SearchResult(error=f"Search
failed: {diagnostics or stdout or 'Search error'}", total_count=0)`. exit 1 (no matches) and
partial-error-with-matches are NOT errors.

**Output parsing by mode** (rg `:2123-2181`, grep mirrors `:2237-2291`):
- `files_only`: non-empty lines → `files`; `total_count=len(all)`, page=`[offset:offset+limit]`.
- `count`: split each line on the **last** `:`; `counts[path]=int(n)`; `total_count=sum(counts.values())`.
- `content` (default): parse match lines via `^([A-Za-z]:)?(.*?):(\d+):(.*)$` (handles Windows
  drive letters) → `SearchMatch(path, line_number, content[:500])`; **content truncated to 500
  chars**. When `context>0`, also parse dash-form context lines
  (`_parse_search_context_line` `:345-367`, rightmost `-(\d+)-` separator). `total_count=len(matches)`,
  page=`[offset:offset+limit]`, `truncated = total > offset+limit`.

### 2.7 SearchResult shape (`file_operations.py:236-261`, `to_dict` `:246-261`)
Always `{"total_count": int}`. Conditionally adds (when non-empty/true):
- `matches`: list of `{"path":…, "line":…, "content":…}` (note JSON key is **`line`**, from
  `SearchMatch.line_number`; `content` ≤500 chars).
- `files`: list of paths (mtime-sorted, most-recent first).
- `counts`: `{path: int}`.
- `truncated`: `true`.
- `error`: string.

`mtime` field on `SearchMatch` exists for sorting but is **not** serialized.

### 2.8 `search_files` error-string catalog (verbatim)
- Loop block: `"BLOCKED: You have run this exact search N times in a row. …"` (`file_tools.py:1379-1381`).
- `f"Path not found: {path}"` (+ optional `"Similar paths: …"`) (`file_operations.py:1894-1917`).
- `"File search requires 'rg' (ripgrep) or 'find'. Install ripgrep …"` (`:1949-1953`).
- `"Content search requires ripgrep (rg) or grep. Install ripgrep: …"` (`:2064-2067`).
- `f"Search failed: {error_msg}"` (`:2118`, `:2234`).
- Truncation hint (non-error): `"[Hint: Results truncated. Use offset=N to see more, …]"`
  (`file_tools.py:1409`).

---

## 3. Pagination normalizers (shared)
- `normalize_search_pagination` (`file_operations.py:658-663`): `offset=max(0,coerce_int(off,0))`,
  `limit=max(1,coerce_int(lim,50))`. Non-int → defaults (`_coerce_int` `:631-636`).
- (read path, FYI) `normalize_read_pagination` (`:639-655`): `offset>=1`,
  `1<=limit<=max_lines` (config `tool_output.max_lines`, default `MAX_LINES=2000`).

## 4. Re-implementation watch-list (what breaks a port if changed)
1. **First-match-wins strategy ordering** + the precise thresholds (block_anchor 0.50/0.70,
   context_aware 0.80 line / 0.50 block) and the **end→start** replacement order.
2. **Ambiguity halts the chain** (>1 match & not replace_all fails *now*, no fall-through).
3. **Escape-drift / `\t`\`\r` conditional-unescape / re-indent** post-match transforms — all
   region-gated against the matched file text; `\n` always excluded.
4. **V4A two-phase validate-then-apply** (no partial writes on validation failure) and the
   running-content simulation so multi-hunk patches validate in order.
5. **V4A header `..` traversal rejected; explicit `path=` arg exempt.**
6. **Lint delta**: only NEW syntax errors surface; pre-existing filtered; both-broke and
   new-error messages are distinct strings.
7. **Search: error only on exit==2 with empty payload**; diagnostics split by output shape;
   content truncated to 500 chars; files sorted by mtime desc; JSON match key is `line`.
8. **Loop guards** (read/search count≥3 warn, ≥4 block; patch failure ≥3 escalating hint) and
   the **registry 100K result cap** + report-resolved-absolute-path behavior.
