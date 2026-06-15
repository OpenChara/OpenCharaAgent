# Hermes general-agentic tools — faithful re-implementation SPEC for LunaMoth

Source tree: `reference/hermes-agent/tools/`. All cites are `file:line` against
that tree. This document specs the *observable contract* (schema, behavior,
return shape, error handling) so a LunaMoth engineer can rebuild apple-to-apple,
plus the honest mapping onto LunaMoth's infra (one process per chara, OpenAI-
compatible `core/llm.py`, `ToolGateway` allowlist, transcript SQLite, sandbox
isolation, `CharaHandle`).

LunaMoth anchors referenced throughout:
- Tool dispatch / allowlist: `tools/gateway.py` (`ToolGateway`, positional-only `call(name, /)`).
- Terminal under isolation: `tools/runner.py`, `tools/sandbox.py`.
- LLM (OpenAI-compatible streaming + tool loop, yields protocol events): `core/llm.py`.
- Per-chara transcript SQLite (WAL, epochs for /reset, `kind="summary"` rows): `core/transcript.py`.
- Durable memory (frozen two-store + `memory` tool): `tools/memory.py`.
- Goals: `tools/goals.py`. Skills: `tools/skills.py`.
- Backend surface frontends see: `protocol/api.py` (`CharaHandle`).

---

## 1. web_search + web_extract (`tools/web_tools.py`)

### web_search — schema (`web_tools.py:1314-1334`)
```
name: "web_search"
parameters (object):
  query  : string   (required)  — passed verbatim to backend; supports site:/filetype:/intitle:/-term/"exact phrase" when backend honors them
  limit  : integer  default 5, minimum 1, maximum 100
required: ["query"]
```
- Registered with `toolset="web"`, `check_fn=check_web_api_key`, `max_result_size_chars=100_000`, sync handler (`web_tools.py:1353-1362`). Handler passes only `query` + `limit` (`:1357`).

**Behavior** (`web_search_tool`, `web_tools.py:784-888`):
- `limit` coerced to int, clamped `[1,100]` (`:818-822`).
- Interrupt check first → returns `tool_error("Interrupted", success=False)` (`:836-838`).
- Resolves a backend via per-capability config (`web.search_backend` → `web.backend` → env auto-detect), then dispatches through the web-search plugin **registry** (`get_provider(backend)` → fall back to `get_active_search_provider()`) (`:844-871`). Backends: exa, parallel, tavily, firecrawl, searxng, brave-free, ddgs, xai (`:152`).
- No provider configured → `{"success": False, "error": "No web search provider configured. Run `hermes tools` ..."}` (`:858-865`).

**Return** = JSON string (`:799-813`):
```json
{ "success": true, "data": { "web": [ {"title","url","description","position"}, ... ] } }
```
Search returns **metadata only** (no page content). On exception → `tool_error("Error searching web: ...")` (`:880-888`).

### web_extract — schema (`web_tools.py:1336-1351`)
```
name: "web_extract"
parameters (object):
  urls : array<string>  (required)  maxItems 5  — page URLs; PDF links convert to markdown
required: ["urls"]
```
- Registered async, `toolset="web"`, `max_result_size_chars=100_000` (`:1363-1374`). Handler **hardcodes** `format="markdown"` and slices `urls[:5]` (`:1367-1368`). Note: the registered schema exposes **only `urls`**; the underlying `web_extract_tool` function has more params (`format, use_llm_processing, model, min_length`) that the model can't reach.

**Function signature** (`web_extract_tool`, `web_tools.py:891-897`): `urls:List[str]`, `format=None`, `use_llm_processing=True`, `model=None`, `min_length=5000` (`DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION`, `:305`).

**Behavior:**
1. **Secret-in-URL guard** before any fetch: URL-decoded + raw scanned against `agent.redact._PREFIX_RE`; any hit → `{"success":False,"error":"Blocked: URL contains what appears to be an API key or token..."}` (`:920-938`).
2. **SSRF guard**: each URL run through `async_is_safe_url`; private/internal targets become a per-URL blocked result, not fetched (`:960-970`).
3. Dispatch safe URLs to extract backend (registry; async for parallel/firecrawl, sync-in-thread for exa/tavily) (`:976-1041`). Search-only backend selected for extract → typed error telling user to set `web.extract_backend` to firecrawl/tavily/exa/parallel (`:999-1024`).
4. **LLM summarization** (`process_content_with_llm`, `:339-436`) when `use_llm_processing` and an auxiliary model is available:
   - content `< min_length` (5000) → returned raw (returns `None`, no summarization) (`:382-384`).
   - content `> 2,000,000` chars → refused: `"[Content too large to process: X.XMB...]"` (`:367,376-379`).
   - content `> 500,000` chars → chunked (100k chunks summarized in parallel, then synthesized) (`:368,395-399,557-710`).
   - normal → single summarizer LLM pass; **output hard-capped at 5000 chars** (`MAX_OUTPUT_SIZE`, `:370,408-409`).
   - Summarizer model: `AUXILIARY_WEB_EXTRACT_MODEL` env or aux default (Gemini 3 Flash Preview via OpenRouter) (`:316-334`); temp 0.1, max_tokens 20000, retries 2 (`:503,513-530`). On failure → **falls back to truncated raw content** (first 5000 chars), never an error string (`:418-436`).
5. Results **trimmed** to `{url, title, content, error}` (+ `blocked_by_policy` when present) (`:1138-1149`). Base64 images stripped (`clean_base64_images`, `:713-743`). Empty results → `tool_error("Content was inaccessible or not found")` (`:1151-1154`).

**Return:** `{ "results": [ {url, title, content, error}, ... ] }` (`content` = markdown summary or raw).

### Infra coupling LunaMoth lacks → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| Multi-vendor plugin registry (`agent.web_search_registry`, `plugins/web/*`), `hermes tools` config (`web.backend`) | Pick **one** OpenAI-compatible-friendly backend. Simplest faithful port: keep the **search/extract split** but resolve via card/`Settings`/env, not a plugin discovery system. A bundled MCP fetch server (`tools/mcp.py`) or `terminal`+`/net on` already exists per CLAUDE.md — `web_extract` can be a thin wrapper that fetches + html→markdown locally. |
| Auxiliary LLM client (`agent.auxiliary_client`, OpenRouter) for summarization | LunaMoth has exactly one OpenAI-compatible client (`core/llm.py`/`config.LLMConfig`). **Honest shape:** reuse the chara's own LLM config for a *non-streaming* summarization call, OR (preferred, given "no fallback model" principle) make summarization **opt-out and degrade to raw-truncation** when no separate summarizer is configured. Keep the 5000-char cap + 2M refuse + chunk thresholds verbatim — pure constants, no infra. |
| SSRF / secret-in-URL guards (`url_safety`, `agent.redact`) | Port directly — they're standalone and align with LunaMoth's audited-gateway ethos (`obs/audit.py`). Network gating already exists via `/net on` + `request_permission`. |

**Feasibility: HIGH.** Schema + caps + guards port 1:1. Only the summarizer needs a decision (separate aux model vs. raw-truncation default). No multi-process needs.

---

## 2. execute_code (`tools/code_execution_tool.py`) — Programmatic Tool Calling

### Schema (`build_execute_code_schema`, `:1724-1810`; registered `:1821-1832`)
```
name: "execute_code"
parameters (object):
  code : string  (required)  — Python; import tools via `from hermes_tools import ...`; print final result to stdout
required: ["code"]
```
- `toolset="code_execution"`, `check_fn=check_sandbox_requirements` (POSIX-only gate, `:200-204`), `max_result_size_chars=100_000`. The description is **rebuilt per session** to list only the enabled sandbox tools and the active mode (`:1724-1791`).
- Handler passes `task_id` and `enabled_tools` from kwargs (`:1825-1828`).

### What it does
The LLM writes a Python script that runs in a **separate child process** and calls a *whitelisted subset* of Hermes tools over RPC. Only the script's **stdout** returns to the model; intermediate tool results never enter the context window (`:24-26`). Collapses multi-step tool chains into one inference turn.

**Sandbox-allowed tools** (`SANDBOX_ALLOWED_TOOLS`, `:61-69`): `web_search, web_extract, read_file, write_file, search_files, patch, terminal`. The sandbox gets the **intersection** with the session's enabled tools (`:1128-1132`); empty intersection falls back to the full allowlist.

**Generated stub module** `hermes_tools.py` (`generate_hermes_tools_module`, `:259-291`; per-tool stubs `:213-256`): each stub is a thin `_call(name, args_dict)` over the transport, with convenience helpers `json_parse`, `shell_quote`, `retry` baked in (`:296-332`).

**Two transports** (`:9-26`):
- **Local (UDS):** parent opens an `AF_UNIX` stream socket (loopback TCP on Windows), chmod 0600, listens(1), runs an RPC server thread, spawns the child with `HERMES_RPC_SOCKET` env (`:1189-1211`,`_rpc_server_loop` `:468-585`).
- **Remote (file-based RPC):** for docker/ssh/modal/daytona terminal backends, ships files into the env and polls request/response files (`:869-1059`).

**Child process** (`:1281-1290`): `subprocess.Popen([python, script.py])`, `stdin=DEVNULL`, own process group (`os.setsid`), env **scrubbed** (`_scrub_child_env`, `:136-197`): secret-substring names (KEY/TOKEN/SECRET/PASSWORD/AUTH/DSN/WEBHOOK) blocked; only safe prefixes (PATH/HOME/LANG/...) + an explicit allowlist pass. API keys deliberately excluded — the child reaches tools only via RPC, never direct API (`:1214-1224`). `project` vs `strict` mode picks venv-python+session-CWD vs `sys.executable`+tmpdir (`:1576-1690`).

**RPC dispatch** (`_rpc_server_loop`, `:480-574`): each request `{tool, args}` → enforce allowlist (`:517-526`), enforce `max_tool_calls` (default 50, `:528-537`), strip blocked `terminal` params (`background/pty/notify_on_complete/watch_patterns`, `:465,539-542`), then `model_tools.handle_function_call(tool, args, task_id=...)` with stdout/stderr suppressed (`:544-574`). **Approval context is propagated into the RPC thread** (`propagate_context_to_thread`, `:1203-1211`) so dangerous commands still hit the approval gate.

**Limits / safety** (`:72-75`): timeout 300s, max_tool_calls 50, stdout cap 50KB (head 40% + tail 60% truncation, `:1300-1408`), stderr cap 10KB. Whole script pre-screened by `check_execute_code_guard` before spawn (`:1104-1112`). Cooperative interrupt + timeout kill the **whole process tree** via psutil (`_kill_process_group`, `:1501-1548`). Output: ANSI-stripped + secret-redacted (`:1418-1430`).

**Return** (`:1432-1464`): JSON
```json
{ "status":"success|timeout|interrupted|error", "output": "<stdout>",
  "tool_calls_made": N, "duration_seconds": F, "error": "<when failed>" }
```

### Infra coupling LunaMoth lacks → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| `model_tools.handle_function_call` (global tool dispatcher reentered from a thread) | LunaMoth has `ToolGateway.call(name, /)` (`tools/gateway.py`). The RPC server thread would call back into the **same chara's** gateway — feasible since it's one process. |
| Per-session terminal env registry, remote backends (docker/ssh/modal/daytona) | Drop entirely. LunaMoth isolation is `dir`/`sandbox`/`docker` per chara via `tools/runner.py`+`sandbox.py`; there is no multi-backend terminal env map. Local-UDS path only. |
| `tools.approval` / TLS approval callbacks, `check_execute_code_guard` | Map to LunaMoth's `request_permission` (presence-gated) + audit trail (`obs/audit.py`). |
| env scrubbing, redaction (`agent.redact`) | LunaMoth `obs/log.py` already does credential redaction; reuse. Sandbox already restricts FS/network. |

### Feasibility in one-process-per-chara
**FEASIBLE but heavy — port a reduced shape.** The UDS-local design is single-process-friendly: parent (the chara process) opens a socket, spawns a child Python interpreter, and services tool RPCs by calling its own `ToolGateway`. Nothing requires a second agent or session. Honest LunaMoth shape:
- **Keep:** child-process Python sandbox, UDS transport, `hermes_tools.py` stub generation, allowlist∩enabled, 50KB/300s/50-call limits, env scrub + redaction, ANSI strip, JSON return.
- **Cut:** the entire remote/file-RPC path, multi-terminal-backend env map, profile HOME isolation, Windows TCP fallback (macOS/Linux only per CLAUDE.md).
- **Re-map:** RPC dispatch → `ToolGateway.call`; allowed set → whatever the card's toolpack enables (`tools/toolpacks.py`); approval → `request_permission`; child must run **inside the chara's isolation** (sandbox-exec/bwrap/docker via `runner.py`), not a bare `subprocess.Popen`, to honor per-chara jail. That isolation re-map is the real work.
- This is essentially "let the chara write a script that batches its own real tools." Consistent with the "real agency behind an allowlisted gateway" philosophy. Lower-priority than web tools.

---

## 3. delegate_task (`tools/delegate_tool.py`) — Subagent delegation

### Schema (`DELEGATE_TASK_SCHEMA`, `:2773-2890`; registered `:2896-2914`)
```
name: "delegate_task"   (toolset "delegation")
parameters (object), required: []   (validated in-body, not by schema)
  goal      : string                      — single-task goal; self-contained (child has no parent history)
  context   : string                      — background (paths, errors, constraints)
  toolsets  : array<string>               — child toolsets; default = inherit parent's; intersected with parent
  tasks     : array<object>               — BATCH mode; each {goal(req), context, toolsets, acp_command, acp_args, role}
  role      : string  enum["leaf","orchestrator"]   — leaf can't re-delegate; orchestrator can (depth-bounded)
  acp_command : string                    — ACP transport override (Copilot CLI etc.)
  acp_args  : array<string>
```
- `description`, `tasks.description`, `role.description` are **rebuilt per `get_definitions()`** to reflect the user's actual `delegation.max_concurrent_children`/`max_spawn_depth` (`:2775-2787`, `dynamic_schema_overrides`).
- `max_iterations` is accepted in the handler but **ignored if model-supplied** — config is authoritative (`:2035-2041`).

### Behavior
- Modes: **single** (`goal`) or **batch** (`tasks[]`, parallel) (`:1984-1986`). Must provide one or the other → else `tool_error` (`:2076`). Each task needs a non-empty `goal` (`:2087-2088`).
- **Spawn pause kill-switch** (`is_spawn_paused`, `:1-2005`) and **depth limit** (`_delegate_depth >= max_spawn_depth`, default `MAX_DEPTH=1`, `:133,2012-2025`) checked first.
- Each child is a **fresh `AIAgent`** (`run_agent.AIAgent`, `_build_child_agent` `:904-1226`) with: fresh conversation (no parent history), its own `task_id` (own terminal session + file-ops cache), restricted toolset (parent∩requested, blocked tools stripped), focused system prompt built from goal+context (`_build_child_system_prompt`, `:603-676`), `quiet_mode=True`, `skip_memory=True`, `skip_context_files=True`, `clarify_callback=None`.
- **Blocked from children always** (`DELEGATE_BLOCKED_TOOLS`, `:45-53`): `delegate_task, clarify, memory, send_message, execute_code`.
- Parent **blocks** until all children finish; concurrency capped by `delegation.max_concurrent_children` (default 3, `:132,362-397`) via `ThreadPoolExecutor`. Per-child timeout default 600s (`:547,400-424`). Heartbeats keep the gateway alive (`:1409-1422`).
- Child summary = its `final_response` (`:1672`). Tool traces, tokens, cost folded back; intermediate child reasoning/tool calls never reach parent context (`:14-16`).

### Return (`_run_single_child` entry, `:1736-1772`; aggregated to a results array)
```json
{ "results": [ { "task_index":0, "status":"completed|failed|timeout|interrupted",
   "summary":"<child final_response>", "api_calls":N, "duration_seconds":F,
   "model":"...", "exit_reason":"completed|max_iterations|interrupted|timeout|error",
   "tokens":{"input":N,"output":N}, "tool_trace":[{tool,args_bytes,result_bytes,status}],
   "error":"<when failed>" }, ... ] }
```

**Recursion limits:** flat by default (`MAX_DEPTH=1`: parent→child only). `role="orchestrator"` + `delegation.max_spawn_depth>=2` unlocks nested delegation; depth has a floor of 1, **no ceiling** (each level multiplies API cost) (`:133-139,427-463`).

### Infra coupling LunaMoth lacks → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| `run_agent.AIAgent` constructible N times **in one process**, each with own conversation/toolset/task_id | **LunaMoth's hard wall.** CLAUDE.md: "one process = one chara; config paths pinned from env at IMPORT time." `LunaMothAgent` (`core/agent.py`) is a singleton-per-process keyed to one card/sandbox. You cannot spin up 3 sibling agents in-process. |
| Provider/credential pools, fallback chains, ACP transports, OpenRouter provider filters | None exist in LunaMoth (single OpenAI-compatible `core/llm.py`, no fallback model by principle). |
| `model_tools` global tool registry, `session_db` parent/child lineage | LunaMoth has per-chara `ToolGateway` + per-chara transcript; no in-process child-session lineage. |

### Feasibility in one-process-per-chara
**A faithful in-process port is NOT feasible.** The whole design rests on constructing many `AIAgent`s in one process with isolated contexts — LunaMoth's import-time env pinning + singleton agent forbid this. Honest LunaMoth shapes (pick one):
1. **Sub-turn delegate (recommended, lightweight):** a `delegate_task(goal, context)` that runs a **scoped inner LLM loop on the same `core/llm.py` client** with a fresh, ephemeral message list (no chara history, restricted tool subset via a temporary gateway view), returns only the final summary. Keeps "isolated context + only-summary-returns" semantics; sequential, not parallel; no second persona. This matches Hermes' value (context isolation, output reduction) without violating one-process.
2. **Process-level delegate:** spawn a real `lunamoth run NAME -p "<goal>" --stream-json` child via `cli.py`/`server/supervisor.py` against a **purpose-built worker card** (no persona), capture its final say-channel output. True isolation, true parallelism, but heavier and needs a worker card convention + supervisor wiring.
- **Drop:** ACP, provider pools, fallback chains, orchestrator nesting (or cap at depth 1). Keep the blocked-tool list and the results-array return shape verbatim.

**Verdict: feasible only as a re-shaped tool (sub-turn loop or supervised `run -p` child), never as the in-process multi-AIAgent port.**

---

## 4. session_search (`tools/session_search_tool.py`) — transcript recall

### Schema (`SESSION_SEARCH_SCHEMA`, `:627-759`; registered `:765-783`)
```
name: "session_search"   (toolset "session_search")
parameters (object), required: []   (mode inferred from which args are set)
  query              : string                    — DISCOVERY: FTS5 search terms (AND default; OR/"phrase"/NOT/prefix*)
  limit              : integer  default 3 (clamped [1,10])   — discovery: max sessions
  sort               : string   enum["newest","oldest"]     — discovery temporal bias on top of FTS5 rank
  session_id         : string                    — SCROLL/READ anchor session
  around_message_id  : integer                   — SCROLL: center message id
  window             : integer  default 5 (clamped [1,20])  — scroll: ± messages each side
  role_filter        : string                    — CSV roles; discovery default "user,assistant"
  profile            : string                    — read another profile's DB (read-only)
```

### Behavior — **four shapes, no mode param** (`session_search`, `:494-615`)
1. **DISCOVERY** (`query` set, `_discover` `:393-491`): FTS5 over the message store (`db.search_messages`, limit 50 to allow lineage dedupe), dedupe hits by session lineage (`_resolve_to_parent`), return top `limit` sessions. Each result: `session_id, when, source, model, title, matched_role, match_message_id, snippet` (FTS5-highlighted) + `bookend_start` (first 3 user+assistant msgs) + `messages` (±5 around match, anchor flagged) + `bookend_end` (last 3) + `messages_before/after` (`:463-482`).
2. **SCROLL** (`session_id`+`around_message_id`, `_scroll` `:269-390`): ±`window` slice via `get_messages_around`; no FTS5/bookends; lineage rebind if anchor lives in a child session; rejects scrolling the **current** session lineage (already in context) (`:301-308`).
3. **READ** (`session_id` only, `_read_session` `:177-223`): dumps whole session (first 20 + last 10 when large), resolves `@session:<profile>/<id>` links.
4. **BROWSE** (no args, `_list_recent_sessions` `:226-266`): recent sessions chronologically; skips child/delegation sessions and `source="tool"` (`:39`).

- **Zero LLM calls** in any shape — every shape returns actual DB messages (`:22-23`).
- Precedence: scroll > read > discovery > browse (`:553-594`).

### Return (per shape, all `success`-tagged JSON; e.g. discovery `:484-491`)
```json
{ "success": true, "mode":"discover|scroll|read|browse", ... , "results"|"messages": [...], "count": N }
```
Errors → `tool_error(msg, success=False)` (e.g. DB unavailable `:520-527`, anchor not found `:366-370`).

### Infra coupling → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| `hermes_state.SessionDB` with **FTS5 index** + `search_messages/get_anchored_view/get_messages_around` | LunaMoth has per-chara transcript SQLite (`core/transcript.py`, WAL, epochs). **No FTS5 and no anchored-view primitives today** — these must be added. FTS5 is a SQLite virtual table; add a contentless FTS5 mirror of the transcript text + the three window primitives. |
| Multi-profile DB scan (`profiles`, `_locate_session_db`, `@session:` links) | **Drop.** LunaMoth is one chara per process; there is no profile registry. Search only this chara's transcript. Remove `profile`, cross-profile locate, and lineage-root walking (no parent/child sessions in LunaMoth's epoch model). |
| Session lineage (`parent_session_id`, delegation children) | LunaMoth uses **epochs** (`/reset`), not parent/child sessions. Map "lineage dedupe" → dedupe by epoch; "current session" guard → current epoch. |

**Feasibility: HIGH (with one build task).** The contract is pure-SQLite, zero-LLM, single-process — a perfect fit. Cost = adding an FTS5 index + the three window helpers to `core/transcript.py`. Strip the entire multi-profile/lineage layer. The discovery/scroll/read/browse shape and JSON return port cleanly.

---

## 5. todo (`tools/todo_tool.py`) — the task list

### Schema (`TODO_SCHEMA`, `:240-294`; registered `:300-308`)
```
name: "todo"   (toolset "todo")
parameters (object), required: []
  todos : array<object>   — write these items; OMIT to read current list
     item: { id:string(req), content:string(req), status:enum["pending","in_progress","completed","cancelled"](req) }
  merge : boolean  default false   — false=replace whole list; true=update-by-id + append new
```

### Behavior (`TodoStore`, `:36-184`; `todo_tool`, `:187-226`)
- **In-memory, one `TodoStore` per agent/session** (`:7,37`). Items are ordered (position = priority).
- **Write** (`:49-96`): `merge=false` replaces the list (validated, deduped by id keeping last); `merge=true` updates existing items by id (only provided fields) and appends new ones, preserving order.
- **Read** = call with no `todos` → returns full list (`:206-209`).
- **Validation** (`_validate`, `:153-175`): empty id → `"?"`; empty content → `"(no description)"`; invalid status → `"pending"`; only `{id,content,status}` kept.
- **Caps** (`:31-32`): content truncated at 4000 chars (`… [truncated]`), max 256 items (head kept).
- **Persistence:** NOT to disk. State lives on the agent and is **re-injected after context compaction** via `format_for_injection` (`:106-138`) — only `pending`/`in_progress` items, as `[x]/[>]/[ ]/[~]` markers, prefixed `"[Your active task list was preserved across context compression]"`. Completed/cancelled deliberately excluded so the model doesn't redo finished work.

### Return (`:217-226`)
```json
{ "todos":[{id,content,status},...],
  "summary":{"total":N,"pending":N,"in_progress":N,"completed":N,"cancelled":N} }
```
`store is None` → `tool_error("TodoStore not initialized")`.

### Infra coupling → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| `store` injected from the `AIAgent` instance (per-session in-memory) | LunaMoth: attach a `TodoStore` to `LunaMothAgent` (`core/agent.py`); pass it through `ToolGateway`. One chara = one process = one store — trivially clean. |
| Re-injection hook into post-compaction history | LunaMoth has `core/compaction.py` (Hermes-style summary compaction). Call `format_for_injection()` and append into the volatile tail / post-compaction message exactly as Hermes does. Optionally persist the list as a transcript row so it survives `/reset`-less restarts (Hermes keeps it purely in-memory; matching that is fine). |

**Feasibility: HIGH — near-verbatim port.** No external infra. The only integration point is the compaction re-injection hook, which LunaMoth's `compaction.py` already has a natural seam for. This is the cheapest tool to port.

---

## 6. clarify (`tools/clarify_tool.py`)

### Schema (`CLARIFY_SCHEMA`, `:87-125`; registered `:131-141`)
```
name: "clarify"   (toolset "clarify")
parameters (object), required: ["question"]
  question : string   (required)  — the question to present
  choices  : array<string>  maxItems 4  — optional; omit for open-ended; UI auto-appends a 5th "Other (type your answer)"
```

### Behavior (`clarify_tool`, `:23-75`)
- Empty `question` → `tool_error("Question text is required.")` (`:42-43`).
- `choices` validated: must be list, trimmed, capped at `MAX_CHOICES=4` (`:20,47-55`); empty list → treated as open-ended (`None`).
- Delegates the actual UI to a **platform-provided `callback(question, choices) -> str`** injected by the runner (`:9-11,57`). No callback → `{"error":"Clarify tool is not available in this execution context."}` (`:57-61`). Callback exception → `{"error":"Failed to get user input: ..."}` (`:64-69`).
- Returns `{ "question", "choices_offered", "user_response" }` (`:71-75`).

### Infra coupling → mapping
| Hermes dependency | LunaMoth mapping |
|---|---|
| Platform `callback` (cli.py arrow-key picker / gateway numbered list) | LunaMoth equivalent: route through `protocol/` to whichever frontend is attached. But note **presence**: LunaMoth charas run unattended; a blocking question only makes sense when `user_present`. Map "no callback" → "not available" exactly (it already covers the unattended case). The natural LunaMoth home is alongside `speak`/`request_permission` (chara-life tools, `tools/`), gated on presence (`presence/`). For say-only messaging frontends, render choices as a numbered list (Hermes does the same on messaging platforms, `:6-7`). |

**Feasibility: HIGH.** Pure schema + a callback seam; LunaMoth's protocol/frontend split + presence model already provide the callback injection point. The one design note: it must be presence-gated (a resting chara cannot block on a human), which fits LunaMoth's `request_permission` precedent.

---

## Cross-cutting notes for the porting engineer
- Every Hermes tool returns a **JSON string** (or `tool_error(...)` from `tools/registry.py`) — LunaMoth's `ToolGateway` should preserve the same string-return contract so tool results stay model-readable.
- Hermes registers tools declaratively (`registry.register(name, toolset, schema, handler, check_fn, ...)`). LunaMoth's analog is the allowlisted `ToolGateway` + `tools/toolpacks.py`; carry over `check_fn` (availability gate) and `max_result_size_chars` (output cap) as gateway concepts.
- "No failure fallbacks" (LunaMoth principle) conflicts with two Hermes behaviors: web_extract's silent raw-truncation fallback and delegate's provider-fallback chains. For web_extract keep the truncation (it's a *backstop like compaction→trim*, allowed). For delegate, drop fallback chains entirely.
