# LunaMoth — project memory for Claude Code

## What this is, and who it serves (the product philosophy — read first)

LunaMoth is a runtime that lets an original character (an OC, a **chara**)
**live in a computer**: a persistent digital being with its own sandbox, memory,
goals, pace of life, and real agency (shell, files, tools) behind an
allowlisted, audited gateway. Three audiences, in priority order:

1. **OC creators** — people who want their original character alive QUICKLY,
   to chat with it and to watch it do its own things. The path from
   *inspiration → living chara* must be short: AI-assisted card creation
   (draft the card, a small SVG avatar, a theme color — like the web deck
   already does) while the human keeps FULL control over every detail of who
   their character is.
2. **The charas themselves** — they live at their own rhythm in step with
   reality: they think, pursue their goals, browse what interests them, rest
   when they choose, and *decide* when something is worth telling their human
   (the `speak` tool). The engine's job is respect: neutral guidance about how
   to use tools and unattended time — suggestions, never orders. This is where
   most of the remaining design effort lives (see Roadmap: the chara
   curriculum).
3. **Developers / agent users** — strip the persona and a chara degenerates
   cleanly into a hermes/openclaw-style workhorse: cards, packs, MCP, skills,
   headless `run -p`, the JSON-RPC gateway.

It draws on several projects — the clones under `reference/` (gitignored) are
**hermes-agent, AstrBot, cc-switch, openclaw**; **always consult them when
designing**. (SillyTavern is the card/world-book FORMAT spec we stay compatible
with, not a clone on disk.)

- **NousResearch/hermes-agent** — the most important. Agent runtime, context
  management, prompt-cache discipline, skills, plugin/registry patterns.
  RULE (owner, 2026-06-13): before building or fixing any COMMODITY
  subsystem (streaming, tool loop, PTY, dashboards, session hygiene —
  anything that isn't the chara-life innovation core), read the hermes
  counterpart first and port its solution shape AND its edge cases —
  hermes's scars are the maturity we lack. Architecture stays ours; never
  inherit its fallback-model logic. Parity checklist:
  `docs/OPEN-WORK.md` (Part 1).
  STRENGTHENED (owner, 2026-06-18): four subsystems are now **apple-to-apple
  IDENTICAL** to hermes — copy the algorithm, the numbers, and the prompt text
  verbatim, then run a comparison agent each pass until they match: (1) the
  **compaction trigger** (threshold ratio, protect-first/last, anti-thrash
  guard), (2) the **summary template** (the full structured `## Active Task…`
  sections, the iterative-update framing, the REFERENCE-ONLY handoff prefix,
  the deterministic static fallback), (3) **cache_control** (the
  `system_and_3` breakpoint placement), (4) **reasoning replay**
  (reasoning_content padding + `reasoning_details`/signature round-trip + the
  per-provider echo tiers). The ONLY edit allowed while porting these is
  de-branding the MODEL-FACING text: no literal "hermes"/"Hermes"/"the VM"/
  "Linux environment" may appear in any string the model sees — system prompts,
  tool descriptions, skill bodies, summary instructions (use neutral wording —
  "this runtime", "your environment"). Source-code COMMENTS may still cite the
  hermes counterpart as provenance (the codebase already does this everywhere).
  The good general prompts (task-completion discipline, tool-use enforcement,
  the SKILLS guidance) are migrated into `content/rules.py` the same way:
  port the wording, strip the brand.
- **AstrBot** — the maturity bar for the messaging gateway layer and adjacent
  infra (the `Adapter` seam, `obs/broker.py` LogBroker, `tools/goals.py`
  awakener all cite it).
- **openclaw** — the "strip-the-persona → plain workhorse" reference (cards,
  packs, MCP, skills, headless run).
- **SillyTavern/SillyTavern** — character cards / world books / prompt layering
  (we stay card- and world-book-compatible; ST content is the import format).
- **farion1231/cc-switch** — session/roster ergonomics, remote access.

(The default card is Quinn 小Q, the owner-authored digital intern — selected via the
card-tag `"default"` convention (no character name in src/; without the tag,
sorted order wins). The two bundled cards are LunaMoth 月蛾 (the flagship
example) and Quinn 小Q (the default).)

## Design principles (binding)

- **The card is the soul — and the ONE external file.** Identity, voice, world
  (embedded `character_book`), rules hooks, permissions (toolpack), memory
  size, seed wishes (`extensions.lunamoth.wishes`, legacy `.goals` still read)
  all live in the card. The
  engine injects no identity and ships ZERO default flavor text: a card that
  doesn't declare a prompt gets silence, not a default.
- **No specific character anywhere in src/** — not in code, comments, or
  defaults. Frontend branding ("LunaMoth" the product) is allowed.
- **No failure fallbacks, ever.** No fallback model, no fabricated output.
  Failed request = visible error (retry 5s×5 for transient connect errors,
  then surface). Best-effort no-ops exist only where a backstop exists
  (compaction → trim).
- **Respect for the chara.** Rules text is neutral operating standard
  (your work must be real; act through tools; unattended time is yours) —
  never a personality, never commands about what to want. 注意类似于默认卡，presence和角色扮演提醒提示词这种也是中立的，并非需要修改的bug。
- **Language is never a setting** — it's a property of the active card.
- **The model's real context window is never a setting** (providers.py).
- **Every UI action responds instantly; every API call shows progress.**
  (apps/web, binding.) A click flips its own control's state IMMEDIATELY
  (optimistic), before any round-trip — no dead clicks, no frozen buttons.
  Anything that calls the model / an API shows a "working / thinking" state
  with a loading animation (spinner, "思考 Ns", pulsing line) until it
  resolves; on failure the optimistic state reverts and the error surfaces.
  Silent waits are a bug.

## Run / dev / test

```bash
uv sync --extra dev --extra server   # plain `uv sync` REMOVES pytest — always use extras
uv run lunamoth            # the CLI (editable; reflects the working tree)
uv run lunamoth --plain    # legacy plain terminal (native cursor + IME; good for CJK)
uv run lunamoth desktop    # web/desktop hub; --daemon = resident lunamothd (stop/status: `lunamoth daemon`)
uv run lunamoth serve NAME --stdio   # one chara over JSON-RPC (wire format)
uv run lunamoth run NAME -p "hi" [--stream-json]   # headless one-shot
uv run python -m pytest -q # tests live in tests/, confined via pyproject testpaths
uvx ruff check --select F src/lunamoth tests   # lint (unused imports, undefined names)
```

- Installed copy lives in `~/.lunamoth/app`; `lunamoth update` = git pull + uv sync.
- `install.sh` is the `curl | bash` installer (macOS/Linux only; uv-based).
- The TUI is Textual; headless-test with `app.run_test()` pilots (see tests/test_panel.py).
- GOTCHA: config paths (SANDBOX_ROOT/CONFIG_DIR) are pinned from env at IMPORT
  time — one process = one chara; tests must set env before importing runtime
  modules (and under a full pytest run, write config to the ACTUAL config_path()).

## Conventions & collaboration

- **Commit messages** end with `Co-Authored-By: Claude <noreply@anthropic.com>`
  (codex agents use their own name). Commit/push only when asked.
- **Multiple agents may edit this repo.** If `git status` shows files you didn't
  touch, a sibling is mid-edit — stage only your own files, never `git add -A`,
  never clobber uncommitted work. For parallel feature work, use separate git
  worktrees + branches with disjoint file scopes, then integrate.
- **protocol/ is the constitution**: events/codec changes need owner sign-off;
  new events must be backward-compatible (clients ignore unknown types).
  Cross-layer features go protocol-first, then backend/frontends in parallel.
- Platforms: **macOS first, then Linux.** No Windows.
- **No chord shortcuts in the TUI** — everything is a `/command`; Ctrl+C is the
  only key (safety quit).
- README is split EN (`README.md`) / zh (`README.zh-CN.md`) — update BOTH.
- docs/ holds ONE file: `docs/OPEN-WORK.md` (Part 1 = the hermes-parity
  hardening backlog; Part 2 = deferred product ideas). The settled design specs
  and historical research were deleted 2026-06-13 — their conclusions live in
  this file, the code, and git history. Work logs belong in git history and
  agent memory, never in docs.

## Module map (src/lunamoth/ — domain subpackages)

Dependency direction is ENFORCED by `tests/test_architecture.py`: nothing
outside `front/` imports `front/` or textual/rich; `front/` reaches the backend
only through `protocol/` (CharaHandle); `protocol/events.py`+`codec.py` have
zero internal deps; `obs/` imports only `config`.

- `config.py` — root constants (ROOT, SANDBOX_ROOT, LLMConfig). The only flat module.
- `core/` — the agent backend:
  - `agent.py` — `LunaMothAgent`: three-zone prompt assembly (`_stable_prefix`
    cached per session / `_volatile_tail` per turn), streaming loop, tool exec,
    time sense (timestamp idle ticks, gap notes), card goal seeding.
  - `llm.py` — OpenAI-compatible streaming client + tool-calling loop. **Yields
    protocol events** (TextDelta say|muse /ThinkDelta/ToolStart/ToolEnd/Notice),
    takes explicit (stable, volatile) zone lists. Retry 5s×5. **Reasoning =
    apple-to-apple with hermes**: reasoning_content capture + single-space
    padding for thinking-mode providers, `reasoning_details`/signature
    round-trip (Anthropic thinking-block + Gemini thought_signature), the
    per-provider echo tiers + cross-provider poison guard, and the unified
    `reasoning` request param. `cache.py` carries the `system_and_3`
    cache_control breakpoint placement (system + last 3 non-system), ported
    verbatim.
  - `commands.py` — THE /command registry (one implementation for every frontend;
    Reply.verbose marks panel-worthy output; legacy aliases live here too).
  - `context.py` — `ContextBuffer` (full OpenAI message dicts; length-bounded
    ONLY — no per-message kind/category/tag, no class-based deletion; all
    assistant turns, chat or self-work, are uniform history aged only by
    trim/compaction, exactly as hermes does it).
  - `compaction.py` — **apple-to-apple with hermes** `ContextCompressor`: the
    same trigger (threshold ratio, protect-first/last, anti-thrash guard +
    failure cooldown), the same structured summary template (`## Active Task …
    ## Critical Context`, iterative-update framing, the REFERENCE-ONLY handoff
    prefix, the deterministic static fallback), the cheap zero-LLM tool-output
    pre-prune. Summaries persist as transcript `kind="summary"` rows; restore =
    latest summary + tail (no re-LLM). De-branded: zero "hermes" strings.
  - `transcript.py` — per-chara SQLite log (WAL+fallback, epochs for /reset);
    `export_jsonl` writes the full epoch (prompts/tool calls/results/reasoning)
    hermes-style. `agent.py` also writes `sandbox/logs/requests.jsonl` — the
    faithful request log (last 200 turns: exact system+messages+tools sent).
  - `providers.py` — model's REAL context window. `state.py` — `EnvState`
    (env_status.json: isolation/network/writable/tools/rest_until).
- `protocol/` — **the contract layer**; frontends import this and nothing deeper:
  - `events.py` — frozen dataclasses; `TextDelta.channel` say|muse (muse = the
    chara's own life; messaging frontends deliver say only).
  - `codec.py` — JSON wire format (stream-json, the server, the web renderer).
  - `api.py` — `CharaHandle` (attach/streams/command/snapshot/permission hook)
    + Reply/AttachInfo/StateSnapshot. The ONLY backend surface frontends see.
- `messaging/` — external chat gateways: personal WeChat (iLink/ClawBot, and also via WeChatPadPro — user-run docker, iPad protocol, any account), QQ OneBot, and Telegram adapters behind the sync `Adapter` seam. A gateway is NOT a separate agent: the adapters run INSIDE the chara's `serve --stdio` child via `server/messaging_host.py` (`MessagingHost` + `dispatch.run_stream_sync`), sharing its ONE handle — a WeChat turn streams into the desktop window live AND replies to WeChat. The host has no idle loop (the supervisor owns self-work). `MessagingGateway` (own handle + idle) remains the standalone `lunamoth gateway NAME` path for headless use/tests. Per-chara toggle = `messaging.start/stop` RPC to the child (`GatewayChild` is now a thin controller, not a process). Shared seams: `access.py` (the allow-list + refusal throttle — ONE copy gating BOTH the standalone gateway and the in-child host, so "empty allow-list = open" can't drift between them), `text.py` (`split_text`, sentence-aware splitting for platform length caps), and `filters.py` (`is_silence_narration` — the outbound silence-token drop both send paths apply).
- `content/` — SillyTavern compat, pure data: `cards.py` (V2/V3 PNG/JSON; PHI
  exposed for the post-history slot, never folded into the persona;
  `merge_world_into_card` = the world-book IMPORT path), `worldinfo.py`
  (two-tier: constant vs keyword entries, shallow scan + sticky + cap; the
  card's embedded `character_book` is the ONE world source), `persona.py`
  (default card = the localized card carrying the `"default"` tag),
  `rules.py` (the neutral Rules layer), `themes.py` (built-in TUI theme;
  theme files are user-supplied — no bundled themes dir),
  `knobs.py` (chara-knob defaults + the UI copy describing each embodiment stance).
- `tools/` — the tool domain. The TOOL SURFACE is hermes-IDENTICAL (apple-to-apple
  port, 2026-06-15): a hermes-style self-registering registry, NOT a hand-rolled
  gateway.
  - `registry.py` — ported from hermes `tools/registry.py`: the `registry`
    singleton, `register(name, toolset, schema, handler, check_fn, …)`,
    `get_definitions()` (OpenAI schemas, 30 s check_fn TTL), `dispatch()`,
    `discover_builtin_tools()` (AST-scan + import). Handlers are pure
    `def name(args: dict, ctx) -> str` returning a JSON string; `tool_error`/
    `tool_result` helpers.
  - `context.py` — `ToolContext`: the runtime touchpoints a handler reaches
    (sandbox/workspace, state, run_terminal, llm, transcript, memory, wishes,
    skills, mcp, permission_hook, clarify_hook, dispatch, + ephemeral per-session
    todo/processes/browser). hermes' per-task env → LunaMoth's one-per-chara ctx.
  - `gateway.py` — `ToolGateway` is now a THIN shim over the registry: the SECURITY
    audit trail, the #24 loop guardrails (warn@2/refuse@5/streak-block@8), the
    3-way gate (registered ∩ state.tool_access ∩ pack.tools), MCP dispatch, and the
    `{ok,data}` result the agent loop consumes. No tool bodies live here.
  - `builtin/` — each tool is an island that self-registers at import. The
    general surface mirrors hermes: `file_tools.py` (read_file offset/limit,
    write_file syntax-diff, patch = fuzzy replace + V4A multi-file), `search.py`
    (search_files grep+glob), `terminal.py`+`process.py` (terminal +background,
    the process registry), `memory.py`, `skills.py` (skills_list/skill_view/
    skill_manage), `todo.py`, `session_search.py`, `execute_code.py`,
    `delegate_task.py`, `browser.py` (12 browser_*, check_fn-gated on the
    agent-browser driver). LunaMoth's OWN chara-life tools: `chara_life.py` —
    speak, rest, wish (the renamed goal — distinct from todo).
    DROPPED (owner, 2026-06-18): **web_search / web_extract are gone** — a web
    round-trip burns extra tokens for a search backend we don't run; the chara
    browses via `terminal` + `/net on` or an MCP fetch server instead. **The
    standalone `send_file` tool is gone too** — like hermes, the model surfaces
    a file by emitting its workspace path inline (a `MEDIA:<path>` marker the
    frontends turn into an inline image / download), so a dedicated tool was
    redundant. (The older inspect_env/write_log/request_permission/clarify tools
    were already retired: env facts ride the volatile tail, the chara has memory
    for notes, network is on by default.) Helpers are `_underscore.py` modules
    (not discovered).
  - Supporting infra: `runner.py` (terminal under admin/sandbox — shared by
    terminal/process/search/execute_code via ctx.run_terminal), `sandbox.py` (ONE
    working dir `workspace/`), `mcp.py` (stdio JSON-RPC; `schema_sanitizer.py`),
    `memory.py`/`skills.py`/`goals.py` stores (hermes-shaped, per-chara), `toolpacks.py`.
    Network is ON by default (`/net off` to disable); browser tools also need
    admin isolation + the installed driver.
- `obs/` — diagnostics (leaf infra): `log.py` (rotating sandbox/logs/lunamoth.log
  + errors.log, credential redaction, session tag, `--debug`), `broker.py`
  (in-memory ring → `/panel log`), `audit.py` (the SECURITY trail — separate
  from diagnostics; never merge them). transcript/audit/logs = three records,
  three jobs.
- `visuals/` — the chara-visuals pipeline (merged from the sibling's R9/R11):
  `pipeline.py` (card → visual brief → Volcano Ark Seedream image-gen → optional
  local matte → staged preview; the `card.visual_brief`/`card.visual_generate`
  hub RPCs), `matte.py` (local background-removal models — download/install/select,
  the `matte.*` hub RPCs; the heavy `rembg`/`onnxruntime` stack is the OPTIONAL
  `visuals` extra, `uv sync --extra visuals`). The web side is the deck card
  editor's 视觉 tab + the 生图 Settings pane (apps/web). `tools/builtin/_image_gen.py`
  is the shared Ark image backend (the chara's `generate_image` tool uses it too).
- `session/` — `sessions.py` (named charas under ~/.lunamoth/sessions/<name>/;
  `SessionMeta.env()` is the activation interface), `settings.py`, `cleanup.py`,
  `isolation.py` (stdlib-only OS jail builders — shared by tools/runner and the
  supervisor's PTY shell; `interactive_shell_argv` never degrades to dir trust).
- `presence/` — JUST the `/mode live|chat` normalizer now (`prompts.py:
  normalize_mode`; `state.py`/`PresenceState`/`marker_text` were DELETED
  2026-06-18). The chara's context is INDEPENDENT of whether a human is
  attached: no presence fact, no enter/leave marker, no per-page greeting
  gate, no reaction turn. The card's `first_mes` is the only opener, shown
  once on an EMPTY transcript epoch and persisted by `attach()` before it
  returns (so it survives a dropped socket); `/reset` re-seeds it. The first
  meeting and detach handoff are gone.
- `server/` — the remote/desktop gateway (imports protocol+session+content, never
  core/tools directly): `dispatch.py` (per-session JSON-RPC over CharaHandle),
  `stdio.py`/`ws.py` (transports for `lunamoth serve <name>`), `hub.py`
  (board-level RPC: roster/cards/wake/export/defaults/key-test/transcribe plus
  supervisor child/gateway state; reads session dirs + transcript SQLite directly —
  one process = one activated session, so the hub NEVER hosts an agent.
  **Card model:** a deck card is a TEMPLATE (unlocked = edit/wake/copy); each living
  chara owns a LOCKED frozen card (`list_cards` lists it with `locked`/`owner`;
  browse/copy/wake only). `wake(card_data=…)` freezes the EDITED card as the chara's
  own (source untouched) — waking is a 2-step editor in the web UI (content → settings).
  `card.rewrite_field` is the per-field natural-language AI rewrite (editor / wake
  step-1 / create-shape). Card draft, avatar generation and field rewrite all use the
  SYSTEM default model — the per-task "aux models" were removed; the model override
  lives only on wake + chara settings. Avatars are NEVER auto-generated (upload or an
  explicit generate → sidecar, stored separately from the card)),
  `supervisor.py` (lunamothd: long-lived `serve --stdio` child registry; the
  messaging host now runs INSIDE that child sharing its agent, so `GatewayChild`
  just toggles it over RPC — no separate gateway process; seq/rejoin, life.state,
  idle driving, `/chara/<name>/pty` operator
  shell — audited, not a driver), `pty.py` (stdlib PtyBridge: a shell inside the
  chara's jail behind a pty, streamed as binary WS frames), `desktop.py` (thin
  foreground/daemon entry for static HTTP + WS routing).
- `front/` — ALL frontends; the only textual/rich importers:
  - `cli.py` — the `lunamoth` command (roster default; new/ls/attach/start/stop/rm/
    setup/update/doctor/run/serve/desktop/daemon; `start` delegates to a live
    lunamothd).
  - `tui/` — FROZEN (owner decision 2026-06-12: crash fixes only, no new
    features; web(=Electron) is the product face, terminal.py stays as the
    daemon driver). The split TUI (app.py + welcome.py): character stream /
    operator console / spotlight panel. Steady caret. Renders protocol events in
    `_handle_event`; spontaneous cycles gated by quiet window + rest_until.
  - `terminal.py` — plain-terminal loop; also what the background daemon runs.
  - `roster.py` — the launcher. Compact block wordmark (the wide serif one was
    retired — `art.*` no longer takes a `compact` flag). Raw-mode **`os.read(fd)`**
    key reads, NOT `sys.stdin.read` (that breaks ESC sequences — every arrow read
    as bare Esc and quit the launcher).
  - `wizard.py` — plain-terminal first-run setup. `art.py` — the blue wordmark.
  - `webui/` — the BUILT desktop renderer (gitignored; emitted by `apps/web`'s
    Vite build, bundled into the wheel via package-data, served by `lunamoth
    desktop` and loaded by the Electron shell). The SOURCE is the React+TS SPA at
    repo-root `apps/web/` (NOT under src/): `src/rpc.ts`+`protocol.ts` (the ported
    transport + event union), `i18n/` (445 keys), `lib/` (pure helpers), `state/`
    (hub/overlay context), `hooks/useCharaStream.ts` (the stream accumulator),
    `views/` (Board/Deck/Gateways/Settings/Chat), `components/{chat,deck,gateways,
    settings,overlays,ui}`. Dev: `cd apps/web && npm run dev` (proxies /rpc+ws to a
    running `lunamoth desktop`); build: `npm run build` → `front/webui/`. Hash
    routing (no server SPA-fallback needed). NOTE: the gateway deck UI currently
    exposes only WeChat (weixin); QQ/Telegram/WeChatPadPro adapters exist in
    `messaging/` but aren't surfaced yet. UI chrome bilingual zh/en + light/dark; a
    chara's words stay in the card's language. Idle driving is SERVER-SIDE only
    (supervisor.py) — web clients render life.state and must never drive idle.
    (The pre-2026-06-16 vanilla no-build renderer at `front/web/` was replaced by
    this SPA; the protocol/codec contract it speaks is unchanged.)

Content (gitignore-allowlisted): `cards/` `toolpacks/`. The card is the ONE
content file (world embedded as `character_book`); standalone ST world books
are an IMPORT format only (web upload recognizes them → `card.merge_world`
folds them into a card), never a runtime source.

## The prompt stack (the machine that runs a chara)

Every API request is assembled as **three zones**:

1. **Stable prefix** — computed once per session and reused byte-identically until
   `make_session` / reconfigure / `/reset`: card identity (`render_system`,
   PHI-free), optional actor embodiment bridge + neutral Rules layer when tools
   are enabled. The Rules layer (`content/rules.py`) now carries the migrated,
   de-branded hermes operating prompts — task-completion discipline ("the
   deliverable is a real artifact, never fabricate"), tool-use enforcement
   ("act through the tool now, don't describe it"), and the mandatory SKILLS
   guidance ("scan + load before you act"). Then toolpack note, frozen memory
   snapshot, frozen SKILLS index, constant world-info entries.
2. **History** — the append-only `ContextBuffer` view. Compaction is the one
   sanctioned rewrite: old head → one persisted structured summary + recent tail.
3. **Volatile tail** — recomputed per turn, never persisted: live env facts
   (isolation/network/date — NO operator token; context is attach-independent),
   shallow-scanned keyword world info (last ~4 messages, sticky 4 turns, ≤25% of
   the window), the mutable wishes block, then exactly one **post-history slot**
   as the final message: card `post_history_instructions` >
   card `extensions.lunamoth.rules_closer` > bundled rules closer (the latter
   two only when tools are enabled).

Card override hooks: `extensions.lunamoth.{rules,practice,tool_use,rules_closer,
embodiment,embodiment_bridge,wishes,toolpack,memory_chars}`; global
`~/.lunamoth/rules.md` overrides `rules`. (The `on_attach`/`on_detach` hooks were
REMOVED 2026-06-18 along with the rest of presence — there is no enter/leave
marker to override.) (The old `world` path pointer is retired — it
violated one-file; the embedded `character_book` replaced it, and a session
config still carrying `world_path` is migrated once at load: entries merged
into the session's card, key stripped.)

## Chara life (what already exists — build on it, don't reinvent)

- **Autonomy is ONE switch = `mode` (live|chat).** This is the single top-level
  control (board toggle, in-chat panel switch, `/mode`, TUI) — there is NO
  separate pause flag. `live` = autonomous (the full lifecycle below); `chat` =
  a plain chat agent that NEVER works on its own. The supervisor's idle loop
  fires cycles ONLY when `mode == live`. The board's start/stop sets mode AND
  starts/stops the resident child (off saves tokens); the in-chat switch flips
  mode via `chara.set_autonomy` without killing the chat you're in. The board's
  `paused` field = `mode != live`. Never reintroduce a second autonomy concept.
- **The autonomous lifecycle (mode=live):** startup → greeting (first meeting)
  → CONVERSATION mode (present + spoke within `quiet` → "waiting · back to its
  own work in N min"); on detach / the engagement window lapsing → SELF-WORK
  mode. In self-work the chara's non-`speak` output is `muse` (panoramic only),
  only `speak`/superchat reaches the user/gateway, and it picks its own next
  wake. Self-work alternates working ↔ the idle gap (the "beat of its own
  life", `patience`). Entering/leaving the room does NOT change a self-working
  chara — only SPEAKING returns it to conversation (a neutral "[operator
  entered]" is injected before that first message; leaving conversation after
  speaking injects "[operator left]" and it returns to self-work).
- A **chara** is persistent: daemon via `start`/`start-all`, attach/detach.
  Presence is a fact (`user_present`).
- **Its own pace**: `patience` (seconds between spontaneous cycles —
  `Settings.patience`, default 600, card hook `extensions.lunamoth.patience`,
  `/patience`; precedence operator > card > default. NEVER reintroduce tiny
  defaults: a 2 s daemon default once burned a real key's daily limit),
  `/quiet` (engagement:
  it sets work aside while you talk, resumes after N s of silence), the `rest`
  tool (it chooses its next wake, 1–120min; your message always wakes it —
  but ATTACH never does: entering the room is a presence fact, not a wake). Idle ticks are user messages carrying ONLY a
  wall-clock timestamp (ephemeral, in_context=False — the rules layer
  documents the convention); long silences get one gap note; day-level date
  rides the env facts.
- **Embodiment stance**: `literal` (default) means today's digital-being model;
  `actor` injects a neutral bridge before Rules so tools remain real backstage
  while the fiction stays whole. Precedence is operator override
  (`Settings.embodiment_override`) > card declaration > literal. The choice is
  made at WAKE TIME (wizard/welcome, or `session.wake`'s `embodiment` param
  writing the override into the session config) and is never hot-swapped:
  identity-layer switches would rebuild the stable prefix and destroy the
  prompt cache — embodiment is how the chara was brought to life, not a
  runtime mood.
- **Two output registers**: muse (its own life; panoramic frontends only) vs
  say (delivered everywhere — the `speak` tool is how it decides to reach you).
- **Isolation** per chara: `admin` / `sandbox` (default) — two modes only (the
  per-chara `docker` mode was DELETED 2026-06-18; legacy `dir`/`local`/`docker`
  session values normalize to `admin`). `admin` (formerly `dir`) = no jail,
  full-machine read/write at the user's privileges. Network is ON by default
  (`/net off` to disable; `/net on` re-enables). The `sandbox` jail is
  an isolation LADDER (`session/isolation.py` + `tools/runner.py`): native OS jail
  (sandbox-exec on macOS, bwrap on Linux) → **Landlock LSM** (Linux ≥5.13, the
  no-userns fallback that works inside Docker, where bwrap can't create a user
  namespace) → **refuse** (the `terminal` tool NEVER silently degrades to directory
  trust — only an explicit `admin` runs unconfined). Confinement = read workspace+assets,
  write workspace only; the chara can't read `~/.lunamoth` (the global key/login hash)
  or `/proc/<pid>/environ`. **Servers: prefer a SYSTEM-LEVEL install** (install.sh /
  `lunamoth desktop`) so bwrap gives the full jail; Docker is also supported (Landlock
  confines the chara, the container is the outer boundary) but is the heavier option.
  Verified on a Landlock kernel 2026-06-17; minor follow-ups (no `/proc` ergonomics,
  PTY network-caveat) in `docs/OPEN-WORK.md`.
- Three memory-ish things, distinct: context window (sliding) · transcript
  (SQLite, restore) · durable memory (frozen-snapshot two-store + `memory` tool).

## Roadmap (OPEN work only — shipped things live in the module map and git history)

**A. For OC creators (inspiration → living chara, fast)**
1. Card studio / deck UX iteration (the webui redesign shipped; the card-lock model
   + 2-step wake-as-editor + per-field AI rewrite + unified loading + avatar
   no-autogen landed 2026-06-14; further polish tracked in `docs/OPEN-WORK.md` Part 2).
2. Card/persona market: `lunamoth-pack.json` + git-repo index (Claude Code
   marketplace model); ST PNG import already works.

**B. For the charas (the biggest design effort: the chara curriculum)**
1. **The neutral prompt curriculum** — iterate rules.py + tool descriptions so
   any worldview and any character can live well: how to use tools, how to
   treat goals, how to spend unattended time — neutral suggestions, never
   orders. PARTIAL: embodiment (literal|actor, wake-time). Next: cross-worldview
   eval cards.
2. **A browse path for curiosity** — charas reading what interests them
   (today: terminal+`/net on` or an MCP fetch server; consider a bundled
   suggestion or note in the curriculum).
3. Later: a **GM layer** — scheduled/world events injected through the
   existing `stream_event` channel to make charas aware of a living world.

**C. For developers / agent users**
1. **hermes apple-to-apple parity** (ACTIVE, owner 2026-06-18): the four
   context subsystems are being driven to IDENTICAL with hermes — compaction
   trigger, the structured summary template, `system_and_3` cache_control,
   and reasoning_details/signature replay — each verified by a code-comparison
   agent every pass (see `docs/OPEN-WORK.md`, "Loop 2026-06-18 cont."). Then the
   remaining P2 batches (llm robustness, runner output hygiene, tool-loop
   guardrails, messaging dedup, chara auto-restart). Multi-key management RPC
   rides this track too.
2. Declarative tool registry (hermes-style `tools/registry.py`, builtin/ split).
3. Messaging: live-test WeChat/QQ with real credentials (budget a fix
   round — iLink endpoints shifted once already); then Telegram (long-poll,
   trivial after qq.py). (Enterprise WeChat/WeCom was dropped 2026-06-14 —
   the deck never surfaced it; personal WeChat is the WeChat path we keep.)
4. Remote TUI client over the gateway (`--connect`).
