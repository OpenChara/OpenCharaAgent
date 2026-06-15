# Spec: `terminal` + `process` tools — faithful re-implementation against LunaMoth

A from-source re-implementation spec for hermes-agent's two background-process tools, written so a LunaMoth engineer can rebuild them apple-to-apple on top of the existing `src/lunamoth/tools/runner.py`. Every behavior cites the source line. Infra-coupled bits (VM/SSH/docker/modal backends) are flagged "→ LunaMoth maps to isolation".

Sources:
- `reference/hermes-agent/tools/terminal_tool.py` (2679 lines)
- `reference/hermes-agent/tools/process_registry.py` (1622 lines)
- LunaMoth target: `src/lunamoth/tools/runner.py`, wired at `src/lunamoth/tools/gateway.py:232-241`

---

## PART 1 — `terminal` tool

### 1.1 JSON schema (terminal_tool.py:2612-2654)

```
name: "terminal"
description: TERMINAL_TOOL_DESCRIPTION  (terminal_tool.py:832-852 — the LLM-facing usage prose)
parameters (type object):
  command           string   REQUIRED        "The command to execute on the VM"
  background        boolean  default False    run in process registry (see 1.4)
  timeout           integer  minimum 1        max seconds; default 180 from TERMINAL_TIMEOUT; FOREGROUND clamp (1.3)
  workdir           string   (optional)       absolute per-command cwd; defaults to session cwd
  pty               boolean  default False     pseudo-terminal for interactive CLIs (local/SSH only)
  notify_on_complete boolean default False    bg-only; one notification on exit; MUTEX with watch_patterns
  watch_patterns    array<string>            bg-only; mid-process signal matcher; MUTEX with notify_on_complete
required: ["command"]
```

Notes on schema fields the *model never sees* but the function signature carries (terminal_tool.py:1817-1827, handler at 2657-2667):
- `task_id` — injected by the runtime via `kw.get("task_id")`, NOT a model param. Environment-isolation key. → **LunaMoth maps to its session: one process = one activated chara; task_id collapses to a single sandbox. Drop the multi-task registry entirely.**
- `force` — internal-only (skip dangerous-command guard after user confirm); never in the schema (comment at 1856).

`max_result_size_chars=100_000` is set at registration (terminal_tool.py:2677) — the registry-level hard cap on the returned tool result (distinct from the in-function 100K output truncation at 2401).

### 1.2 Parameter types / defaults / constraints / mutual exclusion

- **`command`**: must be `str`. Non-str is rejected up front with `{"output":"", "exit_code":-1, "error":"Invalid command: expected string, got <type>", "status":"error"}` (terminal_tool.py:1859-1869).
- **`timeout`**: `minimum: 1`. Effective timeout = `timeout or default_timeout` where default = `TERMINAL_TIMEOUT` env (180) (terminal_tool.py:1910-1911). Foreground clamp: if `not background and timeout and timeout > FOREGROUND_MAX_TIMEOUT` → **reject** (not clamp) with error nudging to background (terminal_tool.py:1915-1922). `FOREGROUND_MAX_TIMEOUT` = `TERMINAL_MAX_FOREGROUND_TIMEOUT` env, default **600** (terminal_tool.py:107-112).
  - **DIVERGENCE from LunaMoth**: hermes *rejects* an over-limit foreground timeout; LunaMoth runner.py:234-239 *clamps* into `[MIN_TIMEOUT=1, MAX_TIMEOUT=600]` and appends a `clamp_note`. The LunaMoth clamp behavior is the better fit (no failure fallbacks principle); keep it. For background there is no foreground cap.
- **`workdir`**: validated by `_validate_workdir` (terminal_tool.py:273-292) — allowlist regex `^[A-Za-z0-9/\\:_\-.~ +@=,]+$`; first offending char rejected with `status:"blocked"` (terminal_tool.py:2082-2092). → **LunaMoth runner.py:242-245 already resolves workdir but does NOT do the shell-injection allowlist. PORT THIS:** add `_validate_workdir` before the resolve, because LunaMoth's `dir` isolation runs with full user privileges.
- **`pty`**: local/SSH backends only. Auto-disabled for commands that need piped stdin (`gh auth login --with-token`, `_command_requires_pipe_stdin` terminal_tool.py:1668-1680); sets `pty_note` in result (terminal_tool.py:2095-2104). → infra-coupled to PTY spawn; **LunaMoth has no PTY in runner.py — defer pty support (supervisor.py owns the PTY shell). Mark as a follow-up, not a port.**
- **`notify_on_complete` ⊻ `watch_patterns`**: hard mutual exclusion. When `background and notify_on_complete and watch_patterns` both set → `watch_patterns` is dropped, `notify_on_complete` wins, a `watch_patterns_ignored` note is attached (`_resolve_notification_flag_conflict` terminal_tool.py:1766-1790; applied 2281-2288). Both are **no-ops unless `background=True`**.

### 1.3 Foreground execution path (terminal_tool.py:2325-2435)

1. **Guardrail before run** (foreground only): `_foreground_background_guidance` (terminal_tool.py:1729-1763) inspects the command (after `_strip_quotes` to avoid matching inside string literals) for shell-level backgrounding (`nohup`/`disown`/`setsid` via `_SHELL_LEVEL_BACKGROUND_RE`), inline/trailing `&`, or long-lived server patterns (`npm run dev`, `vite`, `uvicorn`, `python -m http.server`, etc. — `_LONG_LIVED_FOREGROUND_PATTERNS` 1706-1715). If matched → returns `status:"error"` telling the model to use `background=true` (2926-1934). Help/version invocations are exempt (`_looks_like_help_or_version_command` 1718-1726). → **Optional port for LunaMoth's chara-curriculum**: a soft nudge, not a hard block, fits the "neutral suggestions never orders" principle better. Recommend implementing as an advisory note appended to output, not a rejection.
2. **Execute with retry**: `env.execute(command, timeout=, cwd=)` wrapped in a `max_retries=3` loop with exponential backoff `2**retry_count` s on transient exceptions (terminal_tool.py:2331-2369). A `"timeout"` in the exception string short-circuits to `{exit_code:124, error:"Command timed out after Ns"}` (2344-2349). → **LunaMoth runner.py:262-296 already enforces the timeout itself via `subprocess.communicate(timeout=)` + `_kill_group`; it returns a structured `[timed out after Ns]` string, NOT exit 124.** Keep LunaMoth's in-process timeout — it's stronger (group kill + drain). The retry loop maps to LunaMoth's "retry 5s×5 transient connect errors" policy (CLAUDE.md) but that's an LLM-layer concern, not the runner.
3. **Timeout enforcement**: hermes delegates to `env.execute`'s own timeout (per-backend). → **LunaMoth maps to runner.py's `_kill_group` (SIGTERM→`_KILL_GRACE=1s`→SIGKILL to the process GROUP) + `_drain_nonblocking` (runner.py:144-212). LunaMoth's is the better implementation; reuse as-is.**
4. **Output processing pipeline** (ORDER MATTERS, terminal_tool.py:2372-2419):
   a. `output = result.get("output","")`, `returncode = result.get("returncode", 0)`.
   b. `_handle_sudo_failure` — gateway/messaging-only hint (2376). → SKIP for LunaMoth (no sudo prompt model).
   c. `transform_terminal_output` plugin hook, fail-open, first str wins (2382-2397). → SKIP (no plugin system).
   d. **Truncation** (2400-2410): `MAX_OUTPUT_CHARS = get_max_bytes()` (registry-configurable, default tracks the 100K cap). If `len(output) > MAX`, keep **40% head + 60% tail** with a middle marker `... [OUTPUT TRUNCATED - N chars omitted out of M total] ...`. → **LunaMoth runner.py:124-141 `truncate_middle` is the EXACT same 40/60 head-tail shape (cap `_OUTPUT_CAP=12000` for stdout, `_STDERR_CAP=2000` for stderr). Already ported. Keep LunaMoth's smaller caps — they're tuned for a chara's context window, not a dev workhorse.**
   e. **ANSI strip** AFTER truncation in hermes (2414-2415). → **LunaMoth runner.py:290-291 strips BEFORE truncating (deliberately — comment: so the cap budgets clean text and the cut can't land mid-escape-sequence). LunaMoth's order is BETTER; keep it.** Both share the same regex lineage (runner.py:56-91 is "ported from hermes tools/ansi_strip.py").
   f. `redact_sensitive_text` (2418-2419) — strips secrets leaked via `env`/`printenv`. → **LunaMoth has credential redaction in obs/log.py; consider adding redaction to terminal output too (gap — runner.py does NOT redact). Low-priority port.**
   g. **Exit-code annotation** `_interpret_exit_code` (terminal_tool.py:1604-1665): for non-zero exits, splits the command on `|| && | ;`, takes the LAST segment's base command (stripping `VAR=val` prefixes and `/usr/bin/` paths), and looks up a per-command table — `grep/egrep/fgrep/rg/ag/ack`:1="No matches found", `diff/colordiff`:1="Files differ", `find`:1="Some dirs inaccessible", `test`/`[`:1="Condition false", `curl`:{6,7,22,28}, `git`:1="often normal". Attached as `exit_code_meaning`. → **LunaMoth runner.py:94-121 `_exit_code_note` is a PORTED SUBSET** (grep/egrep/fgrep/rg/diff/cmp), gated to `returncode==1 and empty stderr`, taking the FIRST word (not last segment). **DIVERGENCE: hermes parses the last pipeline segment + has curl/find/test/git/cmp coverage LunaMoth lacks. Recommend widening LunaMoth's `_EXIT1_IS_INFO` table and switching to last-segment parsing for pipelines.**
5. **Return** (foreground success, 2425-2435): JSON `{"output", "exit_code", "error":None}` plus optional `approval` and `exit_code_meaning`. → **LunaMoth returns a flat string (`exit=N\nSTDOUT:...\nSTDERR:...`), not JSON.** This is a deliberate LunaMoth shape (runner.py:300-307). Keep the string shape — but ENSURE the clamp_note, exit_code_meaning, and partial-output-on-timeout are all present (they are).

### 1.4 Background handoff to the process registry (terminal_tool.py:2106-2324)

When `background=True`:
1. Resolve `session_key` (gateway approval context) and `effective_cwd` via `_resolve_command_cwd` (workdir > live `env.cwd` > config cwd; terminal_tool.py:1793-1814).
2. **Dispatch by backend**:
   - `env_type == "local"` → `process_registry.spawn_local(...)` (Popen + reader thread). → **LunaMoth's `dir` and `sandbox` isolation are both "runs on host with a jail wrapper" — they map to spawn_local, but the spawned argv must be the JAILED command (runner.py builds `_macos_jail`/`_linux_jail`/`_docker` cmd lists at runner.py:248-258). PORT NOTE: LunaMoth's spawn must call the same isolation builders, not raw `bash -c`.**
   - else → `process_registry.spawn_via_env(...)` (runs `nohup ... > logfile` inside sandbox, polls the log). → **LunaMoth maps `docker` isolation here IF the container is long-lived; but LunaMoth's runner.py spins a fresh `docker run` per command (no persistent container). For background docker, LunaMoth must either (a) keep the one-shot `docker run` alive as a tracked Popen (treat like spawn_local since `docker run` blocks in the foreground process), or (b) build a persistent container. Recommend (a): the `docker run` invocation IS a host Popen, so spawn_local-style tracking works directly — no log-file polling needed.**
3. Build `result_data = {"output":"Background process started", "session_id", "pid", "exit_code":0, "error":None}` (2138-2144).
4. **Silent-bg nudge** (2162-2174): if `background and not notify_on_complete and not watch_patterns`, attach a `hint` warning that the process runs silently. → **Port as an advisory `hint` — fits the chara curriculum.**
5. **CI-poller anti-pattern nudge** (2205-2253): hermes-dev-specific (`gh pr view --json statusCheckRollup`, `gh pr checks | jq`). → **SKIP for LunaMoth (hermes-internal dogfooding scar, not general).**
6. **Routing metadata** (2258-2272): copies `HERMES_SESSION_*` gateway env onto `proc_session.watcher_*` for notification routing. → **LunaMoth maps to the `speak` channel / presence: a completed bg process should surface via the existing `stream_event`/notification path, NOT a messaging chat-id. Re-target watcher_* to "emit a Notice protocol event."**
7. Apply mutex (2281-2288), set `notify_on_complete` flag + optionally register a fast watcher (interval 5s) in gateway mode (2291-2311), set `watch_patterns` (2314-2316).
8. Return `result_data` JSON. The process keeps running; the agent learns the outcome only via `process(...)` or the completion_queue drain.

### 1.5 watch_patterns rate-limit + auto-promote (process_registry.py:62-75, 191-404)

Constants: `WATCH_MIN_INTERVAL_SECONDS=15` (min spacing between match notifications), `WATCH_STRIKE_LIMIT=3` (consecutive strike windows → disable), plus a GLOBAL circuit breaker `WATCH_GLOBAL_MAX_PER_WINDOW=15` / `WATCH_GLOBAL_WINDOW_SECONDS=10` / `WATCH_GLOBAL_COOLDOWN_SECONDS=30`.

Per-session logic (`_check_watch_patterns` process_registry.py:191-317):
- Skip if no patterns, `_watch_disabled`, or `session.exited` (post-exit chunks are dropped to stop stale-notification spam, 210-211).
- Scan new chunk line-by-line; substring match (`pat in line`, not regex), first matching pattern recorded (216-222).
- **Cooldown active** (now < `_watch_cooldown_until`): drop the match, increment `_watch_suppressed`. First drop in this window sets `_watch_strike_candidate=True` and `_watch_consecutive_strikes += 1`. At `>= WATCH_STRIKE_LIMIT` → `_watch_disabled=True` AND **auto-promote** `session.notify_on_complete=True` (so one exit notification still fires), emit a one-shot `watch_disabled` event (234-292).
- **Cooldown expired**: if the prior window had no strike candidate, reset the consecutive-strike counter (healthy cadence); emit the `watch_match` event, start a fresh 15s cooldown, bump `_watch_hits`, attach the suppressed count (247-266, 303-317).
- **Global breaker** (`_global_watch_admit` 319-404): a rolling 10s window across ALL sessions; >15 admits trips a 30s cooldown that drops further matches, emitting one `watch_overflow_tripped` and later one `watch_overflow_released` summary.

→ **LunaMoth port**: this whole subsystem is sound and self-contained (stdlib threading + a queue). Port it largely verbatim into LunaMoth's process registry, but re-target the emitted events: instead of `completion_queue` dicts with messaging `chat_id`/`thread_id`, emit `Notice`/`TextDelta(channel=...)` protocol events through the existing stream so the TUI/web render them. The rate-limit math is provider-agnostic — keep all five constants.

### 1.6 Return values per mode (summary)

| mode | return |
|---|---|
| invalid command | `{output:"", exit_code:-1, error:..., status:"error"}` (1864) |
| fg over-limit timeout | `{error:"Foreground timeout Ns exceeds max..."}` (1916) |
| fg guidance block | `{output:"", exit_code:-1, error:<guidance>, status:"error"}` (1929) |
| blocked workdir | `{output:"", exit_code:-1, error:..., status:"blocked"}` (2087) |
| fg success | `{output, exit_code, error:None, [approval], [exit_code_meaning]}` (2425) |
| fg timeout | `{output:"", exit_code:124, error:"Command timed out..."}` (2345) |
| bg started | `{output:"Background process started", session_id, pid, exit_code:0, error:None, [hint], [notify_on_complete], [watch_patterns], [watch_patterns_ignored], [pty_note]}` (2138) |
| bg spawn failure | `{output:"", exit_code:-1, error:"Failed to start background process: ..."}` (2320) |
| top-level exception | `{output:"", exit_code:-1, error:..., traceback, status:"error"}` (2441) |

→ **LunaMoth uses flat strings for fg; for bg it must introduce a JSON-ish session handle (`session_id`, `pid`) since the model needs the id for the `process` tool.** This is the one place LunaMoth must add structure.

### 1.7 Infra-coupled parts (mark "→ LunaMoth maps to isolation.py")

All of the following are hermes multi-backend machinery that **collapse to LunaMoth's single-session, three-isolation (dir/sandbox/docker) model** and should NOT be ported:
- `_get_env_config` (1063-1187): TERMINAL_ENV/docker/singularity/modal/daytona/ssh config soup. → LunaMoth reads isolation from `EnvState` (state.py) + session meta, fresh per call (already done, gateway.py:233-240).
- `_create_environment` (1199-1346), all `tools/environments/*` classes, Modal managed/direct backend resolution. → LunaMoth's `session/isolation.py` builders (`_docker`, `_linux_jail`, `_macos_jail`).
- `_active_environments` / `_last_activity` / `_creation_locks` / cleanup thread / orphan reaper / atexit (855-1593). → LunaMoth: ONE session = ONE sandbox, no multi-env registry, no idle reaper for envs (the supervisor owns chara lifecycle). DROP entirely.
- `task_id` collapse logic (`_resolve_container_task_id` 996-1028), per-task overrides. → DROP; LunaMoth task_id ≡ the single session.
- sudo prompt/cache/transform (152-817). → DROP (no interactive sudo model; isolation is the security boundary).
- dangerous-command approval guards (`_check_all_guards` 255-263). → LunaMoth maps to `request_permission` (presence-gated) + the allowlisted ToolGateway, NOT an inline command-string guard.

---

## PART 2 — `process` tool

### 2.1 JSON schema (process_registry.py:1541-1583)

```
name: "process"
description: "Manage background processes started with terminal(background=true). Actions: list/poll/log/wait/kill/write/submit/close."
parameters (type object):
  action      string   REQUIRED  enum: ["list","poll","log","wait","kill","write","submit","close"]
  session_id  string             required for ALL actions except "list" (handler enforces, 1595)
  data        string             for "write" and "submit" (stdin payload)
  timeout     integer minimum 1  for "wait" (max block seconds; partial output on timeout)
  offset      integer            for "log" (line offset; default 0 = last `limit` lines)
  limit       integer minimum 1  for "log" (max lines; default 200)
required: ["action"]
```

Handler `_handle_process` (process_registry.py:1586-1612): coerces `session_id` to str (models sometimes send int, 1590); routes to registry methods; returns `tool_error` for missing session_id or unknown action.

### 2.2 Per-action behavior

- **`list`** (`list_sessions` 1274-1301): returns `{"processes": [...]}`. Each entry: `session_id, command[:200], cwd, pid, started_at (localtime ISO), uptime_seconds, status (running|exited), output_preview (last 200 chars)`, plus `exit_code` if exited and `detached:True` if recovered. Optional `task_id` filter. Refreshes detached sessions first (1279).
- **`poll`** (994-1023): `get()` the session; **`_reconcile_local_exit`** first (see 2.5); returns `{session_id, command, status, pid, uptime_seconds, output_preview (strip_ansi last 1000 chars)}`. If exited: adds `exit_code` and marks `_completion_consumed` (so the drain loop won't double-notify). If detached: adds `detached:True` + a "output history unavailable" note.
- **`log`** (`read_log` 1025-1054): full `strip_ansi(output_buffer)` split into lines. Pagination: if `offset==0 and limit>0` → last `limit` lines (`lines[-limit:]`); else `lines[offset:offset+limit]`. Returns `{session_id, status, output, total_lines, showing:"N lines"}`. Marks `_completion_consumed` if exited.
- **`wait`** (1056-1131): blocks until exit / timeout / interrupt. Timeout resolution: `effective = min(requested, TERMINAL_TIMEOUT default 180)`; if requested > max, clamps and adds `timeout_note` (1079-1086). Loop (1s tick): refresh detached, `_reconcile_local_exit`, return on `exited` → `{status:"exited", exit_code, output (last 2000 chars)}`; on `is_interrupted()` → `{status:"interrupted", output (last 1000), note}` (1111-1119). On deadline → `{status:"timeout", output (last 1000), timeout_note:"Waited Ns, still running"}`. → **`is_interrupted()` maps to LunaMoth: a user message arriving mid-wait should break the block. LunaMoth's equivalent is the patience/quiet engagement signal — wire `wait` to abort when a new user turn arrives.**
- **`kill`** (`kill_process` 1133-1201): see 2.4. Returns `{status:"killed", session_id}` / `already_exited` / `not_found` / `error`.
- **`write`** (`write_stdin` 1203-1232): raw bytes to stdin, NO newline. PTY path uses `_pty.write` (bytes on POSIX, str on Windows); Popen path uses `process.stdin.write/flush`. Returns `{status:"ok", bytes_written}` or error if stdin unavailable (non-local backend or closed).
- **`submit`** (`submit_stdin` 1234-1236): `write_stdin(data + "\n")` — answers an interactive prompt (presses Enter).
- **`close`** (`close_stdin` 1238-1259): PTY → `sendeof()`; Popen → `stdin.close()`. Sends EOF WITHOUT killing the process. Returns `{status:"ok", message:"EOF sent"/"stdin closed"}`.

→ **write/submit/close are stdin interaction — only meaningful for the local Popen/PTY path. LunaMoth's `dir`/`sandbox` spawns ARE host Popens, so these port directly IF the bg Popen is created with `stdin=subprocess.PIPE` (note: hermes spawn_local uses `stdin=subprocess.DEVNULL` for the non-PTY path, process_registry.py:606 — so write/submit/close only work in PTY mode there. To support stdin in pipe mode, LunaMoth must spawn bg with `stdin=PIPE`.) Docker one-shot → stdin generally unavailable; return the "non-local backend" error.**

### 2.3 Registry data structure & lifecycle

**`ProcessSession` dataclass** (process_registry.py:89-134): `id` (`proc_<uuid12>`), `command`, `task_id`, `session_key`, `pid`, `process` (Popen, local only), `env_ref`, `cwd`, `started_at`, `exited`, `exit_code`, `output_buffer` (rolling, capped at `max_output_chars`), `detached`, `pid_scope` ("host"|"sandbox"), watcher_* routing fields, `notify_on_complete`, `watch_patterns` + the private `_watch_*` rate-limit counters, a per-session `_lock`, `_reader_thread`, `_pty`.

**`ProcessRegistry`** (137-1495): two dicts `_running` / `_finished` under a single `_lock`; a `completion_queue` (Queue, unified completion + watch events); `_completion_consumed` set; global-watch-breaker state. Module singleton `process_registry` (1495).

Constants (57-60): `MAX_OUTPUT_CHARS=200_000` (200KB rolling buffer), `FINISHED_TTL_SECONDS=1800` (keep finished 30 min), `MAX_PROCESSES=64` (LRU prune).

**Lifecycle:**
- **Spawn** → `spawn_local` (516-651) or `spawn_via_env` (653-747). Creates session, starts a reader/poller thread, `_prune_if_needed()` under lock, inserts into `_running`, `_write_checkpoint()`.
- **Reap (reader thread)** → `_reader_loop` (751-777): reads stdout in 4096-byte chunks, strips shell-startup noise from the first chunk (`_clean_shell_noise` 183-189), appends to `output_buffer` (trims to last `max_output_chars`), runs `_check_watch_patterns`. On EOF: `process.wait(timeout=5)`, set `exited`/`exit_code`, `_move_to_finished`.
- **`_move_to_finished`** (863-888): idempotent (guards against kill racing the reader); moves running→finished; on FIRST move with `notify_on_complete`, enqueues a `completion` event (last 2000 chars, ansi-stripped) to `completion_queue`.
- **Cleanup/prune** `_prune_if_needed` (1350-1375): drops finished sessions older than `FINISHED_TTL_SECONDS`; if total ≥ `MAX_PROCESSES`, evicts oldest finished; cleans stale `_completion_consumed` entries.
- **Checkpoint** `_write_checkpoint` (1379-1410): atomic JSON of running sessions to `~/.hermes/processes.json`. **Recovery** `recover_from_checkpoint` (1412-1491): on restart, probes each saved PID; host-scope alive PIDs become `detached=True` sessions (status+kill only, no output); sandbox-scope PIDs are skipped (in-sandbox PIDs meaningless to the restarted host). → **LunaMoth maps the checkpoint to its session dir (`~/.lunamoth/sessions/<name>/processes.json`); recovery is owned by the supervisor (lunamothd) on rejoin. Useful but a follow-up — the core registry works without it.**

**Notification drain** `drain_notifications` (896-914): pops all `completion_queue` events, skips already-consumed completions, formats via `format_process_notification` (1498-1533) into `[IMPORTANT: ...]` strings. → **LunaMoth re-targets: emit protocol events, not `[IMPORTANT:]` text.**

### 2.4 kill — process-group / descendant handling (the scar-dense bits)

`_terminate_host_pid` (process_registry.py:435-500): POSIX walks the tree with `psutil` — SIGTERM **children first (recursive), then parent** — so subprocess trees (Chromium renderers etc.) don't get reparented to init and survive. Windows uses `taskkill /T /F`. Bare-`os.kill` fallback on psutil failure. `kill_process` (1133-1201) dispatches: PTY → `_pty.terminate(force=True)`; local Popen → psutil children-first (1160-1170); non-local → `env.execute("kill <pid>")` inside sandbox; detached host PID → liveness check then `_terminate_host_pid`. On success sets `exit_code=-15` (SIGTERM) and `_move_to_finished`.

→ **LunaMoth ALIGNMENT (the key reuse win)**: `runner.py:144-177 `_kill_group` already does SIGTERM→grace(`_KILL_GRACE=1s`)→SIGKILL to the whole **process group** via `os.killpg(os.getpgid(pid), sig)`, with the EPERM-fallback to `send_signal` on the leader (macOS group-mid-exit scar). This is **cleaner than hermes's psutil children-walk** for the bg-kill case because the bg Popen is spawned with `start_new_session=True` (runner.py:268) / `os.setsid` (hermes spawn_local uses `preexec_fn=os.setsid`, process_registry.py:607). **PORT DECISION: LunaMoth's bg `kill` should reuse `_kill_group` directly — it's the group-kill hermes reaches for psutil to approximate.** The only gap: hermes spawn_local uses `os.setsid` (new session, killpg-able) — LunaMoth must spawn bg the same way (it already does via `start_new_session=True` in the fg runner; replicate for bg). Note the spawn-failure cleanup in hermes (process_registry.py:630-649) ALSO uses `os.killpg(os.getpgid(proc.pid), SIGKILL)` — same group discipline.

### 2.5 The orphaned-pipe drain (process_registry.py:922-992) — the #17327 scar

`_reconcile_local_exit` (922-992): the reader thread only flips `session.exited` in its `finally` after `stdout.read()` hits EOF. If the direct Popen child has exited but a **descendant still holds the stdout pipe open** (e.g. a double-forked daemon), the reader blocks forever and `poll`/`wait` report "running" indefinitely (issue #17327 — 74 polls over 7 min). Fix: when `session.exited` is still False but `proc.poll()` returns a real exit code, do a **non-blocking drain** of any buffered bytes (`fcntl` O_NONBLOCK + `stdout.read()`, restore flags in finally), append to buffer, flip `exited`/`exit_code`, `_move_to_finished`. The orphaned reader thread stays stuck but is a daemon and dies with the process. Called at the top of `poll` (1004) and inside the `wait` loop (1099).

→ **LunaMoth ALIGNMENT**: `runner.py:179-212 `_drain_nonblocking` is the SAME shape (O_NONBLOCK + wall-clock deadline `_DRAIN_DEADLINE=1s`, the comment even cites "the hermes _reconcile_local_exit drain shape"). LunaMoth already uses it in the FG timeout path. **For the BG registry, LunaMoth needs the reconcile LOGIC (poll the direct child, flip exited, drain) — `_drain_nonblocking` is the primitive it'll call. Port `_reconcile_local_exit` as a method on LunaMoth's registry, calling the existing `_drain_nonblocking`.** This is the single most important correctness port for the bg path: without it, `process(action='poll')` hangs on any command that spawns a surviving grandchild.

### 2.6 Infra-coupled in process_registry → "→ LunaMoth maps to isolation"

- `spawn_via_env` (653-747) + `_env_poller_loop` (779-830): the nohup-into-logfile + `kill -0` poll dance for non-local sandboxes. → **LunaMoth's docker bg should track the `docker run` host Popen directly (spawn_local-style), making this whole log-poll path unnecessary for dir/sandbox/docker. Only port spawn_via_env if a persistent-container model is later adopted.**
- `pid_scope`/`detached`/`_refresh_detached_session` (406-433): host-vs-sandbox PID semantics for crash recovery. → simplifies to host-only for LunaMoth (all three isolations spawn a host Popen).
- gateway watcher routing (`pending_watchers`, `has_active_for_session`, `session_key`): → re-target to LunaMoth presence/supervisor, not gateway chat sessions.

---

## Reuse vs. port summary (what LunaMoth already has)

| Concern | hermes | LunaMoth runner.py | action |
|---|---|---|---|
| group kill SIGTERM→SIGKILL | psutil children-walk (435-500) | `_kill_group` killpg (144-177) | **reuse LunaMoth's** for bg kill |
| non-blocking pipe drain | `_reconcile_local_exit` (922-992) | `_drain_nonblocking` (179-212) | reuse primitive; **port the reconcile loop** |
| ANSI strip | tools/ansi_strip.py | runner.py:56-91 (ported) | already aligned (LunaMoth strips before truncate — keep) |
| head/tail truncate | inline 40/60 (2402-2410) | `truncate_middle` (124-141) | already aligned |
| exit-code-info note | `_interpret_exit_code` (1604-1665) | `_exit_code_note` (94-121) subset | **widen the table + last-segment parse** |
| timeout clamp | reject fg (1915) | clamp+note (234-239) | keep LunaMoth's clamp |
| workdir injection guard | `_validate_workdir` (273-292) | none | **PORT** (dir isolation runs as user) |
| bg registry / reader / watch | process_registry.py | **none — must build** | port registry, reader_loop, watch subsystem, reconcile |
| stdin write/submit/close | Popen stdin (1203-1259) | none | port; spawn bg with `stdin=PIPE` |
| multi-backend env mgmt | 855-1593 | n/a (one session) | **DROP** |
