# LunaMoth — open work

This is the ONE doc under `docs/` (owner rule, re-affirmed 2026-06-17: everything
condenses here; settled plans/specs/build-logs get deleted once their conclusions
live in `CLAUDE.md`, the code, and git history). What's kept is only what's still
*open* or worth remembering:

- **Part 1** — engineering hardening backlog (hermes-parity).
- **Part 2** — deferred product ideas.
- **Part 3** — active `/loop` backlog (owner requests).
- **Part 4** — 2026-06-17 test-feedback triage (what's open from it).
- **Appendix A** — client + deploy architecture reference (the only durable bits
  of the now-deleted CLIENT-AND-DEPLOY-PLAN; the runbook proper is README + `deploy/`).

> **Verify before starting:** this is a 2026-06-13 read-only audit; some rows have
> since LANDED and been deleted (e.g. #27 chara auto-restart `RestartBackoff`, #30
> inbound `MessageDeduplicator`, #31 outbound containment `DeliveryDeferred`). The
> gateway also moved in-process (`server/messaging_host.py` shares the chara's one
> agent). Re-check each remaining row against current code before picking it up.

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
| 32 | **QQ send-while-disconnected handling** — `_send_frame` raises if the socket is down (reconnect loop owns the socket); an unattended `speak` during a reconnect window can crash the gateway (the generic outbound containment from the now-landed #31 may already cover it — verify against `qq.py`) | platform adapters queue or fail-soft and report | buffer one outbound or convert to logged DeliveryDeferred until reconnect completes | S | P2 |
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

1. The P1s that are silent hangs or data loss: **#19 (MCP unbounded
   block)**, **#14 (terminal timeout doesn't kill the group / can hang
   forever)**, **#23 (tool exception kills the turn)**, **#12 (trim eats the
   summary)**.
2. The two P1s that are invisible bad turns: **#4 (silent empty
   completion)**, **#10 (compaction thrash = nightly budget burn)**, then
   **#1 (stall watchdog)** which completes the known-roadmap set.
3. P2 batch by file: llm.py (#2, #5, #7, #8, #9), runner.py (#15–17),
   compaction (#11, #13), gateway/guardrails (#24, #25), messaging (#32).
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

## SEC-low (from the 2026-06-16 security review) — image-gen blocking + key-on-disk readability
- generate_image is synchronous: ark_generate 240s×5 + download 120s×5 can freeze the chara
  for minutes on a flapping endpoint. Tune retries/timeouts down for image-gen.
- macOS sandbox profile allows file-read* globally (documented: confine writes, not reads),
  so the chara's own terminal could `cat` its session config.json and read its provider
  api_key. Consider tightening reads, or not storing the key where the jailed shell reads it.

### ✅ FIXED 2026-06-17 — terminal isolation ladder (Landlock); was: terminal reads the whole container

Confirmed on the live box (Ubuntu 22.04 host, kernel 5.15, Docker, `no-new-privileges`):
- The container **cannot create user namespaces** (`unshare --user` → EPERM; Docker's seccomp +
  no-new-privileges block nested userns — even though the HOST allows them: `unprivileged_userns_clone=1`,
  `unshare --user` OK on the host). So **bwrap cannot run in-container**, and `runner.py:251-256`
  silently **degrades the `terminal` tool to directory trust** (runs as root, only a `[jail unavailable]` note).
- Proved: a chara's `terminal` read `/root/.lunamoth/desktop.json` (the global LLM key) and `auth.json`
  (the login PBKDF2 hash), and can read `/proc/1/environ`. So a chara — or a logged-in user who drives a
  chara to run a command — can exfiltrate the instance's key/secret, **bypassing the web layer's "key value
  never travels"** (`hub.list_keys` only returns `has_key`). The container boundary still protects the host
  and other tenants; exposure is limited to *this* instance's own secrets. **Owner stance (2026-06-17):
  acceptable for now — a Docker instance holds nothing else sensitive — but track for a better fix.**
- NOT the OS version's fault: the host fully supports userns; the block is Docker's hardened defaults.
  Upgrading Ubuntu (→24.04) would make it WORSE (`kernel.apparmor_restrict_unprivileged_userns=1`).
  "Fix" by relaxing the container (`seccomp=unconfined` / drop no-new-privileges) = weakening the OUTER
  jail to enable a redundant inner one → rejected.

**Researched fix — an isolation LADDER (keeps macOS↔Linux parity; serves BOTH Docker and system installs):**
1. **macOS** → `sandbox-exec`/Seatbelt (already used). Also tighten its global `file-read*` so the same
   secret-read is closed on macOS too (the original SEC-low bullet above).
2. **Linux + userns** (bare-metal / `lunamoth desktop` on host via install.sh) → **bwrap** as today
   (full path jail + network gating; chara sees only workspace+assets). Strongest; unchanged.
3. **Linux, no userns** (Docker) → **Landlock LSM** (ABI v1, kernel ≥5.13). **VERIFIED to work inside this
   very container**: `landlock_create_ruleset(NULL,0,1)` → ABI 1, no EPERM (Docker's default seccomp permits
   the landlock syscalls; host LSM list includes `landlock`). An unprivileged process confines its OWN fs
   access to an allow-list (workspace rw + assets ro + the system paths bwrap binds, incl. procfs) — no
   namespaces, no root. Closest namespace-free match to bwrap's semantics. Implementation: ~60 lines of
   ctypes (`landlock_create_ruleset`/`add_rule`/`restrict_self`, syscalls 444/445/446) applied in the
   terminal/exec child before exec; no new deps. (hermes/Codex don't hand-roll this — they lean on Codex's
   own Seatbelt/Landlock or Modal containers — so it's ours to build.)
4. **uid-drop** (`setpriv` to a dedicated low-priv user, per-chara; workspace chown'd to it, secrets root 600)
   as a complementary/last fs layer when even Landlock is unavailable; otherwise **refuse to run** — honour
   `isolation.py`'s "never degrade to directory trust" for the `terminal` tool too (today only
   `interactive_shell_argv` honours it; `runner.run` does not).

**✅ SHIPPED 2026-06-17** (`session/landlock.py` + `session/isolation.py` + `tools/runner.py`,
`tests/test_landlock_isolation.py`; subagent-reviewed, no CRITICAL/HIGH). Implemented tiers 1-4 above:
macOS read-deny of the home, bwrap unchanged, the Landlock tier (ctypes ABI v1), and refuse-not-degrade.
LIVE-verified on `chat.lunamoth.ai` (kernel 5.15, Docker): `bwrap:False landlock:True`; a chara `terminal`
runs normal commands but `cat ~/.lunamoth/desktop.json`, `auth.json`, `/proc/1/environ`, and `ls ~/.lunamoth`
all return Permission denied. Full suite 891 passed. Servers now recommended to run system-level (bwrap);
Docker supported via Landlock (README + CLAUDE.md updated).

Remaining LOW follow-ups (clarity/ergonomics, NOT jail escape — from the 2026-06-17 review):
- The Landlock tier grants no `/proc` (deliberate — `/proc/1/environ` leaks the supervisor token), so
  `/proc`-dependent tools (`ps`, some interpreters) fail with a bare EACCES under Docker. Consider a clearer
  message, or a procfs-hidepid mount if ever feasible.
- `interactive_shell_argv`'s Landlock fallback doesn't surface "network not gated (ABI v1)" to the operator
  PTY the way `runner.run_terminal` does — add the one-line notice for parity with the "fail visibly" contract.
- Network gating under Landlock needs ABI v4 (kernel 6.7); until then `/net off` is fs-only under that tier.

## R5-followup (LOW) — card-view art editing + richer world/expressions
R5 shipped the multi-page card view (display + 设定/世界 editing). Deferred:
per-asset upload for 立绘/主视觉/背景 + stickers (need upload RPCs like avatar_upload);
a labeled-expression data model (`assets.stickers` → `[{label,file}]`, back-compat with
bare strings) so 表情 becomes a named set; and the per-entry world editor for EDITABLE
cards (read-only cards already show per-entry cards; editable still uses the text editor).

## R6 (P3) — Blank card → auto-generate a visual set via the image key (opt-in)
Well-designed interaction, now fully UNBLOCKED (R9 in-app visuals pipeline + R4
generate_image both landed). Reuse `visuals/pipeline.py` + the brief approach to
fill a blank card's 立绘/主视觉/头像/背景 set — essentially the "auto-fill a blank
card" entry point into the existing visuals pipeline. Spends real money; opt-in/cost UX.

---

# Part 4 — 2026-06-17 test-feedback triage (open items only)

Owner tested the 2026-06-16 build. Open items from that triage (the small settled
fixes are on main and not retained here):

## Live verification (visuals + messaging) — needs real credentials/a host
The visuals pipeline + global keys + matte all shipped but were never exercised
end-to-end: needs a real ARK image key (full card visual-set generation) and a
downloaded matte model (the cutout path). Same shape as the WeChat/QQ messaging
live-test (roadmap C.3). Budget one verification round with real keys.

## (1) write_file ~12KB truncation — RESEARCH DONE, decision pending
Not a tool cap: `write_file` has no size limit. The truncation is the model's
COMPLETION token cap — `core/llm.py:_max_tokens_param()` feeds `cfg.max_tokens`
(`config.py` default `LLM_MAX_TOKENS=4096`), and tool-call ARGUMENTS count in that
budget; ~4096 tok ≈ 12KB, so the model's `write_file` content gets cut mid-arg and
it splits the file. Industry pattern (hermes #26425, openclaw #63210): this is a
silent `stop_reason=="length"` truncation in `tool_use`. Proposed fix (S/P1-ish):
① raise the `LLM_MAX_TOKENS` default; ② check `finish_reason`/`stop_reason` each
response, surface `length` as a visible error (matches "no failure fallbacks")
instead of feeding a truncated arg to the tool; ③ steer large writes to chunked
append/patch. DECISION NEEDED: default value + whether to add truncation detection.

## (8) both web tools return empty — RESEARCH DONE, decision pending
NOT a missing User-Agent (`web.py:_http_get` sets one). `_search_duckduckgo()`
scrapes `html.duckduckgo.com/html/`, which 403s / empties on bot detection. Two
paths: LIGHT (switch to `lite.duckduckgo.com/lite` + retry/backoff — fast, still
fragile) or a real SEARCH API backend (Brave/SerpAPI; `_resolve_search_backend()`
is already pluggable — robust, needs a key). Pairs with the messaging live-test
round (roadmap C.3). DECISION NEEDED: light fix vs API backend.

## (2)(5)(6)(7) — deferred to the UI/feel refactor
- **(2) send_file UX**: file cards don't re-render on chat reopen; unclear where a
  download lands; html should open in the browser; other files should open in the
  sandbox Finder. (React file-card render + open-with.)
- **(5) interrupt / insert-message feel**: jank when interrupting or injecting a
  message mid-stream. (Streaming preemption semantics.)
- **(6) tool-call compression**: fold consecutive tool calls with no assistant text
  into one group + one reasoning. NOTE: the React `streamModel.ts` already folds
  tool-groups — owner tested the OLD client; RE-TEST on the new SPA before doing work.
- **(7) silent after tool calls**: chara sometimes ends a turn without speaking.
  (Curriculum / prompt steering toward a closing `speak`.)

## Retired-clarify follow-up (from item 9)
The clarify TOOL is gone, but its generic interactive-question plumbing remains
dormant (codec `clarify_ask`/`clarify_reply`, dispatch round-trip, terminal stdin
hook, ~7 React files; mirrors `permission_hook`). Fully excising it is a
protocol-first change (constitution codec + React client) that wants owner sign-off.

## Shelved — presence enter/leave markers
Owner shelved (2026-06-17) the idea of deleting the injected `[operator entered]`/
`[operator left]` markers. Finding for when it's revisited: the marker TEXT +
`presence.marker_text` + `on_attach`/`on_detach` hooks are removable (presence
already rides the volatile tail as `user_present`; the injected lines are redundant
and permanently pollute the transcript), but the attach/detach STATE driving
conversation↔self-work is load-bearing and stays. Meaningful simplification, not a
full module collapse. Lone cost: losing the "just changed this turn" signal in
history (the timestamp/gap approximates it).

## Frontend refactor pass (2026-06-17) — done, and two deferred-with-rationale

Grounded in a React-philosophy + frontend-design audit (server-state vs UI-state,
derive-don't-sync, composition-over-config). DONE + pushed: the CRITICAL dead
avatar-gen pipeline removal; 4 streaming-core bugs (dead unread/superReadTs,
non-deterministic restored ids, swallowed rejoin gap, stale super-chat watermark);
card serialization unified onto the tested serializeCardFields (undefined=preserve
contract); DeckModal reuse + hub refresh() lost-update + assertNever exhaustiveness;
HubContext split (stable useHubApi vs changing useHubState — pushes no longer
re-render the tree) + typed wire-boundary decoders (killed `p as LifeSnapshot`).

DEFERRED (considered, judged net-negative TODAY — revisit with test scaffolding):
- **useCharaStream controller extraction** — split the 100-line connect/attach
  effect (client construct + 9 callback wirings + async attach + timers) into a
  controller the hook thinly wraps. Pure readability in the MOST delicate code
  (streaming lifecycle), and the web side has near-zero coverage of this hook — a
  rewrite needs `app.run_test()`-style pilot coverage FIRST. Also fixes the
  timers-created-inside-the-async-IIFE cleanup race the audit flagged.
- **CardContentForm spine** — one field-spec driving CardEditor / WakeSheet /
  CreateFlow. Declined: `CardBlock` is ALREADY the shared row primitive, and the
  three flows (tabbed editor / 2-step wake / section-chain create) differ enough
  that one spec-driven form needs per-flow flags (which fields, editable cond,
  AI-rewrite, initial source) = config-explosion. The error-prone part
  (serialization) is already unified; the remaining dup is just JSX layout.

Other audit findings still open (lower value): useAsync/useBusySet hooks to fold
the repeated alive-flag load + Set busy-tracker (~10 files); per-pane file split of
ChatPanel's 6 bundled panes; React.memo on the markdown items (measure first).

## UX/design pass (2026-06-17, second wave) — multi-reviewer audit + live screenshots

Grounded in a 4-lens reviewer panel (visual harmony / interaction feedback /
onboarding / safety) + the design skills (frontend-design, design-md,
popular-web-designs), VERIFIED with a live screenshot harness (real desktop server
+ Chromium driving the hash routes — at /tmp/lm-shots, server on :8780).

DONE + pushed:
- Safety: card SOFT-DELETE + Undo (backend `.trash/` + `card.restore`, traversal-
  guarded; deckToastAction); dirty-guards on CardEditor/WakeSheet/CreateFlow (no
  lost edits on stray Esc/backdrop); per-chara composer draft persistence; confirm
  on visual-asset delete.
- Interaction: optimistic net switch (was a dead+silent click); NumField feedback/
  revert; `/reset` working-state; ChatWorks swallowed errors → toasts.
- Visual: board/deck empty states CENTERED (killed the top-heavy void — the biggest
  visible harmony issue; only findable by rendering); FirstRun close affordance;
  filled the empty second-CTA subtitle.
- Onboarding: the model-gate ELECTION → an in-flow ModelGate overlay that resumes
  intent (no more eject-to-Settings); de-tavern'd the import framing (our format is
  ST-compatible — reframed the label + made create-flow tavern-absorb a natural line).

DONE (second wave, pushed): onboarding delight — image-gen INVITE (VisualEditor
reads has_image_key, shows a "give them a face? → Settings·生图" banner + gates
generate, instead of a raw error) and a warm CHAT LANDING ("Say hello to {name} ✦"
on a fresh wake, not a blank stream). Token pass CORE — the field family unified
onto --field-bg (killed the raw #FAFBFC across 3 input rules), the scale-token layer
added to :root (--r-sm/--r-lg/--r-pill/--field-radius/--control-h/--fw-*), and the
three dark-parity badges fixed. Screenshot-verified light+dark.

STILL OPEN — the token pass LONG TAIL (do per-surface with screenshot checks, NOT
blind-bulk — the rendered look is good and must be preserved):
- collapse the 26 font-sizes onto a ~7-step --fs-* scale (six are .5px); 8 weights → 4.
- migrate the 20 hardcoded radii onto --r-sm/--radius/--r-lg (esp. the 5 "card" radii
  13/14/15 → --r-lg).
- one `.selectable` base for the 5 near-identical tile pickers (iso-seg / pack-option
  / emb-option / provider / gw-plats) — the wake sheet stacks 3 of them slightly off.
- give .btn + the field base a shared --control-h so rows stop wobbling.
- replace the ~24 inline style={{marginTop:N}} literals with rhythm utilities.

---

# Appendix A — client + deploy architecture reference

The full build plan (CLIENT-AND-DEPLOY-PLAN) shipped 2026-06-16 and was deleted;
the operational runbook proper lives in **README (EN/zh)** + **`deploy/`**
(Dockerfile, compose.yml, entrypoint.sh) + `install.sh`. The durable
architecture/rationale worth keeping (change only with owner sign-off):

- **Stack**: React 19 + Vite + TypeScript SPA; **Context + hooks**, no Redux/MobX;
  **hash routing** (so the supervisor's static handler needs no SPA-fallback list,
  and `file://` would work). Source at repo-root `apps/web/`.
- **Distribution = a wheel that bundles the built frontend** (hermes model). `apps/web`
  builds to `src/lunamoth/front/webui/` (GITIGNORED, not committed); setuptools
  package-data (`lunamoth=["front/webui/**/*"]`) packs it into the wheel at CI
  packaging time → users `uv tool install` and get the prebuilt UI, no node at install.
  `vite.config.ts`: `base:'./'`, `outDir:'../../src/lunamoth/front/webui'`, `emptyOutDir`.
- **Electron stays a thin local shell**, unchanged, always pointing at the local
  supervisor's HTTP URL. Remote = browser, never Electron.
- **Remote, two ways**: (a) SSH tunnel to the loopback-bound supervisor (`lunamoth
  connect ssh://user@host` opens `ssh -L` after reading the remote daemon token/ports);
  (b) supervisor bound to a real host behind a TLS reverse proxy (Caddy / cloudflared).
- **Auth**: ONE `lm_auth` SameSite cookie minted by a `?token=` handshake gates GET /
  `/asset` / `/rpc` / `/upload` / WS uniformly (401); Origin/Host allowlist; optional
  PBKDF2 password login layered on top; "no server token ⇒ open (dev/loopback)".
  Lives in `supervisor.py` + `netsec.py`.
- **One-click deploy = Docker**: `python:3.12-slim` + `pip install` the release wheel
  (carries `webui/`, so no node); `compose.yml` with `restart: always`,
  `no-new-privileges`, a `~/.lunamoth` volume for sessions/cards/config.
- **Two known non-ideal-but-shipped choices** (future upgrade path): the supervisor
  runs TWO server stacks on TWO ports (stdlib http.server + websockets, WS = http+1
  for non-loopback) — single-port ASGI (Starlette/uvicorn) is the eventual cleanup;
  it's the backbone of the deferred UI/feel refactor loop.

# Loop 2026-06-18 — recurring-bug + UX sweep (owner /loop, in progress)

Owner blessed free refactoring; no back-compat with old cards/tavern/contexts.

- **[DONE] First-message swallow — permanent root fix.** Was a dual-authority desync
  (durable presence `met` committed before the transcript greeting row). Fix: the
  TRANSCRIPT is the single authority — `attach()` shows card `first_mes` iff the epoch
  is empty and persists it FIRST; `/reset` re-seeds the greeting into the fresh epoch
  at the command level (survives a live chara self-working before reopen). (api.py,
  commands.py, tests/test_presence.py)
- **[DONE] Delete all operator enter/leave (presence) logic.** Model context+behavior
  now independent of attach/detach. Kept mode=live|chat, muse/say, rest/patience, the
  speech-driven quiet timer. (presence/, core/state, core/agent, core/rules, protocol/api,
  dispatch, supervisor, messaging)
- **[DONE] StateSnapshot now carries the active chara's avatar/sprite/bg/keyvisual.**
- **[DONE] #1a Frontend: render the active chara's avatar/visuals in the chat view.**
  useCharaStream exposes the snapshot's avatar_uri/sprite_url/bg_url/keyvisual_url;
  Chat header + empty-state + per-message Avatar render avatar_uri (glyph only as
  fallback); the chara background uses the already-ported-but-unwired .chat-bg/
  .chat-veil/.chat-sprite layers via assetUrl(); + a mobile right-panel drawer.
- **[TODO] #1b in-session bg/sprite (立绘) EDIT control** (a ChatPanel tab calling
  card.save_asset / card.asset_delete / card.visual_generate against the active
  chara's frozen session card path) — RENDER done, EDIT control still to build.
- **[TODO] #3 Image assets: compress quality + progressive load** (compressed first).
- **[TODO] #4 Matte models: let the user install BiRefNet's two models from web/electron;
  delete the other two; shareable across instances; default to the stronger one.**
- **[TODO] #5 Unify key management into ONE settings surface** (multiple text keys +
  multiple image keys from multiple sources); kill the two-tab split + inconsistent
  input control styles.
- **[TODO] #6 The empty rightmost column on web looks abrupt — fill or remove it.**
- **[TODO] Mobile: make the web app responsive (mobile-first for the new UI work above).**
- **[DONE] Deleted per-chara `docker` isolation; `dir`→`admin`.** Two modes now:
  `sandbox` (bwrap→Landlock→refuse ladder, untouched) + `admin` (no jail, full-machine
  r/w, same workspace). Legacy `dir`/`local`/`docker` normalize to `admin` at every read
  site. (isolation.py, runner.py, _process_registry.py, sessions.py, cli.py, state.py,
  hub.py, supervisor.py + tests)
- **[TODO] background-process sandbox degrade (MEDIUM, pre-existing).** `_process_registry`
  silently degrades `sandbox`→directory-trust on a no-bwrap host and never tries Landlock,
  diverging from `runner.run_terminal`'s native→Landlock→refuse ladder. Align them (factor
  the ladder into one helper both call). Latent on the bwrap-equipped server; bites a
  no-userns deploy.
- Review LOW/MEDIUM from the keystone review: `_card_visuals` re-reads the card from
  disk instead of reusing the in-memory `character` (cached, harmless); bundled cards
  still carry inert `on_attach`/`on_detach` keys (cleanup).
- Discipline: nothing env-specific or personal (server IPs, deploy domains) in the
  repo; the per-host management tool stays server-only.
