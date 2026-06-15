# Browser tool suite — faithful re-implementation spec (hermes-agent → LunaMoth)

Source read (all paths under `reference/hermes-agent/`):
- `tools/browser_tool.py` (164 KB — the main tool surface + driver + lifecycle)
- `tools/browser_cdp_tool.py` (raw CDP escape hatch)
- `tools/browser_dialog_tool.py` (native-dialog responder)
- `tools/browser_supervisor.py` (persistent CDP WebSocket per task)
- `tools/browser_camofox.py` (alt anti-detection REST backend — out of scope for the port)
- `hermes_cli/dep_ensure.py`, `package.json`, `Dockerfile`
- `website/docs/developer-guide/browser-supervisor.md`

**Bottom line up front:** this is the heaviest tool group. The driver is NOT
Playwright-in-process and NOT raw CDP — it is an external **Node CLI named
`agent-browser`** (`package.json:36` → `"agent-browser": "^0.26.0"`) that hermes
shells out to once per tool call. `agent-browser` internally uses Playwright +
Playwright's Chromium/headless-shell build. So a faithful port carries a
**Node.js runtime + an npm package + a ~150 MB Chromium download** as its true
dependency footprint, plus per-task subprocess/daemon lifecycle management.

---

## 1. The tools (exact JSON schemas + real behavior)

There are **12 agent-facing tools** across three registry toolsets:
`browser` (the 10 core), `browser-cdp` (`browser_cdp` + `browser_dialog`).
Core schemas live in `BROWSER_TOOL_SCHEMAS` (`browser_tool.py:1472-1619`);
each handler takes an extra `task_id: Optional[str]` that is **injected by the
runtime, NOT exposed in the schema** (session isolation key; defaults to
`"default"`). Every tool returns a **JSON string** (`json.dumps(..., ensure_ascii=False)`)
with a `success: bool` and either result fields or `error`.

### 1.1 `browser_navigate` (`browser_tool.py:2291`)
- Schema: `{ "url": {type:string, required} }`. Required: `["url"]`.
- Behavior: the entry point — must be called before any other browser tool
  (creates/looks up the session). Steps, in order:
  1. **Secret-exfil guard** — rejects if the URL (raw or %-decoded) matches an
     API-key/token prefix regex (`agent.redact._PREFIX_RE`) → `success:false`
     "URL contains what appears to be an API key or token" (`:2306-2322`).
  2. **SSRF guard** — `_is_always_blocked_url` always blocks cloud-metadata
     IMDS endpoints (169.254.169.254, metadata.google.internal, ECS task
     metadata) regardless of backend; `_is_safe_url` blocks private/internal
     addresses unless a local backend / `allow_private_urls` / hybrid local
     sidecar (`:2336-2357`). Re-checked **post-redirect** on the final URL,
     navigating to `about:blank` if a redirect lands somewhere blocked
     (`:2404-2436`).
  3. **Website-policy** check (`check_website_access`) — allow/deny lists.
  4. Runs CLI `open <url>` (timeout = max(command_timeout, 60)).
  5. **Auto-snapshots** (`snapshot -c`, compact) and folds the snapshot text +
     `element_count` into the response so the model can act without a second
     call (`:2474-2489`). Truncated at 8000 chars (`SNAPSHOT_SUMMARIZE_THRESHOLD`).
  6. Adds `bot_detection_warning` if the page title matches Cloudflare/captcha
     patterns (`:2446-2461`), and `stealth_features`/`stealth_warning` on first
     nav for cloud backends.
- Returns: `{success, url(final), title, snapshot, element_count, ...}`.

### 1.2 `browser_snapshot` (`browser_tool.py:2499`)
- Schema: `{ "full": {type:boolean, default:false} }`. Required: `[]`.
  Handler also accepts internal `user_task` (task-aware extraction, not in schema).
- Behavior: runs CLI `snapshot` (`-c` compact unless `full=true`). Compact =
  interactive elements + ref ids only; full = complete accessibility tree.
  If text > 8000 chars: with `user_task` → LLM-summarize relevant content
  (`_extract_relevant_content`, aux model `AUXILIARY_WEB_EXTRACT_MODEL`); else
  hard `_truncate_snapshot`. Merges CDP-supervisor state when attached:
  `pending_dialogs` + `frame_tree` (`:2546-2557`).
- Returns: `{success, snapshot, element_count, [pending_dialogs], [frame_tree]}`.

### 1.3 `browser_click` (`browser_tool.py:2568`)
- Schema: `{ "ref": {type:string, required} }`. Required: `["ref"]`.
- Behavior: normalizes ref to `@`-prefixed (`@e5`), runs CLI `click @e5`.
  Returns `{success, clicked}`.

### 1.4 `browser_type` (`browser_tool.py:2605`)
- Schema: `{ "ref":{string,req}, "text":{string,req} }`. Required: `["ref","text"]`.
- Behavior: runs CLI `fill @ref text` — **clears then types** (it's a fill, not
  an append). Returns `{success, typed, element}`.

### 1.5 `browser_scroll` (`browser_tool.py:2645`)
- Schema: `{ "direction": {string, enum:["up","down"], req} }`. Required: `["direction"]`.
- Behavior: validates direction; runs CLI `scroll <dir> 500` (500 px ≈ half a
  viewport — one subprocess, not the old 5×). Returns `{success, scrolled}`.

### 1.6 `browser_back` (`browser_tool.py:2694`)
- Schema: no params. Required: `[]`. CLI `back`. Returns `{success, url}`.

### 1.7 `browser_press` (`browser_tool.py:2726`)
- Schema: `{ "key": {string, req} }` (e.g. "Enter","Tab","Escape","ArrowDown").
  CLI `press <key>`. Returns `{success, pressed}`.

### 1.8 `browser_get_images` (`browser_tool.py:3012`)
- Schema: no params. Required: `[]`.
- Behavior: NOT a CLI verb — runs CLI `eval <js>` with a JS snippet that maps
  `document.images` → `{src,alt,width,height}`, filtering out `data:` URLs
  (`:3029-3036`). Returns `{success, images:[...], count}`.

### 1.9 `browser_vision` (`browser_tool.py:3073`)
- Schema: `{ "question":{string,req}, "annotate":{boolean, default:false} }`.
  Required: `["question"]`.
- Behavior: runs CLI `screenshot --full <path>` (`+ --annotate` to overlay
  numbered `[N]` labels mapping to `@eN`). Saves PNG to
  `~/.hermes/cache/screenshots/`, prunes >24 h old. Then:
  - If the **active model has native vision** → attaches the screenshot
    directly as a multimodal tool-result (no aux call), returns an envelope
    with `meta.screenshot_path`.
  - Else → calls the **auxiliary vision model** (`AUXILIARY_VISION_MODEL`) for
    a text analysis.
  - Returns a `screenshot_path` so it can be surfaced to the user via
    `MEDIA:<path>`. **This is the only tool that needs a graphical renderer**
    (Lightpanda has none → pre-routes to a throwaway Chrome session, `:3107-3142`).

### 1.10 `browser_console` (`browser_tool.py:2761`)
- Schema: `{ "clear":{boolean, default:false}, "expression":{string} }`. Required: `[]`.
- Behavior: **dual-mode.** If `expression` is set → `_browser_eval` runs JS in
  the page (`Runtime.evaluate` via supervisor fast-path if attached, else CLI
  `eval`), returns the (JSON-parsed) value. Else → reads console buffers: CLI
  `console` + `errors`, returns `{success, console_messages[], js_errors[],
  total_messages, total_errors}`. `clear=true` empties the buffers after read.

### 1.11 `browser_cdp` (`browser_cdp_tool.py:300`, schema `:427`)
- Schema: `{ "method":{string,req}, "params":{object, default:{}},
  "target_id":{string}, "frame_id":{string}, "timeout":{number, default:30, max:300} }`.
  Required: `["method"]`.
- Behavior: raw CDP escape hatch. Needs a reachable CDP WebSocket endpoint
  (`/browser connect` to a locally-running Chrome/Brave/Edge, or `browser.cdp_url`
  in config). `target_id` → attach to a tab (`flatten=True`) and run with its
  `sessionId`; `frame_id` → route through the supervisor's live socket for
  cross-origin iframe (OOPIF) `Runtime.evaluate`. Stateless per call otherwise.
  **Requires the Python `websockets` package** (`_WS_AVAILABLE`). `check_fn`
  gates the tool: only present when a CDP endpoint resolves.

### 1.12 `browser_dialog` (`browser_dialog_tool.py:82`, schema `:28`)
- Schema: `{ "action":{string, enum:["accept","dismiss"], req},
  "prompt_text":{string}, "dialog_id":{string} }`. Required: `["action"]`.
- Behavior: response-only. Read `pending_dialogs` from `browser_snapshot` first,
  then accept/dismiss a blocking native `alert/confirm/prompt/beforeunload`.
  `prompt_text` supplies the `prompt()` answer; `dialog_id` disambiguates when
  multiple are queued. Routed through the per-task CDPSupervisor. Same CDP gate
  as `browser_cdp` (the two appear/disappear together).

> Note: there is **no standalone `browser_close`** tool exposed to the model —
> teardown is automatic (see §2 lifecycle). `cleanup_browser` /
> `cleanup_all_browsers` (`browser_tool.py:3393`, `:3511`) are internal.

---

## 2. The DRIVER, lifecycle, and install

### 2.1 What backs it
- **Automation engine:** the `agent-browser` **Node CLI** (npm
  `agent-browser@^0.26.0`), invoked as a subprocess per tool call. It is NOT
  imported in-process; hermes builds an argv and `subprocess.Popen`s it.
- **Under agent-browser:** Playwright driving **Playwright's Chromium /
  chromium_headless_shell** build. agent-browser also supports an alternate
  `--engine lightpanda` (a lightweight headless renderer with no graphics — used
  for fast text nav/snapshot; screenshots fall back to Chrome).
- **Backends (auto-detected):** **local Chromium** (default, zero-cost,
  headless, works without a display); **cloud** (Browserbase / Browser Use /
  Firecrawl, via `--cdp <ws>`); **Camofox** (separate REST anti-detect server,
  `CAMOFOX_URL`); **user CDP** (`/browser connect` / `browser.cdp_url`). The port
  should ship **only the local-Chromium path** — the cloud providers live in
  `plugins/browser/<vendor>/` and are pluggable.

### 2.2 How a command is run (`_run_browser_command`, `browser_tool.py:1877`)
- Resolve CLI: `_find_agent_browser()` (`:1753`) checks PATH → extended PATH
  (Homebrew `node@NN`, hermes-managed node) → repo `node_modules/.bin` →
  `npx agent-browser` fallback → lazy install. Result cached.
- Resolve session: `_get_session_info(task_id)` (`:1653`). Local mode →
  `_create_local_session` mints `session_name = f"h_{uuid[:10]}"` (`:1626`).
- Build argv: `<cli> --session <name> --json <command> <args...>` (local) or
  `<cli> --cdp <ws> --json ...` (cloud). `--engine <e>` appended unless `auto`.
- **Per-task socket dir:** `$TMPDIR/agent-browser-<session_name>/` (created
  0700), passed as `AGENT_BROWSER_SOCKET_DIR`. This isolates parallel tasks so
  they don't fight over one daemon socket. macOS `AF_UNIX` 104-byte path limit
  is actively worked around (`_socket_safe_tmpdir`, `:1136`).
- **stdout/stderr to temp files, NOT pipes** (`:2046-2098`): agent-browser
  spawns a background **daemon** that inherits fds; with pipes `communicate()`
  never sees EOF and hangs to timeout. So it writes to `_stdout_<cmd>` /
  `_stderr_<cmd>` files and reads them after `proc.wait(timeout)`. **A porter
  MUST replicate this** — it's the #1 hang foot-gun.
- **`--no-sandbox` injection** (`:2012-2044`): when running as root (Chromium
  refuses otherwise) or when AppArmor restricts unprivileged user namespaces
  (`/proc/sys/kernel/apparmor_restrict_unprivileged_userns == 1`), sets
  `AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage`. **Directly relevant
  to LunaMoth's OS-jail — see §4.**

### 2.3 State held across tool calls
- **In-process:** `_active_sessions[task_id] → session_info` dict (under
  `_cleanup_lock`), `_session_last_activity[task_id]`, `_last_active_session_key`
  (so snapshot/click after a nav hit the right session under hybrid routing).
- **Out-of-process:** the **agent-browser daemon** (a long-lived Node process +
  its Chromium) keyed by `--session <name>` + socket dir. Subsequent CLI calls
  with the same session name reconnect to the same daemon/page — **that's how
  the page persists between tool calls** (the Python side is stateless per call).
- **CDP supervisor** (`browser_supervisor.py`): one `asyncio.Task` in a daemon
  thread per task, holding a persistent CDP WebSocket — only for CDP-capable
  backends — tracking dialog queue, frame tree, session map, console ring.

### 2.4 Lifecycle: start / reuse / stop
- **Start:** lazy on first `browser_navigate` (or any command) for a `task_id`.
- **Reuse:** same `task_id` → same session_name → same daemon/page.
- **Stop, three layers:**
  1. **Daemon self-idle:** `AGENT_BROWSER_IDLE_TIMEOUT_MS` env (default
     `BROWSER_INACTIVITY_TIMEOUT=300` s ×1000) tells the Node daemon to kill
     itself + its Chromium after inactivity (`:2003-2010`).
  2. **Python cleanup thread:** `_start_browser_cleanup_thread` periodically runs
     `_cleanup_inactive_browser_sessions` → `cleanup_browser(task_id)` (CLI
     `close`) for sessions idle > 300 s (`:1247`, `:1434`).
  3. **atexit:** `_emergency_cleanup_all_sessions` + `_stop_browser_cleanup_thread`
     registered via `atexit` only — NOT SIGINT/SIGTERM (signal handlers from a
     non-main thread corrupt coroutine state / make the process unkillable,
     `:1234-1240`).
- **Orphan reaper:** `_reap_orphaned_browser_sessions` (`:1293`) scans
  `$TMPDIR/agent-browser-*` socket dirs, reads `<session>.owner_pid`, and kills
  daemons whose owning host process is dead (survives crashes/SIGKILL).

### 2.5 Install
- **npm package:** `agent-browser@^0.26.0` (in `package.json`). Installed via
  `npm install` in repo root (drops `node_modules/.bin/agent-browser`) or
  `npm install -g agent-browser`, or run ad-hoc via `npx agent-browser`.
- **Chromium download (separate, one-time):** `agent-browser install`
  (downloads Chromium) / `agent-browser install --with-deps` (also apt system
  libs on Debian/Ubuntu/Docker). Equivalent to `npx playwright install
  --with-deps chromium`. Docker does `npx playwright install --with-deps
  chromium --only-shell` into `PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright`
  (`Dockerfile:17,133`).
- **Presence check:** `_chromium_installed` (`:3580`) accepts (1)
  `AGENT_BROWSER_EXECUTABLE_PATH`, (2) system chrome/chromium in PATH, (3) a
  `chromium-*` / `chromium_headless_shell-*` dir in the Playwright cache. Tool
  is gated behind this so it doesn't advertise a capability that hangs.
- **Lazy install:** `hermes_cli/dep_ensure.ensure_dependency("browser")` is
  attempted on first use if the CLI is missing.

---

## 3. The accessibility-snapshot model (the core UX)

This is what makes it usable by a text LLM — copy it faithfully.

- agent-browser serializes the page via Playwright's **`ariaSnapshot`**
  (accessibility tree, not raw DOM) into compact text. Each interactive element
  is tagged with a stable **ref id** rendered as `@e1`, `@e2`, … (shown in the
  snapshot, e.g. in `[ ]` brackets).
- **The flow is: snapshot → act-by-ref.** The model never sends CSS selectors or
  coordinates. It reads `@eN` from a snapshot, then `browser_click("@e5")` /
  `browser_type("@e3", "...")`. Handlers auto-prefix `@` if the model omits it.
- `browser_navigate` returns an **auto compact snapshot** so the model can act
  immediately (no mandatory separate snapshot call). `element_count` = number of
  refs. Refs are re-issued per snapshot; after a page mutation the model should
  re-snapshot to get fresh refs.
- **Size discipline:** snapshots > 8000 chars are either LLM-summarized against
  the user's task (`_extract_relevant_content`) or hard-truncated
  (`_truncate_snapshot`). Compact mode (`-c`) returns interactive elements only;
  `full=true` returns the whole tree.
- **`--annotate`** (vision): overlays numbered `[N]` labels on the screenshot;
  each `[N]` maps to `@eN`, bridging the visual and the ref model for spatial
  reasoning / CAPTCHA work.
- The CDP supervisor **augments** the snapshot with `pending_dialogs[]`
  (`{id,type,message}`) and a `frame_tree` (with OOPIF `frame_id`s) — those feed
  `browser_dialog` and `browser_cdp(frame_id=...)`.

---

## 4. Portability, footprint, and the LunaMoth optional extra

### 4.1 macOS/Linux portable vs not
- **Portable:** the whole local-Chromium path. macOS-first/Linux is exactly
  hermes' stance. macOS Homebrew node discovery + the 104-byte `AF_UNIX` socket
  path workaround are already in-code (`:1136`, `_discover_homebrew_node_dirs`).
- **Linux-only bits:** `--no-sandbox` AppArmor detection (`/proc/.../userns`),
  `--with-deps` apt libs. Benign on macOS.
- **Drop entirely for the port:** all Windows `STARTUPINFO`/`CREATE_NO_WINDOW`
  branches (LunaMoth is macOS/Linux only — CLAUDE.md), Termux/Android PATHs, the
  cloud providers (Browserbase/Browser Use/Firecrawl), and Camofox.

### 4.2 Dependency footprint (be honest — this is the heavy one)
1. **Node.js runtime** (the tool is a Node CLI). LunaMoth is otherwise
   stdlib-leaning Python — this is a brand-new, large runtime dependency.
2. **npm package** `agent-browser` (+ its Playwright dep tree).
3. **Chromium / headless-shell** binary (~120–170 MB) via
   `playwright install chromium` (or `--only-shell` for the smaller shell).
4. **Python `websockets`** — only for `browser_cdp` / `browser_dialog`
   (supervisor + raw CDP). The 10 core tools need none of it.
5. Optional aux LLM (vision/extraction models) — already in LunaMoth's provider
   layer; reuse, don't add.

### 4.3 As a LunaMoth optional extra
- **Python side:** `uv sync --extra browser` → pulls only `websockets`
  (and any small helpers). The real weight is non-Python.
- **Node + Chromium:** cannot be a `uv` extra. Mirror hermes:
  a `lunamoth doctor`/`setup`-driven step that (a) verifies Node, (b)
  `npm i -g agent-browser` (or vendors it under `~/.lunamoth/node`), (c)
  `agent-browser install` for Chromium into a pinned `PLAYWRIGHT_BROWSERS_PATH`.
  Gate the tool behind a `_chromium_installed()`-style check so a chara without
  the browser stack simply doesn't get the tools (clean degrade — matches the
  no-fallback principle: surface a clear install hint, never silently no-op).
- **Toolpack:** expose as a `browser` toolpack so a card opts in
  (`extensions.lunamoth.toolpack`); keep `browser_cdp`/`browser_dialog` in a
  separate `browser-cdp` toolset gated on a reachable CDP endpoint.
- **Desktop (Electron) app:** Electron already bundles Chromium, but
  agent-browser wants its OWN Playwright Chromium — bundle/download it at
  install time into `~/.lunamoth` and point `AGENT_BROWSER_EXECUTABLE_PATH` /
  `PLAYWRIGHT_BROWSERS_PATH` at it. Do NOT try to reuse Electron's Chromium.

### 4.4 The OS-jail problem (flag loudly)
- LunaMoth's default isolation is `sandbox` (macOS **sandbox-exec** /
  Linux **bwrap**) per chara. **A browser is a fork-bomb of helper processes,
  needs its own user-namespace sandbox, GPU/display shims, and writable temp +
  socket dirs.** sandbox-exec / bwrap profiles will very likely **block
  Chromium from launching** (namespace creation, `/dev/shm`, socket dirs,
  process spawn). Symptoms mirror the in-code hints: "No usable sandbox",
  silent daemon exit, screenshot file never created.
- Mitigations a porter must decide on:
  - Allow `AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage` inside the
    jail (Chromium drops its own sandbox; the OS-jail becomes the only sandbox).
  - Ensure the jail profile permits: process spawn, the per-task socket dir
    under a writable `$TMPDIR`, the Playwright browsers path (read+exec), and
    enough `/dev/shm` or `--disable-dev-shm-usage`.
  - Realistically, the browser toolpack may require the chara's isolation to be
    `dir` or `docker` (the hermes Docker image is the proven config), NOT the
    default sandbox-exec/bwrap. **Treat "browser under sandbox-exec/bwrap" as an
    open R&D item, not a given.**

### 4.5 Per-chara process lifecycle under the supervisor (lunamothd)
- One chara = one activated session = one `task_id` is the natural key. Each
  gets its own agent-browser daemon + Chromium + socket dir.
- lunamothd (LunaMoth's supervisor) should own: the cleanup thread, the
  idle-timeout env, and **orphan reaping** keyed on an owner-pid file (port
  `_write_owner_pid` / `_reap_orphaned_browser_sessions`) so a chara crash
  doesn't leak a Chromium. Register teardown on chara detach/stop, mirroring
  hermes' `atexit` (but driven by the supervisor's lifecycle, not `atexit`,
  since lunamothd is long-lived).

---

## 10-line summary (for the porter)

1. **Tools (12):** `browser_navigate`, `browser_snapshot`, `browser_click`,
   `browser_type`, `browser_scroll`, `browser_back`, `browser_press`,
   `browser_get_images`, `browser_vision`, `browser_console` (+JS-eval mode) —
   plus CDP-gated `browser_cdp` and `browser_dialog`. All return JSON strings; an
   injected `task_id` is the session key (not in schema).
2. **Driver:** NOT in-process Playwright/CDP — it's the external **Node CLI
   `agent-browser@^0.26.0`**, shelled out per call, wrapping Playwright+Chromium.
3. **State across calls:** held by a long-lived **agent-browser daemon** keyed by
   `--session <name>` + per-task socket dir; Python side is stateless per call.
4. **Critical foot-gun:** capture stdout/stderr to **temp files, not pipes**, or
   the inherited daemon fds hang every call to timeout.
5. **Snapshot model:** Playwright `ariaSnapshot` → compact text with `@eN` ref
   ids; act by ref (`click @e5`), re-snapshot after mutations, 8 KB summarize cap.
6. **Install:** `npm i agent-browser` + `agent-browser install` (Playwright
   Chromium, ~150 MB) → can't be a `uv` extra; needs a `setup`/`doctor` step.
7. **Lifecycle:** lazy start; 300 s idle timeout (daemon-side env + Python
   thread); supervisor-owned cleanup + owner-pid orphan reaper on crash.
8. **`uv --extra browser`** pulls only Python `websockets` (CDP tools); the real
   weight (Node + Chromium) is non-Python and per-host.
9. **Decisions a porter must make:** vendor Node+Chromium under `~/.lunamoth`;
   make it a toolpack the card opts into; split `browser-cdp` behind a CDP gate.
10. **Hard part — flag it:** a real Chromium almost certainly **won't launch
    under sandbox-exec/bwrap**; expect to require `--no-sandbox` + `dir`/`docker`
    isolation for the browser toolpack. Treat sandbox-jailed browser as open R&D.
