# LunaMoth tool-integration seam — what the hermes transplant replaces / keeps

Authoritative map of LunaMoth's CURRENT tool layer, so the hermes registry+tools
graft in cleanly. Owner directive: tools must be hermes-IDENTICAL; the LunaMoth
integration may be rewritten freely. Keep only: the audit trail, the loop
guardrails, and the chara-life tools speak/rest/add_goal/set_goal_status.

## The execution flow today

1. `core/agent.py:_reply_stream` (607) calls
   `llm.stream_agent(user_text, context, stable, volatile, self.tools.schemas(),
   self._execute_tool, max_steps=settings.max_tool_steps(80), ...)`.
2. `core/llm.py:stream_agent` (796) owns the tool-CALLING loop and is ALREADY
   hermes-hardened (arg-repair 4-pass :147, replayed-history repair :249,
   empty-completion retry :846, stall guard, truncation/continuation). **Keep
   stream_agent as-is** — the transplant does NOT touch the LLM loop.
3. Per tool_call, llm calls back into `agent.py:_execute_tool` (615) →
   `self.tools.call(name, **args)` → returns `{display, content, ok, say?}`.
   - **speak** is special-cased here (643): ok speak → `out["say"]=text`,
     `display=""` (the words become a say-channel event). PRESERVE.
   - **6000-char cap** (633): `content = text[:6000]` + explicit truncation
     note. THIS MUST CHANGE — hermes tools self-cap at 100K and read_file
     paginates; a blanket 6000 cap defeats read/search. Lift to ~100K and let
     tools own their own caps (hermes shape).

## ToolGateway (tools/gateway.py) — what to keep vs replace

KEEP (the wrapper around dispatch):
- `call(name, /, **kwargs)` (75): guard signature → refusal → dispatch →
  record. The loop guardrails (#24, GUARD_* at 30-32, `_guard_*` 160-208) and
  `reset_guardrails()` (called from agent.py:681 on each fresh user turn).
- The audit writes on every path (tool_call/denied/unknown/badargs/crash).
- The exception boundary (118-126): typed errors → message; final
  `except Exception` → `{"ok":False,"error":...}` + `tool_crash` audit. Never
  let a tool kill the turn. (Hermes' tool_result_classification is the same
  idea — reconcile, don't duplicate.)
- `schemas()` (552) → `[{"type":"function","function":{name,description,
  parameters}}]` and `schemas_names()` (568). Public API the agent/llm use —
  keep the SIGNATURE, source the data from the registry.
- `_effective()` (64) gating: `implemented ∩ state.tool_access ∩ pack.tools`.
  Keep the three-way gate; the "implemented" set now comes from the registry.

REPLACE:
- All `tool_*` methods (212-356) and `_all_schemas()` (360-550) → a hermes-style
  **ToolRegistry** of ported tool modules (self-registering at import). Each
  handler takes a `ToolContext` (the runtime touchpoints below) + validated args.
- `_dispatch` (90) becomes "look up handler in registry, call with context".

## Runtime touchpoints a handler needs (the ToolContext)

hermes handlers reach infra; LunaMoth must supply the same shape:
- **filesystem root** → the chara's `sandbox.root/workspace` (Sandbox,
  tools/sandbox.py); path confinement lives here. hermes session-cwd → this.
- **shell backend** → `tools/runner.py:run_terminal` under `session/isolation.py`
  (dir/sandbox/docker). hermes VM/SSH/modal backends → this single isolation.
- **llm client** → `core/llm.py` LLMClient (for web summarize / execute_code /
  delegate_task). hermes provider chain → the one OpenAI-compatible client.
- **state** → `core/state.py` EnvState (network_access, writable_paths,
  isolation, user_present, rest_until).
- **transcript** → `core/transcript.py` SQLite (for session_search).
- **audit** → obs/audit.py. **mcp** → tools/mcp.py McpManager (already
  registry-like; keep `mcp__server__tool` dispatch at gateway.call 84).

## Allowlist + pack seeding to widen to the hermes set

- `core/state.py` DEFAULT_STATUS["tool_access"] (15-20): the per-session
  allowlist — extend to the full ported set; add a migration so OLD state files
  gain the new tools (the file already migrates run_python→terminal etc. at
  52-66 — same mechanism).
- `toolpacks/sandbox.json` "tools" (the default pack): replace the 14-name list
  with the hermes set (read_file/write_file/patch/search_files/terminal/process/
  web_search/web_extract/memory/todo/session_search/skills_list/skill_view/
  skill_manage/execute_code/delegate_task/clarify/browser_*/inspect_env +
  speak/rest/add_goal/set_goal_status). `mcp_servers:["*"]` stays.

## Naming reconciliation (LunaMoth current → hermes target)

- `read_file` (whole-file, 6000 cap) → hermes `read_file` (offset/limit, 2000-line).
- `write_file` (overwrite) → hermes `write_file` (overwrite + syntax-diff). same name.
- (none) → `patch` (replace + V4A). NEW.
- `list_files` (no-arg recursive) → REMOVE; folded into `search_files target=files`.
- (none) → `search_files`. NEW.
- `terminal` → hermes `terminal` (+ background, +pty, +timeout clamp 600s).
- (none) → `process`. NEW.
- `memory` → hermes `memory` (reconcile actions/storage — see spec-memory-skills).
- `read_skill`/`create_skill` → hermes `skills_list`/`skill_view`/`skill_manage`.
- `write_log` → keep (audit note) or fold; decide in blueprint.
- `inspect_env` → keep (LunaMoth-useful; hermes has no exact twin).
- `request_permission` → keep (LunaMoth presence-gated; chara-life-adjacent).
- `add_goal`/`set_goal_status` → **RENAME to `add_wish`/`set_wish_status`**
  (chara-life, kept). `speak`/`rest` → KEEP.
- NEW general: `web_search`,`web_extract`,`todo`,`session_search`,
  `execute_code`,`delegate_task`,`clarify`, `browser_*`.

## DECISION (owner, this session): goal → wish (愿望), distinct from todo

The chara-life "goal" concept is renamed **wish / 愿望** to AVOID collision with
the generic agent task-list, and because the meanings genuinely differ:
- **`todo`** (ported from hermes) = a task list for GETTING WORK DONE — forced,
  checkable, completion-oriented. The model's scratchpad for a job.
- **`wish` / 愿望** (chara-life, ours) = what the character LIVES FOR — its own
  aspirations, not tasks anyone forces it to finish. Part of the innovation core.
They coexist as two separate tools with separate stores; NEVER conflate them.

Rename surface (fold into the migration): `tools/goals.py` GoalStore→WishStore;
tools `add_goal`→`add_wish`, `set_goal_status`→`set_wish_status`; the volatile
"goals block" in agent.py → "wishes block"; card hook
`extensions.lunamoth.goals`→`wishes` (migrate the old key on load); seed-goals →
seed-wishes; `/goal` command + aliases → `/wish` (keep `/goal` as a legacy
alias); state tool_access + toolpack names; web UI + i18n (目标→愿望);
transcript/audit labels; CLAUDE.md. English term = **wish** (concept:
aspiration). Keep a one-load migration for any on-disk goals.json → wishes.

## Tests that will move
`tests/` has ~41 files; tool tests assume the gateway `tool_*`/`_all_schemas`
shape and the 6000-cap. They get rewritten against the registry + new tool
behaviors. `tests/test_architecture.py` boundary (front!→backend, obs→config)
must stay green — the registry lives under tools/, imports allowed there.
