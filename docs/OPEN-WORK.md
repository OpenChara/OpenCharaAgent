# LunaMoth — open work

This is the ONE surviving doc after the 2026-06-13 docs cleanup. The settled
design specs (prompt machine, desktop, supervisor) and the historical research
(hermes UI/code study, WeChat research, the webui task book) were deleted — their
conclusions live in `CLAUDE.md`, the code, and git history. What's kept here is
only what's still *open*: the engineering hardening backlog (Part 1) and the
deferred product ideas worth remembering (Part 2).

> **Verify before starting — several items have LANDED since this audit was
> written (2026-06-13):** #27 (chara child 3-strike auto-restart — `CharaChild`
> now has `RestartBackoff`), #30 (inbound dedup — `MessageDeduplicator` in both
> the gateway and the in-child host), #31 (outbound send containment — bounded
> retry + `DeliveryDeferred`, tested). The gateway also moved in-process
> (`server/messaging_host.py` shares the chara's one agent), so GatewayChild's
> backoff concern in #27 is moot. Re-check each item against current code.

---

# Part 1 — Engineering hardening (hermes-parity)

Read-only audit of `src/lunamoth/` against
`reference/hermes-agent` (the cloned upstream). Scope: the commodity agent
infrastructure only (llm loop, compaction/context, tools, server, messaging).
The innovation core (presence, chara life, cards, world info, curriculum) has
no hermes counterpart and was not audited.

Legend: **Effort** S (<½ day) / M (1–2 days) / L (multi-day).
**Priority** P1 = correctness/data-loss/hang risk, P2 = robustness under real
usage, P3 = polish. All hermes references are `reference/hermes-agent/<path>`.

---

## 0. The four known roadmap items (CLAUDE.md C4), precisely

These are confirmed real and detailed here first; they reappear as items 1–4.

**(a) Stream stall detection** — hermes runs a *dual-layer* watchdog:
an httpx socket read timeout (`HERMES_STREAM_READ_TIMEOUT`, default 120 s;
`agent/chat_completion_helpers.py:1755-1801`) plus an outer 0.3 s poll loop
that tracks wall-clock time since the last *payload* chunk
(`chat_completion_helpers.py:2555-2589`; `HERMES_STREAM_STALE_TIMEOUT`
default 180 s). The outer loop exists because SSE keep-alive pings defeat
socket timeouts. The stale timeout scales with context (240 s above 50k
tokens, 300 s above 100k — "Cloud reasoning models routinely pause mid-stream
for minutes during extended thinking", :2788) and is *disabled entirely* for
local endpoints (Ollama/vLLM prefill can take 300+ s, :2515-2517). On stall:
kill the connection, rebuild the HTTP client pool to purge dead sockets,
surface a status line. There is also a Codex-specific first-byte (TTFB)
watchdog (:264-340) for endpoints that accept the connection and never emit
one event. *LunaMoth today*: `urlopen(timeout=90/120)` gives an implicit
per-read timeout, but any keep-alive traffic resets it; no payload-level
wall clock, no first-token deadline, no client teardown on stall.

**(b) Tool-call args repair** — `agent/message_sanitization.py:185-279`
(`_repair_tool_call_arguments`), a four-pass pipeline: (0) `json.loads(...,
strict=False)` + re-serialize (accepts literal control chars — "the most
common local-model repair case (#12068)"); (1) strip trailing commas, close
unclosed braces/brackets by counting; (2) pop excess closers (bounded 50
iterations); (3) escape raw control chars inside strings; last resort return
`"{}"` — "far better than a crashed session" (the GLM-via-Ollama scar,
`chat_completion_helpers.py:2016-2022`). Applied both at stream end
(:2012-2029) and as a pre-flight over replayed history
(`conversation_loop.py:724-737`), so one bad turn doesn't poison every later
request. *LunaMoth today* (`core/agent.py:573-579`): `JSONDecodeError → {}`,
which then trips the gateway's missing-required-args message — a reasonable
model-visible error, but repairable calls (trailing comma, unclosed brace)
are needlessly failed, and broken args *persisted into history* are replayed
verbatim forever.

**(c) Parallel tool execution** — `agent/tool_executor.py:243-767`
(`execute_tool_calls_concurrent`), ThreadPoolExecutor with
`_MAX_TOOL_WORKERS = 8` (:50-52). Gating logic in
`agent/tool_dispatch_helpers.py:103-147`: never parallelize batches of 1 or
batches containing interactive tools; only known parallel-safe read-only
tools, or path-scoped mutators whose normalized paths don't overlap
(`_paths_overlap`); MCP tools opt in explicitly. Results are re-ordered to
the original tool_call index before being appended to messages; interrupts
cancel unstarted futures. *LunaMoth port shape*: parallel-safe set =
{list_files, read_file, list_workspace, read_workspace_file, inspect_env,
read_skill}; serialize terminal/write_file/memory/speak/rest/
request_permission and all MCP calls by default.

**(d) Empty/zero-chunk completion handling** (the fourth leftover) — a
stream that ends with no content, no reasoning, no tool calls and no
finish_reason raises `RuntimeError("Provider returned an empty stream …")`
and routes through the retry loop (`chat_completion_helpers.py:2043-2052`);
empty *content* with reasoning present is distinguished ("thinking
exhausted") from truly-empty, with ≤3 retries
(`conversation_loop.py:3994-4101`). *LunaMoth today*
(`core/llm.py:_stream_turn`/`stream_agent`): an empty stream produces
`text=""`, no tools, `finish=""` → an assistant message with `content: None`
is recorded and the turn ends *silently* — the user sees nothing and nothing
indicates failure, which violates the project's own "no fabricated output,
visible errors" principle by the back door (an invisible non-answer).

---

## 1. core/llm.py — streaming + tool loop

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 1 | **Stream stall watchdog** (payload-level wall clock + first-token deadline; teardown on stall) | `chat_completion_helpers.py:2555-2589, 1755-1801, 264-340`; constants 180/240/300 s, local-endpoint exemption | implicit per-read socket timeout only; SSE keep-alives defeat it; no stall notice | M | **P1** |
| 2 | **Tool-args JSON repair** (4-pass; also repair *replayed* history args) | `message_sanitization.py:185-279`; `conversation_loop.py:724-737` | `JSONDecodeError → {}` → missing-args error; broken args replayed forever from context | S | P2 |
| 3 | **Parallel tool execution** (read-only batch concurrency, ordered results) | `tool_executor.py:243-767`, `tool_dispatch_helpers.py:103-147` | strictly serial loop in `stream_agent` | M | P3 |
| 4 | **Empty-completion detection + bounded retry** (zero-chunk, missing finish_reason, reasoning-only) | `chat_completion_helpers.py:2043-2104`; `conversation_loop.py:3994-4101` (≤3 retries) | silent empty turn, `content: None` recorded, no error surfaced | S | **P1** |
| 5 | **Jittered backoff + Retry-After for 429** | `agent/retry_utils.py:19-57` — `min(base·2^(n−1), 120) + U(0, 0.5·delay)`; jitter "decorrelates concurrent retries" | flat 5 s × 5 for everything; a 60 s provider rate window burns all 5 attempts | S | P2 |
| 6 | **Clean-close mid-tool-args ≠ "length"** — a stream that closes without finish_reason while args are partial must be reported as an honest stream drop, not routed down the truncation path | scar at `chat_completion_helpers.py:2060-2104` (Nemotron: "Stamping 'length' here … retries 3× to no effect … the red herring this guards against") | partial args parse-fail → `{}` → missing-args error; survivable but mislabels the failure; fold into items 2+4 | S | P3 |
| 7 | **Lone-surrogate sanitization** of model output before re-serialization | `message_sanitization.py:31-140`; scar `conversation_loop.py:741-743` ("Ollama Kimi/GLM/Qwen … lone surrogates crash json.dumps … prevents the 3-retry cycle") | model text goes into context and back out un-scrubbed; any `ensure_ascii=False` serialization path (messaging adapters, codec) can blow up on U+D800–DFFF | S | P2 |
| 8 | **Capture `usage` from the stream** and prefer real `prompt_tokens` over the heuristic for compaction triggers | `context_compressor.py:714-764` ("defers to recent real API usage over known-noisy rough estimates") | `estimate_tokens()` char heuristic only; CJK-heavy or tool-heavy windows can be off enough to either thrash or overflow | S | P2 |
| 9 | **Step-budget exhaustion is announced** — when max iterations hit, tell the model/user instead of stopping silently | `agent/turn_finalizer.py:52-70` (`_handle_max_iterations` summary + `_turn_exit_reason` explainer, :230-261) | `stream_agent` exhausts `max_steps=8` and just returns; mid-task work stops with no marker in context or UI | S | P2 |

Already at parity (worth knowing, no action): function-name *assignment*
rather than concatenation in tool-call deltas (llm.py:509 matches the
MiniMax-redelivery fix at `chat_completion_helpers.py:1961-1970`); orphaned
tool-result dropping before send (`context.render()` ≈ hermes
`_sanitize_api_messages`); null-content → `""` for strict providers; the
truncated-tool-call drop-and-split-note pattern; continuation notes on
`finish=length`; interrupt-commit of partials.

## 2. core/compaction.py + core/context.py

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 10 | **Anti-thrashing guard + failure cooldown** — count ineffective compactions, stop after 2; cooldown (30–600 s) after summarizer failures | `context_compressor.py:744-764, 1973-1989` (scar #40803: "Without recording this as an ineffective compression the anti-thrashing guard never fires"); failure cooldowns :1456-1562 | `should_compact()` re-fires **every turn** once over threshold; if the summary call fails or doesn't shrink the window, that's one extra LLM call per turn forever — the same failure family as the 2 s-patience burned-key incident | S | **P1** |
| 11 | **Tail boundary respects tool pairs and the last user message** | `_align_boundary_forward/backward` :1678-1730; `_ensure_last_user_message_in_tail` :1745-1790 (scar #10896: the active task ends up summarized and the model ignores it) | tail cut is token-walk only: it can (a) start the tail with orphan tool results which `render()` then silently drops, and (b) push the operator's most recent instruction into the summary | S | P2 |
| 12 | **Protect the summary head from `trim()`** — the backstop trim pops `messages[0]` first, which after compaction *is* the summary: the one message holding everything old | hermes always keeps the summary in the protected region (`context_compressor.py:1618-1676` keeps pairing sane around it) | `context.trim()` (context.py:143-151) happily drops `kind="summary"` first, deleting the entire compressed past in one pop | S | **P1** |
| 13 | **Prune old tool outputs in the LIVE window**, not just the summarizer copy — dedup identical results by hash, one-liner results older than the tail | three-pass pre-pruning `context_compressor.py:770-936` (e.g. `[terminal] ran 'npm test' -> exit 0, 47 lines`) | `_prune_tool_outputs_for_summary` only affects the serialized copy; live window carries full 6 KB tool results until whole-message trim/compaction | M | P2 |

Skipped knowingly: hermes' static fallback summary (`:1017-1204`) conflicts
with the no-fabrication policy (LunaMoth's trim-backstop is the agreed
degradation); compression locks (#34351) are for multi-agent-per-session —
one process per chara makes them moot; image pruning (#9434) — text-only;
Anthropic `cache_control` layout (`prompt_caching.py`) — LunaMoth's
three-zone byte-stable prefix is the OpenAI-compatible equivalent and is
already stricter than hermes' rotating last-3 scheme.

## 3. tools/runner.py — the terminal

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 14 | **Kill the process GROUP on timeout, then drain non-blocking** — `subprocess.run(timeout=)` kills only the leader; a grandchild inheriting the stdout pipe keeps `communicate()` blocked **forever**, so the tool call hangs despite the timeout, and orphans keep running | `process_registry.py:436-500` (SIGTERM children recursively before parent), :922-992 (scar #17327: "a descendant … still holding the stdout pipe open, the reader blocks forever" → non-blocking drain) | `start_new_session=True` creates the group but nothing signals it; the docstring's "timeout kills children" claim is wrong — Python's `run()` does not killpg. Replace with Popen + killpg + bounded O_NONBLOCK drain (PtyBridge.close already has the right pattern to copy) | S | **P1** |
| 15 | **Explicit truncation with head+tail split** | terminal truncates 40% head / 60% tail with marker (`terminal_tool.py:2406-2409`); limits configurable (`tool_output_limits.py:39-110`: 50 KB / 2000 lines / 2000 chars-per-line) | runner silently keeps only the LAST 12 000 chars (`(proc.stdout or "")[-_OUTPUT_CAP:]`) — the agent layer's truncation note then reports sizes of an already-silently-cut string; early errors (often the head) vanish | S | P2 |
| 16 | **ANSI/control stripping** of command output before it reaches the model | `tools/ansi_strip.py:16-44` — full ECMA-48 (CSI incl. private modes, OSC with BEL/ST, DCS/SOS/PM/APC, 8-bit C1), fast-path skip | raw bytes pass through; colored/TUI output wastes tokens and can derail weaker models | S | P2 |
| 17 | **Clamp the model-supplied timeout** | `TERMINAL_TIMEOUT` default 180 s, clamped with a note to the caller (`process_registry.py:1056-1131`); deadlines on `time.monotonic()` | `tool_terminal` accepts any integer — the model can ask for `timeout=999999` and wedge an unattended cycle for days | S | P2 |
| 18 | **Exit-code annotation** — tell the model that grep/rg/diff exit 1 means "no match", not failure | `terminal_tool.py:1609-1670` | bare `exit=1` invites pointless retries | S | P3 |

## 4. tools/mcp.py

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 19 | **Use the RPC timeout** — `_RPC_TIMEOUT = 30.0` is defined at mcp.py:40 and **never used**; `_rpc()` blocks on `for line in self.proc.stdout` indefinitely. A hung MCP server wedges the whole turn (and the worker thread) forever — there is no "caller's patience" anywhere above it | per-call timeout 120 s, connect 60 s, both per-server configurable (`tools/mcp_tool.py:20-21,37`) | unbounded block; also `list_tools` during `schemas()` can hang session startup | S | **P1** |
| 20 | **MCP stderr to a log file, not DEVNULL** | scar `tools/mcp_tool.py:103-116`: SDK default leaked subprocess stderr into the live TUI; fix = shared `mcp-stderr.log` with server-name headers | `stderr=subprocess.DEVNULL` — a crashing server leaves zero diagnostics; route to `sandbox/logs/` | S | P2 |
| 21 | **Reap on close** — `close()` terminates but never waits; repeated reconfigure leaves zombies | explicit shutdown in finally blocks; process registry reaps with `wait(timeout=5)` | `terminate()` then drop the handle; add `wait(timeout)` + `kill()` escalation | S | P2 |
| 22 | **Schema sanitization for strict backends** (nullable unions, `pattern`/`format`, empty-object `properties`, top-level combinators) | `tools/schema_sanitizer.py:40-445`; scar: llama.cpp "Unable to generate parser for this template"; xAI rejects `pattern`/`format` and `/` in enums (#27907 — and note the deep-copy lesson: sanitizers must not mutate the shared registry) | MCP `inputSchema` forwarded verbatim; fine on OpenRouter, will 400 on local llama.cpp routes | M | P3 |

## 5. tools/gateway.py + memory/skills

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 23 | **Catch-all exception boundary around tool dispatch** — `call()` catches only `(TypeError, SandboxViolation, FileNotFoundError, ValueError, PermissionError)`; an `OSError`/`KeyError`/`BrokenPipeError` (e.g. MCP `_send` to a dead process raises BrokenPipeError, not McpError) escapes `execute()` and **kills the whole streaming turn** with a raw traceback instead of feeding an error to the model | `tool_executor.py` wraps every call; failures become classified tool results (`tool_result_classification.py`), never turn aborts | add a final `except Exception` → `{"ok": False, "error": ...}` + audit; keep the typed branches for nicer messages | S | **P1** |
| 24 | **Loop guardrails** — repeated identical failing calls warned then blocked; same-tool failure halt; no-progress detection for idempotent reads | `tool_guardrails.py:20-82, 224-376` — SHA256 of canonical args; warn at 2 identical failures / block at 5; halt turn after 8 same-tool failures; no-progress warn 2 / block 5 | nothing: an unattended chara can spend a whole night (and a key's budget) re-running the same failing terminal command. Matches the patience-burn scar class | M | P2 |
| 25 | **Memory write durability + drift guard** — fsync before atomic replace; detect external edits (round-trip mismatch / oversized single entry) and back up instead of clobbering | `memory_tool.py:577-606` (mkstemp→write→fsync→replace), :522-575 (scar #26045: flushing after external append "would truncate that entry … discarding the appended bytes" → `.bak.<ts>` + refuse) | `tmp.write_text` + `replace` but **no fsync** (power-loss window), and `except OSError: pass` silently swallows a failed memory write — the chara is told "saved" (gateway returns ok) when nothing landed | S | P2 |
| 26 | **Silent truncation in skills/memory tool responses** — `create_skill` writes `text[:24_000]` with no signal; memory `_write` truncates `text[:cap]` mid-entry | hermes rejects over-limit memory adds with "consolidate" guidance (`memory_tool.py:330-341`) instead of silently cutting | return an explicit error/notice instead of slicing — silent cuts violate the project's own explicitness rule (cf. agent.py:589 comment) | S | P3 |

## 6. server/ (supervisor, dispatch, ws)

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 27 | **Chara child auto-restart with stuck-loop cap** — a crashed resident stays dead: `state="crashed"`, `ensure_started()` raises, and an unattended chara dies silently until a human notices the board. Gateways already restart (60 s→1800 s) but charas don't | gateway/session.py:920-943 + `_STUCK_LOOP_THRESHOLD = 3` (#7536): restart with backoff, suspend after 3 consecutive crash-restarts instead of looping forever | port the GatewayChild `_run_supervised` pattern to CharaChild with the 3-strike suspension; also fix GatewayChild's `_backoff` never resetting after a healthy run (one crash a week eventually means 30-min restart delays) | M | P2 |
| 28 | **Shutdown forensics + resource canary** — record what triggered shutdown and a process snapshot; periodic RSS/GC/thread log line for leak detection | `gateway/shutdown_forensics.py:197-406` (incl. the systemd `TimeoutStopSec >= drain_timeout` scar); `gateway/memory_monitor.py:119-126` (5-min `[MEMORY] rss/gc/threads` line, daemon thread) | lunamothd logs nothing on unexpected exit; a leak in a week-long daemon is invisible until the OS kills it | S | P3 |
| 29 | **Slow-client backpressure on the event path** — `_WSSink.write` from the agent thread blocks up to 10 s per frame on a stalled browser; a wedged client can slow a streaming turn to a crawl | gateway/stream_consumer.py buffers via queue + drains async at 50 ms intervals; flood-control strikes degrade to single final send | drop-or-buffer instead of blocking the stream worker (FrameRing already exists on the supervisor side; the direct `serve --stdio`+ws path lacks it) | M | P3 |

Already at parity / deliberately fine: token auth with
`hmac.compare_digest`; single-active-client gate; seq/rejoin FrameRing
replay; PTY group-kill ladder with the documented macOS EPERM fallback (this
file is *ahead* of hermes' pty handling in care); zombie-aware
`daemon_alive`; idle backoff for permanent model errors.

## 7. messaging/

| # | Item | Hermes | LunaMoth today | Effort | Pri |
|---|------|--------|----------------|--------|-----|
| 30 | **Inbound dedup (TTL cache keyed on platform message id)** — WeCom retries callbacks that aren't answered (restart mid-callback ⇒ redelivery), OneBot/NapCat redelivers after reconnect; the same operator message then runs two full LLM turns | `gateway/platforms/helpers.py` MessageDeduplicator (300 s TTL, 2000 entries); scar in slack.py: "Socket Mode can redeliver events after reconnects (#4777)" | `parse_message_xml` keeps `MsgId` in `raw` but nobody checks it; qq.py has no echo/event dedup either | S | P2 |
| 31 | **Outbound send error containment + one bounded retry** — any non-`DeliveryDeferred` exception from `adapter.send()` propagates through `tick()`/`run()` and **crashes the whole gateway process** (then a 60 s supervisor backoff drops everything in between). A transient socket error on *send* should not kill the inbox | `gateway/stream_consumer.py:788-926` — failed sends retried once after 3 s, then degrade; 429 backoff doubling to 10 s cap, 3 strikes → stop streaming edits | wrap `_send` in a catch-all + single retry + log; only configuration errors should crash | S | **P1** |
| 32 | **QQ send-while-disconnected handling** — `_send_frame` raises if the socket is down (reconnect loop owns the socket); an unattended `speak` during a reconnect window crashes the gateway via item 31's path | platform adapters queue or fail-soft and report | buffer one outbound or convert to logged DeliveryDeferred until reconnect completes | S | P2 |
| 33 | **Anti-loop output filter** for bot-reachable channels — drop "silence narration" tokens (`*(silent)*`, bare `.`, 🔇) before delivery | scar `gateway/delivery.py:329-337`: "In bot-to-bot channels these tokens mirror back and forth until a model crashes" | nothing; low exposure today (private-chat-only adapters) but cheap and the failure mode is ugly | S | P3 |

---

## Explicitly skipped (and why)

- **Fallback model chains / credential pools / model cost guards**
  (`chat_completion_helpers.py:1076-1201`, `hermes_cli/fallback_*`) — the
  no-fallback policy excludes them by design. (Noting one transferable scar
  anyway: their pool-contamination bug (:1192-1201) is the generic lesson
  "never mutate shared config from a per-request recovery path".)
- **Static fallback summaries** (`context_compressor.py:1017-1204`) —
  fabricated output; LunaMoth's trim backstop is the agreed degradation.
- **Multi-provider native adapters** (Anthropic/Bedrock/Gemini/Codex
  transports, `agent/*_adapter.py`) — LunaMoth is OpenAI-compatible-only on
  purpose; the unified-reasoning gating already imported is the right slice.
- **Anthropic prompt-cache `cache_control`** — N/A on the wire LunaMoth
  speaks; the three-zone prefix already implements the discipline.
- **Compression locks (#34351), cross-session summary leaks (#38788)** —
  one-process-one-chara makes these structurally impossible.
- **Image/media pruning (#9434), vision/TTS/transcription hardening** —
  text-only runtime today.
- **Memory threat-pattern scanning, skills AST audit, skill provenance,
  write-approval / file_safety deny-lists** — philosophy mismatch: hermes
  guards a workhorse on the user's real home dir; LunaMoth's boundary is the
  OS jail, the toolpack allowlist and the audit log, and the chara's own
  memory/skills are trusted by design. (file_safety becomes relevant only if
  `dir` isolation plus broad writable_paths becomes a common deployment —
  revisit then.)
- **Background process registry + watch patterns**
  (`process_registry.py:62-404`, checkpoint recovery :1378-1491) — LunaMoth
  has no background-process tool yet; when the chara curriculum grows
  long-running work, port the registry wholesale (it is the single most
  scar-dense file in hermes). Listed as future-L, not in the 33.
- **Pairing codes / multi-user authz** — LunaMoth is single-operator with a
  static allowlist + token; hermes' pairing machinery solves a multi-tenant
  problem LunaMoth doesn't have.
- **Kanban/curator/insights/cron/voice/Windows** — out of scope features.

## Suggested order of attack

1. The five P1s that are silent hangs or data loss: **#19 (MCP unbounded
   block)**, **#14 (terminal timeout doesn't kill the group / can hang
   forever)**, **#23 (tool exception kills the turn)**, **#12 (trim eats the
   summary)**, **#31 (one bad send kills the messaging gateway)**.
2. The two P1s that are invisible bad turns: **#4 (silent empty
   completion)**, **#10 (compaction thrash = nightly budget burn)**, then
   **#1 (stall watchdog)** which completes the known-roadmap set.
3. P2 batch by file: llm.py (#2, #5, #7, #8, #9), runner.py (#15–17),
   compaction (#11, #13), gateway/guardrails (#24, #25), supervisor (#27),
   messaging (#30, #32).
4. P3s opportunistically (#3 parallel exec last among the roadmap four — it
   is a throughput feature, not a correctness fix, and serial execution is
   currently load-bearing for the audit trail's ordering).

---

## Structural root-causes — the recurring smell (diagnosis 2026-06-16)

A batch of real-trace bugs (`send_file` invisible; `execute_code` faked success;
`ERROR: None` on a started server; `resting` ate the first-meeting greeting; the
`workspace/workspace` double-path) were all symptoms of **two structural smells**,
not independent defects. Fixing them one-by-one is whack-a-mole; the durable fix
is to collapse the shared root. (Owner decision 2026-06-16: **document this round,
do not refactor** — the bugs themselves are patched. This is the simplification
backlog.)

**Smell A — one fact owned in several places that drift:**
- **Tool allowlist has FOUR owners**, the worst offender. `FULL_TOOL_ACCESS`
  (`core/state.py:22-34`) is a hand-kept third list beside `registry` and
  `pack.tools`; the migration at `state.py:80-82` *force-resets* `tool_access`
  back to it on every `load()` (so a tool missing from the list is not "forgotten"
  but **actively deleted each load**); and `execute_code._enabled_tools`
  (`tools/builtin/execute_code.py:346-352`) derives the sandbox's tool set from
  `state.tool_access` alone, bypassing `registry ∩ pack` entirely. This is the
  `send_file`-vanished root.
- **`isolation` derived on two paths**: foreground `ctx.run_terminal`
  (`tools/context.py:64-75`) never reads `state["isolation"]` (falls to
  `runner.backend()`); background `terminal._run_background`
  (`tools/builtin/terminal.py:221-224`) does — so fg/bg can run at different
  isolation. Plus `ISOLATION_TO_BACKEND` is mirrored in 3 files.
- **`execute_code` cwd set in two owners** (`run_terminal(workdir=)` + the
  command's own `cd`) — the double-cd (now patched by dropping `workdir`).
- **`mode`/autonomy double-written** to disk config + the live agent (`supervisor.py`).

**Smell B — distinct meanings collapsed into one flag / shape:**
- **Tool success/failure inferred from JSON shape** (`gateway._is_error_json`,
  `gateway.py:279-294`) instead of an explicit status — `{"error": null}` read as
  failure (patched, but the *judge-by-shape* root remains; five different
  result shapes flow through it). `execute_code` status is `"success"` unless a
  substring match — and the matched literal `"[runner: timeout after"` doesn't
  even match runner's actual `"[timed out after"` (`runner.py:287`); a non-zero
  script exit still reports success because the exit code is never returned.
- **`attach` decision** (`protocol/api.py:149-237`) chains four orthogonal facts
  (`present`/`_greeted`/`resting`/`first_meeting`) as sequential short-circuits;
  `_greeted` (process) and `presence.met` (disk) are two owners of "have we met".
- **`LifeState.state`** single string carries 6 meanings with an overloaded
  `detail` field (`supervisor.py:383-460`).

**The simplification plan (prioritized):**
- **P0 (low-risk, no protocol/cache/card impact):**
  - *Isolation single-source*: `ctx.run_terminal` passes `isolation=ctx.isolation()`;
    `terminal._run_background` uses the `ctx` accessors; extract the one
    `ISOLATION_TO_BACKEND` map. (`context.py`, `terminal.py`, `runner.py`)
  - *Allowlist stop-the-bleed*: delete the `state.py:80-82` force-reset and make
    `_effective` treat `state.tool_access` as a soft narrowing of `registry ∩ pack`
    (missing ⇒ not narrowed), so a new tool can't be silently deleted.
- **P1 (medium-risk, high-value):**
  - *Explicit tool status*: `tool_error` writes a namespaced `{"__tool_error__": msg}`;
    gateway judges on that key, not a scan for `"error"`; `runner`/`execute_code`
    return structured status (real exit code), not parsed text. Migrate in two
    steps (recognize both, write the new) so replayed transcripts stay valid.
  - *attach decision table*: evaluate the four facts then a pure
    `_decide_opening(present, greeted, resting, first)` — unit-testable, which this
    bug family has always lacked. Pick `presence.met` as the single authority.
- **P2 (needs owner sign-off — touches default capability / Settings schema /
  card semantics):** allowlist white→deny-list inversion (default-open, same
  philosophy as network-on-by-default); `patience` dropping its companion
  `patience_override` bool; card `wishes` re-seeding on edit; `LifeState` struct.

Sharpest framing: the shared root is that **`tool_access`/`isolation` are modeled
as "raw-loaded in many places, each with its own default, with a migration that
rewrites them"**, and **tool success is modeled as "no explicit status, guess from
JSON shape"**. Collapse those two (single derived source + explicit status) and at
least six known defects lose their common root instead of being patched one at a time.

---

# Part 2 — Deferred product ideas (worth considering)

Salvaged from the deleted desktop design doc, the webui needs register, and the
hermes-desktop study — product directions deliberately deferred. These overlap
with `CLAUDE.md`'s roadmap (card market, remote access, the chara curriculum);
that roadmap is the source of truth for *direction*, this is the concrete UI/
product backlog behind it.

- **Menu-bar resident** *(the designer's "most wanted")* — a mac menu-bar moth
  icon; close the window and the chara stays alive; a badge = a chara is waiting
  for you; click = a mini board. The ultimate "lives in your computer" form.
  The Electron shell shipped; this is the next shell step.
- **Card-defined custom life-state words** — let a card override the displayed
  `life.state` word (a statue's "resting" could read "weathering"), via
  `extensions.lunamoth`. The engine keeps factual defaults; the card customizes.
  (Engine-side stance/flavor text was already stripped — only factual state
  words remain, so this is purely a card-override hook.)
- **Artifacts backtrace** — file → the tool call / session message that produced
  it; inline "work cards" in the chat stream. Needs a backend file↔tool-call
  mapping (today works live only on the drawer shelf).
- **Let the chara paint its own portrait** — when the avatar slot is left empty
  at creation, the chara's first goal can be "paint myself a portrait." An
  artist's rite of passage that also solves the asset problem.
- **Weekly digest** — a quiet weekly summary (from existing goals + transcript)
  instead of an infinite feed.
- **Notifications & quiet hours** — waiting-for-you → system notification;
  respect night DND (a slot is already reserved in Settings · General).
- **Remote VPS residence** — over the existing `serve` WS+token: the desktop
  connects to a backend on an always-on server; the chara lives there, the
  desktop is just a window. (Same as the roadmap's remote-TUI-client item.)
- **Card / pack marketplace** — one-click package (card + embedded world book) +
  a shareable index. (Same as the roadmap's card-market item.)
- **Multi-chara visiting** — charas on one machine visiting each other; the
  `say|muse` protocol already supports multiple audiences. Far-future.
- **Voice (STT/TTS)** — hermes has the full chain to port; a big "aliveness"
  boost, but only after the core stabilizes.
- **Multimodal** — detected and shown as "not enabled this version"; the skeleton
  is left in place for it.
- **Panel polish leftovers** — memory entry-level editing and goal checkbox
  editing in the chat drawer are still read-only; a board-level context ring +
  ⚡ high-load chip needs `serve` to expose a lightweight last-activity / resource
  sample.
- **In-character closer for tool-less cards** *(low priority)* — the post-history
  closer (`content/rules.py:_CLOSER`) carries two reminders now: stay-in-character
  AND make-real-things. But the whole slot is tool-gated (`agent.py:_post_history_slot`
  returns "" with no tools, asserted by `test_rules.py:62`), so a pure-roleplay
  card with no tools gets the in-character anchor only at the TOP (render_system),
  never at the closer. A pure-roleplay tavern card arguably needs the closing
  in-character nudge most. Fix later: split the closer so the in-character half
  fires even tool-less while the no-fabrication half stays tool-gated (adjust the
  gate + `test_rules.py:62`). Deferred — tool-less pure-roleplay is not a current
  focus.
- **Self-contained desktop app (signed DMG / AppImage)** — the consumer install
  should be "drag LunaMoth.app to /Applications, double-click, it works" — not
  the `curl|bash` CLI install (that stays the dev/terminal path). The hard part:
  `apps/desktop` is a THIN Electron shell that spawns the Python backend
  (`lunamoth desktop`); today `electron-builder` (`npm run dist`) bundles only
  the shell, and `main.cjs` finds the backend via a dev checkout or a (currently
  mismatched) installed path — so the DMG today is NOT self-contained and shows
  "No backend found". Plan:
  1. **Freeze the backend** — PyInstaller/py2app into a standalone `lunamoth`
     binary, OR ship a uv-managed standalone Python + the venv as a folder.
     Decision point: PyInstaller (one binary, smaller) vs bundled-venv (simpler,
     larger). KEY constraint: the supervisor RE-INVOKES the backend as
     subprocesses (`lunamoth serve NAME --stdio` per chara) — the frozen binary
     must support re-exec, and the spawn command must point at the bundled
     binary (not `python -m lunamoth…`). The OS-jail isolation (`sandbox-exec`
     on macOS, the `isolation.py` argv builders) must also work from inside the
     .app bundle.
  2. **Bundle it** — electron-builder `extraResources` puts the frozen backend
     in the .app; `main.cjs`, when `app.isPackaged`, spawns
     `process.resourcesPath/backend/…` instead of the dev/installed discovery.
  3. **Sign + notarize** (Apple Developer ID) — otherwise Gatekeeper blocks the
     .app on any other Mac. Linux: AppImage has the same bundling shape, no
     signing.
  - **Bug to fix regardless** (independent of the DMG work): `main.cjs`
    `installedLauncher()` looks for `~/.lunamoth/bin/lunamoth`, but `install.sh`
    puts the shim at `~/.local/bin/lunamoth` (and `~/.lunamoth/bin/` holds only
    `uv`). So even today the Electron app can't find a `curl|bash`-installed
    backend — fix the path (check `~/.local/bin/lunamoth` too).
  - Icon assets already exist (`apps/desktop/assets/icon.png` + the menu-bar
    `trayTemplate*`). The menu-bar-resident idea (above) composes with this.

---

# Part 3 — Active loop backlog (owner requests, 2026-06-16)

Managed by the `/loop` dev cycle: each iteration picks the top OPEN item, writes
its plan + acceptance here, implements (parallel subagents where independent),
runs tests, has an **audit subagent** verify against the acceptance bar (and
parity with `reference/hermes-agent` for commodity surfaces), confirms
**functionality with a live Quinn** (wake → self-check tools → read its jsonl →
delete), then **removes the item from this list**. Anyone (incl. subagents) may
add a diagnosed problem here with a priority. Independent items run in parallel.

## R5 (P2) — Better card preview (multi-page, ideally editable)
Game-style multi-page card view: 设定 / 视觉(立绘+主视觉) / 表情 / world. Editable.

## R6 (P3) — Blank card → auto-generate a visual set via the image key (opt-in)
Well-designed interaction; depends on R4.


## R7-followup (LOW, from the R7 security audit) — V4A Move header polish
`file_tools._V4A_HEADER_RE` matches Update|Add|Delete but not Move, so a V4A
`Move File: src -> dst` skips both the friendly assets read-only message and the
`..`-traversal pre-guard. NOT a confinement hole (move_file's write-mode resolver
still refuses assets/escape destinations — verified by the audit), only a less
helpful error. Add Move to the regex (and check its destination) for consistency.

DONE this loop: R1 tool-access single-source (4435d77), R2 on-disk image vision
(f34a55e), R3 tool-call fold i18n (e4dcce4), R7 sandbox geography (assets read-only
sibling / workspace / works/ — full suite green, security-audited, live-Quinn verified),
R8 new-user character-select carousel + R8b deck filter (未唤醒/已唤醒 toggle + colorful
✨默认 carousel entry; 8 built-ins, 2 swipeable pages, authored bilingual copy/tags in
front/web/builtins.js; selecting routes to the existing wake flow; deck splits editable
OCs from read-only living charas),
R4 agent self-image-generation (generate_image tool → Volcano Ark Seedream, gated on
an image key via check_fn so no key = no tool = no spend; saves to workspace/works via
the R7 write path; image-signature-validated, size-capped, no-fallback; security-audited;
live-Quinn generated + sent one real 2048² image).
