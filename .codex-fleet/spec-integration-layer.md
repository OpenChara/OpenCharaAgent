# Spec: Transplant hermes-agent's tool integration LAYER into LunaMoth

Goal: replace LunaMoth's `ToolGateway` with hermes's **registry + executor**, keeping
LunaMoth's identity-bearing pieces (the audit trail, the #24 loop guardrails, the
chara-life tools `speak`/`rest`/`add_goal`/`set_goal_status`/`request_permission`,
and the three-zone prompt's per-turn schema emission).

All cites are file:line at the snapshot read for this spec.
Hermes root: `/Users/.../reference/hermes-agent`. LunaMoth root: `src/lunamoth`.

This spec describes the LAYER (registry/toolset/executor/handler-contract), not the
individual tool implementations.

---

## 1. `tools/registry.py` — the `ToolRegistry` singleton

Hermes cite: `tools/registry.py:1-589`.

### 1.1 The module-level singleton + self-registration-at-import
- `registry = ToolRegistry()` is a process-global singleton (`registry.py:544`).
- Every tool module calls `registry.register(...)` at **module top level**
  (e.g. `tools/terminal_tool.py:2670`, `tools/file_tools.py:1583-1586`). Importing
  the module is what registers the tool. There is no decorator; it's a bare call.
- `discover_builtin_tools(tools_dir)` (`registry.py:57-74`) finds the modules to
  import: it `glob("*.py")` over the tools dir, **AST-parses each file**
  (`_module_registers_tools`, `registry.py:42-54`) and keeps only modules with a
  *top-level* `registry.register(...)` Expr (`_is_registry_register_call`,
  `registry.py:29-39` — it matches `registry.register` by attribute/name, so a
  module that only calls register inside a function is skipped). It then
  `importlib.import_module()`s each, catching/logging import failures
  (`registry.py:67-74`). `registry.py` and `mcp_tool.py` are excluded from the glob
  (`registry.py:63`) — registry is the dependency root, MCP registers dynamically.
- Import chain (registry docstring `registry.py:7-15`): `registry.py` (imports
  nothing from tools/model_tools) ← `tools/*.py` (import `from tools.registry import
  registry`) ← `model_tools.py` (imports registry + calls `discover_builtin_tools`)
  ← entrypoints. This is **circular-import safe** because registry has zero tool deps.

### 1.2 `ToolEntry` — every field
`ToolEntry.__slots__` (`registry.py:80-83`), set by `register()` (`registry.py:290-302`):
| field | meaning |
|---|---|
| `name` | tool name as the model sees it |
| `toolset` | grouping key (used for enable/disable scoping) |
| `schema` | OpenAI *function* schema dict (`{"description","parameters",...}`; `name` injected at emit time) |
| `handler` | the callable: `handler(args: dict, **kwargs) -> str` (see §4) |
| `check_fn` | zero-arg `() -> bool` capability gate; `None` = always available |
| `requires_env` | list of env-var names this tool needs (for UI/diagnostics) |
| `is_async` | if True, dispatch bridges via `model_tools._run_async` (`registry.py:401-403`) |
| `description` | falls back to `schema["description"]` (`registry.py:298`) |
| `emoji` | display emoji |
| `max_result_size_chars` | per-tool result-size cap (`registry.py:422-430`) |
| `dynamic_schema_overrides` | zero-arg `() -> dict`, merged onto schema at emit time (`registry.py:99-106`, applied `registry.py:372-382`) for runtime-dependent fields |

### 1.3 `register()` signature + shadowing policy
`register(name, toolset, schema, handler, check_fn=None, requires_env=None,
is_async=False, description="", emoji="", max_result_size_chars=None,
dynamic_schema_overrides=None, override=False)` (`registry.py:234-248`).
- Under `self._lock` (RLock, `registry.py:161`). Bumps `self._generation`
  (`registry.py:305`) — a monotonic counter external callers memoize against.
- Shadowing guard (`registry.py:257-289`): a register for an existing name from a
  *different* toolset is REJECTED unless (a) both toolsets are `mcp-*`
  (server refresh), or (b) `override=True` (plugin opt-in, logged at INFO).
- First tool of a toolset with a `check_fn` seeds `self._toolset_checks[toolset]`
  (`registry.py:303-304`).
- `deregister(name)` (`registry.py:307-331`): pops the entry; if it was the last in
  its toolset, drops the toolset check + aliases. Used by MCP nuke-and-repave.

### 1.4 `get_definitions()` — OpenAI schema emission + 30s check_fn TTL
`get_definitions(tool_names: Set[str], quiet=False) -> List[dict]` (`registry.py:337-384`):
- For each requested name (sorted), look up the entry; skip unknown.
- If it has a `check_fn`, evaluate via `_check_fn_cached` (`registry.py:126-141`):
  result cached **30 s** (`_CHECK_FN_TTL_SECONDS = 30.0`, `registry.py:121`), under a
  lock, exceptions → False. There's also a per-call dedupe dict (`check_results`,
  `registry.py:352-360`) so one definitions pass probes each fn once. Failing
  check_fn → tool omitted from the emitted list.
- Emits `{"type": "function", "function": {**entry.schema, "name": entry.name}}`
  (`registry.py:366,383`). `dynamic_schema_overrides()` shallow-merged first
  (`registry.py:372-382`).
- `invalidate_check_fn_cache()` (`registry.py:144-148`) flushes after config changes.

### 1.5 `dispatch()` — handler lookup + invoke + error normalization
`dispatch(name, args: dict, **kwargs) -> str` (`registry.py:390-416`):
- Unknown name → `{"error": "Unknown tool: ..."}` JSON string.
- Async entry → bridged through `model_tools._run_async(handler(args, **kwargs))`.
- Sync → `handler(args, **kwargs)`.
- ANY exception is caught, logged, sanitized via `model_tools._sanitize_tool_error`,
  and returned as `{"error": "Tool execution failed: ..."}` JSON string. **The
  registry never raises out of dispatch** — a tool result is always a string.

### 1.6 Toolset query helpers (used by emission/UI)
`get_tool_names_for_toolset`, `get_registered_toolset_names`,
`is_toolset_available`, `check_toolset_requirements`, `get_available_toolsets`,
`get_tool_to_toolset_map`, `get_max_result_size`, `get_emoji`, `get_schema`
(`registry.py:197-540`). Toolset aliases: `register_toolset_alias` (`registry.py:208-218`).

### 1.7 MCP dynamic registration
MCP tools register the SAME way, just at runtime not import time
(`tools/mcp_tool.py:3355-3461`): per discovered tool, `registry.register(name=prefixed,
toolset="mcp-<server>", schema=..., handler=_make_tool_handler(...), check_fn=
_make_check_fn(server), is_async=False)` (`mcp_tool.py:3410-3418`), with a collision
guard against built-in toolsets (`mcp_tool.py:3400-3408`). `register_toolset_alias`
maps server→toolset (`mcp_tool.py:3460`). `notifications/tools/list_changed` triggers
`registry.deregister(...)` then re-register (`mcp_tool.py:1233-1262, 1978`).

### 1.8 Result helpers
`tool_error(msg, **extra)` / `tool_result(data=None, **kwargs)` (`registry.py:563-589`)
— JSON-string builders handlers use instead of inline `json.dumps`.

---

## 2. `toolsets.py` — toolset groupings + session selection

Hermes cite: `toolsets.py:1-882`.

- `_HERMES_CORE_TOOLS` (`toolsets.py:31-73`): the master tool-name list shared by the
  CLI and all messaging platforms. `_HERMES_WEBHOOK_SAFE_TOOLS` (`toolsets.py:78-83`)
  is a constrained set for untrusted webhook origins.
- `TOOLSETS` dict (`toolsets.py:88+`): `{name: {"description","tools":[...],
  "includes":[other toolset names]}}` — toolsets compose other toolsets via `includes`.
- Resolution: `get_toolset(name)` (`toolsets.py:555`), `resolve_toolset(name, visited)`
  (`toolsets.py:606`, recursive include-expansion with cycle guard via `visited`),
  `resolve_multiple_toolsets(names)` (`toolsets.py:680`), `get_all_toolsets`/
  `get_toolset_names`/`get_toolset_info` (`toolsets.py:725-817`).
- **Session/profile selection**: the agent is constructed with
  `enabled_toolsets` / `disabled_toolsets` (run_agent `run_agent.py:356,429`;
  CLI flag parsing `run_agent.py:5255-5278`). `model_tools.get_tool_definitions(
  enabled_toolsets=, disabled_toolsets=, ...)` resolves those names → tool-name set →
  `registry.get_definitions(set)`. The agent caches the resolved names as
  `agent.valid_tool_names` and passes `enabled_toolsets`/`disabled_toolsets` down to
  `handle_function_call` (executor `tool_executor.py:1187-1192,1229-1234`) so Tool
  Search scope and dispatch agree.

---

## 3. The executor — model `tool_calls` → messages

### 3.1 Dispatch decision (parallel vs sequential)
`AIAgent._execute_tool_calls` (`run_agent.py:4990-5012`): if
`_should_parallelize_tool_batch(tool_calls)` (`tool_dispatch_helpers.py:103-146`) →
`execute_tool_calls_concurrent`, else `execute_tool_calls_sequential`.
Both live in `agent/tool_executor.py` as module functions taking the agent first.

**Parallel-safety gate** (`tool_dispatch_helpers.py:39-146`):
- `len<=1` → sequential.
- Any tool in `_NEVER_PARALLEL_TOOLS = {"clarify"}` → sequential (`:41,109`).
- Read-only tools in `_PARALLEL_SAFE_TOOLS` (`:44-56`) always parallel-OK.
- Path-scoped tools `_PATH_SCOPED_TOOLS = {read_file, write_file, patch}` (`:59`):
  parallel-OK only if their normalized target paths **don't overlap**
  (`_extract_parallel_scope_path` `:149-163`, `_paths_overlap` prefix-compare `:166-174`).
- MCP tools: parallel only if the server opted in (`_is_mcp_tool_parallel_safe` `:90-100`).
- Anything else → sequential (conservative default).

### 3.2 `execute_tool_calls_concurrent` (`tool_executor.py:243-766`)
Call flow:
1. **Pre-flight interrupt** (`:253-261`): if interrupted, append cancelled-result
   messages for every call and return.
2. **Parse + pre-exec bookkeeping per call** (`:264-416`): JSON-parse args
   (bad → `{}`); reset nudge counters; **Tool Search unwrap** (`:281-314`, peel the
   bridge to the real tool, enforce session scope); tool_request middleware
   (`:316-322`); **block evaluation BEFORE checkpoint** — scope block, plugin
   pre-tool-call block (`:344-373`), then guardrail `agent._tool_guardrails.before_call`
   (`:375-390`); checkpoint preflight for `write_file/patch/terminal` (`:392-414`).
3. **Concurrent run** (`:450-623`): `ThreadPoolExecutor(max_workers=min(n,8))`
   (`_MAX_TOOL_WORKERS=8`, `:52,561`). Each worker registers its tid for interrupt
   fan-out (`:467-478`), sets the thread-local activity callback (`:483-487`), and
   calls `agent._invoke_tool(...)` (`:494-503`). `propagate_context_to_thread`
   carries ContextVars + approval callbacks into the worker (`:567-569`). The wait
   loop polls every 5 s, heartbeats every ~30 s, and on interrupt cancels not-started
   futures (`:579-617`). **Results are collected by original index** (`results[index]`,
   `:452,531`) so message order matches call order.
4. **Post-exec per tool** (`:625-753`): append guardrail observation, file-mutation
   verifier record, callbacks, `maybe_persist_tool_result` (offload huge results),
   subdir hints, multimodal unwrap, and `messages.append(make_tool_result_message(...))`.
5. **Turn budget** `enforce_turn_budget` over the turn's tool msgs (`:755-759`),
   then `/steer` drain (`:761-766`).

### 3.3 `execute_tool_calls_sequential` (`tool_executor.py:770-1401`)
Same per-call pipeline but in-order with interrupt checks BEFORE each tool (`:776-789`)
and `tool_delay` between calls (`:1389-1390`). Several agent-runtime tools (`todo`,
`session_search`, `memory`, `clarify`, `delegate_task`, context-engine, memory-provider)
are dispatched **inline** here (`:964-1170`) instead of through `handle_function_call`;
everything else goes through `_ra().handle_function_call(...)` (`:1221-1235`). The inline
tools are the agent-loop-owned ones — they need agent state (stores, callbacks) the
stateless registry handler can't reach. **This is the seam LunaMoth's chara-life tools
map onto** (see §5).

### 3.4 `make_tool_result_message` (`tool_dispatch_helpers.py:320-397`)
Builds `{"role":"tool","name","tool_name","content","tool_call_id"}`. Wraps string
content from untrusted tools (`web_*`, `browser_*`, `mcp_*`) in
`<untrusted_tool_result>` delimiters (`:351-397`) — promptware defense.

### 3.5 `tool_result_classification.py` (`agent/tool_result_classification.py:1-26`)
`FILE_MUTATING_TOOL_NAMES = {write_file, patch}`; `file_mutation_result_landed()`
proves a write succeeded by parsing the JSON result (`bytes_written` / `success:true`).
Feeds the guardrail's failure classifier and the turn-end verifier.

### 3.6 `turn_finalizer.finalize_turn` (`agent/turn_finalizer.py:30-428`)
Post-loop tail. **Max-iteration handling** (`:53-122`): when `final_response is None`
and `api_call_count >= max_iterations` (or iteration budget exhausted), it sets
`_turn_exit_reason="max_iterations_reached(...)"`, emits a status, and calls
`agent._handle_max_iterations(messages, api_call_count)` — **one extra API call with
tools stripped** asking the model to summarize. Then assembles the `result` dict
(`:326-354`: `final_response`, `completed`, `turn_exit_reason`, `interrupted`, token
counts, `guardrail` metadata if halted, `pending_steer`). Also: file-mutation verifier
footer (`:189-212`), turn-completion explainer (`:214-261`).

---

## 4. The handler contract (the touchpoints LunaMoth must supply)

### 4.1 Signature & return
A registry handler is `handler(args: dict, **kwargs) -> str`. Args come as ONE dict
(the model's parsed `function.arguments`); infra is threaded via `**kwargs` that
`registry.dispatch` forwards (`registry.py:404`): `handle_function_call` passes
`task_id=`, and `user_task=` (or `enabled_tools=` for `execute_code`)
(`model_tools.py:1114-1126`). **Return is a string** (JSON or plain text), OR a
multimodal envelope dict `{"_multimodal":True,"content":[...],"text_summary":...}`
(`tool_dispatch_helpers.py:177-188`) for vision tools. Handlers use
`tool_error`/`tool_result` to build JSON strings (`registry.py:563-589`).

### 4.2 Error classification
Two layers:
- `registry.dispatch` catches all exceptions → `{"error":...}` string (`registry.py:405-416`).
- The executor classifies a *returned* result as failure via
  `agent.display._detect_tool_failure` (`tool_executor.py:526,1268`) and
  `guardrails.classify_tool_failure` (`tool_guardrails.py:189-221`: terminal non-zero
  exit, `memory` full, `"error"`/`"failed"` substrings, `Error` prefix — but a landed
  file mutation is never a failure). `failed` feeds `before_call` counters next turn.

### 4.3 How a handler reaches infra (the exact map LunaMoth must supply)
Hermes handlers reach infra **not via injected objects but via the `task_id` kwarg +
process env**:
| hermes touchpoint | how the handler reaches it | LunaMoth equivalent to supply |
|---|---|---|
| **filesystem root / cwd** | `TERMINAL_CWD` env + `task_id`→`_resolve_path_for_task` (`file_tools.py:81-150`) | the chara workspace: `Sandbox.root/"workspace"` (`tools/sandbox.py:19`; gateway uses it `gateway.py:219-241`) |
| **shell / execution backend** | `get_active_env(task_id)` → a `BaseEnvironment` subclass (local/docker/modal/ssh) (`tools/environments/base.py:288`; selected per `task_id`) | LunaMoth isolation: `runner.run_terminal(cmd, workspace, allow_network, writable_paths, timeout, workdir)` (`tools/runner.py:215`) over dir/sandbox/docker |
| **per-session isolation key** | `task_id` (top-level agent = `None`→`"default"`, `terminal_tool.py:996-1027`) | one process = one chara (`config.py` env-pinned) — `task_id` collapses to a single workspace; LunaMoth needs no multi-task keying |
| **LLM client** (delegate/MoA tools) | imported lazily inside the handler | `core/llm.py` `LLMClient` |
| **result-size budget** | `registry.get_max_result_size(name)` (`registry.py:422-430`) | LunaMoth's 6000-char truncation in `_execute_tool` (`core/agent.py:633-637`) |

Because LunaMoth is one-process-one-chara, the cleanest graft binds infra at
**registration time** via closures (handler factories capture `sandbox`, `state`,
`runner`, `memory`, `goals`, `skills`) rather than threading `task_id` everywhere.
This is exactly how hermes MCP handlers already work
(`_make_tool_handler(server,...)` closure, `mcp_tool.py:3414`).

---

## 5. LunaMoth's CURRENT integration (what gets replaced/kept)

### 5.1 `tools/gateway.py` (`src/lunamoth/tools/gateway.py:1-580`) — REPLACED
- `ToolGateway.call(name, /, **kwargs) -> {"ok","data"|"error"}` (`gateway.py:75-88`):
  positional-only name; loop-guard signature/refusal/record around an allowlist
  dispatch. **This is hermes `registry.dispatch` + `tool_guardrails` fused into one
  class.**
- `_effective()` (`gateway.py:64-70`): callable = `implemented ∩ state.tool_access ∩
  pack.enabled_tools`. **This is hermes toolset scoping** (`get_definitions(set)`).
- `_all_schemas()` (`gateway.py:360-550`): a hand-maintained dict of every tool's
  schema. **This is what the registry replaces** — each schema moves next to its
  handler as a `registry.register(...)` call.
- `schemas()` (`gateway.py:552-566`): emits OpenAI `{"type":"function","function":...}`
  for effective tools + MCP. **This becomes `registry.get_definitions(effective_set)`.**
- `tool_*` methods (`gateway.py:212-356`): the handler bodies. They return raw Python
  (str/list/dict); the gateway wraps in `{"ok":True,"data":...}`. Hermes handlers
  return strings directly; the executor classifies. **Bodies port over; the wrapping
  changes.**

**MUST SURVIVE (chara-life tools):** `tool_speak` (`gateway.py:287-293`, delivery
handled in agent loop), `tool_rest` (`:298-306`, `state.set_rest_until`), `tool_add_goal`
(`:264-268`), `tool_set_goal_status` (`:270-274`), `tool_request_permission`
(`:308-356`, presence-gated via `permission_hook`). These need `state`/`goals`/
`permission_hook` — they're LunaMoth's analogue of hermes's **inline agent-runtime
tools** (§3.3): either register them with closures capturing those objects, OR keep
them as an executor-inline branch.

### 5.2 The #24 loop guardrails (`gateway.py:27-208`) — KEPT, relocated
`GUARD_EXACT_WARN_AT=2 / EXACT_REFUSE_AT=5 / STREAK_REFUSE_AT=8` (`:30-32`);
`_guard_signature` (SHA256 of name+canonical args, `:160-164`), `_guard_refusal`
(`:173-190`), `_guard_record` (`:192-208`), `reset_guardrails` (`:166-171`, called
from `agent.stream_handle` on a fresh user turn, `core/agent.py:681`). This is
LunaMoth's own port of `agent/tool_guardrails.py`. **Keep it** — but it must wrap the
new registry dispatch, not the old `_dispatch`. Map it onto hermes's
`ToolCallGuardrailController.before_call`/observe seam (`tool_guardrails.py:224-257`)
OR keep LunaMoth's simpler version as a wrapper around `registry.dispatch`. The
*refusal-before-execute* + *warn-appended-to-error* behavior must be preserved.

### 5.3 The audit trail (`gateway.py` everywhere) — KEPT
`self.audit.write("tool_call"|"tool_denied"|"tool_crash"|"tool_loop_refused"|...)`
(`:81,95,110,125,127,139,150,152`). Hermes has NO equivalent SECURITY audit (it has
plugin post_tool_call hooks + agent.log diagnostics). **LunaMoth must keep `AuditLog`
and fire it around the new dispatch** — wrap `registry.dispatch` in a thin LunaMoth
dispatch shim that writes audit rows. The audit is identity, per CLAUDE.md
(transcript/audit/logs = three records).

### 5.4 `core/agent.py` — ADAPTED
- `_reply_stream` (`:599-613`): passes `self.tools.schemas()` + `self._execute_tool`
  to `llm.stream_agent`. → `self.tools.schemas()` becomes
  `registry.get_definitions(self._effective_set())`.
- `_execute_tool(tool_call)` (`:615-648`): parses args, `self.tools.call(name,**args)`,
  formats `{"display","content","ok"}`, and **special-cases `speak`** (`:643-647`:
  promotes `args.text` to a say-channel `out["say"]`). → calls the new dispatch shim;
  the speak special-case stays here (it's frontend delivery, not tool logic).
- `_load_toolpack` (`:216-233`) + `tools.set_enabled(pack.tools, pack.mcp_servers)`
  (`:233`): resolves the active pack → enabled set. → feeds the registry's emit set.
- `_agent_loop_active()` (`:569`): gates tool-enabled loop vs plain stream. KEEP.

### 5.5 `core/llm.py` — UNCHANGED (the seam is stable)
`stream_agent(self, user_text, context, stable, volatile, tools, execute, ...)`
(`core/llm.py:796-...`): takes the **OpenAI schema list** (`tools`) and an `execute`
callback that returns `{"display","content","ok","say"?}`. The per-step loop yields
`ToolStart`/`ToolEnd`, appends assistant `tool_calls` + `{"role":"tool",...}` results,
echoes `say` to the say channel, repeats to `max_steps`. **As long as the new layer
still produces (a) an OpenAI schema list and (b) an `execute(tool_call)->dict` callback,
llm.py needs no change.** It already has hermes's arg-repair (`_repair_tool_args`,
`llm.py:146`) and stall detection.

### 5.6 `tools/toolpacks.py` + `core/state.py` — KEPT as the scoping inputs
`ToolPack.tools` / `.mcp_servers` (`toolpacks.py:17-37`); `state.tool_access`
(`core/state.py:15,52-62`). The new emit set is still
`implemented(registry) ∩ state.tool_access ∩ pack.tools` — the same three-way
intersection `_effective()` computes today, now feeding `registry.get_definitions`.

### 5.7 The three-zone prompt's schema emission
`_stable_prefix`/`_volatile_tail` (`core/agent.py:426-560`) assemble the prompt;
the **tool schemas are passed separately** to `stream_agent` (`:607`), NOT embedded in
the zones. So swapping schema *source* (gateway dict → registry) does not touch the
zone machinery. The static "tool-use nudge" + toolpack note in the stable prefix
(per CLAUDE.md) stay as-is.

---

## 6. Graft plan (preserve audit + #24 guards + chara-life + zone emission)

1. Port `tools/registry.py` near-verbatim (drop hermes-specific `model_tools`
   imports in `dispatch`'s error path → use a local sanitizer). Keep `ToolEntry`,
   `register`, `get_definitions` (with 30s check_fn TTL), `dispatch`, toolset queries.
2. Convert each `_all_schemas()` entry + matching `tool_*` body into a
   `registry.register(name, toolset, schema, handler, check_fn=...)` call, where
   `handler` is a closure capturing `sandbox`/`state`/`runner`/`memory`/`goals`/
   `skills` (the §4.3 touchpoint map). Self-register at import.
3. Replace `ToolGateway` with a thin **`LunaToolDispatcher`** that: computes the
   effective set (`implemented ∩ tool_access ∩ pack`), emits via
   `registry.get_definitions`, and on `call(name, **args)`: runs the #24 loop-guard
   refusal → `registry.dispatch` → audit.write → loop-guard record. Keep
   `reset_guardrails`, `permission_hook`, `set_enabled`, `mcp_allowed`.
4. Keep chara-life tools (`speak`/`rest`/`add_goal`/`set_goal_status`/
   `request_permission`) as registered tools whose closures hold `state`/`goals`/
   `permission_hook`; keep the `speak`→say-channel promotion in `agent._execute_tool`.
5. Optionally port `agent/tool_executor.py` parallel path + `tool_dispatch_helpers`
   gating later (LunaMoth's llm.py loop is currently sequential; hermes concurrency is
   an additive upgrade, NOT required for parity). The handler contract + registry are
   the load-bearing transplant.

Open question for the owner: keep LunaMoth's compact `{"ok","data"|"error"}` envelope
(simpler, audit-friendly) vs hermes's bare-string returns. Recommendation: keep the
LunaMoth envelope at the dispatcher boundary, have registered handlers return strings,
and let the dispatcher wrap — preserves both the audit shape and hermes handler ergonomics.
