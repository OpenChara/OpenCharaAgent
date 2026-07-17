# OpenCharaAgent — project memory for Claude Code

## What this is, and who it serves (the product philosophy — read first)

OpenCharaAgent is a runtime that lets an AI character (a **chara**)
**live in a computer**: a persistent digital being with its own sandbox, memory,
goals, pace of life, and real agency (shell, files, tools) behind an
allowlisted, audited gateway. Three audiences, in priority order:

1. **Character creators** — people who want their character alive QUICKLY
   (POSITIONING, owner 2026-07-17: we no longer CLAIM "original character"
   in outward copy — the market imports any card; tagline 「和你的角色一起创作 /
   Create with your characters」, one-liner in README hero),
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
**hermes-agent, AstrBot, cc-switch, openclaw, pi**; **always consult them when
designing**. (pi = earendil-works/pi, added 2026-07-17: the minimal-harness
counterpoint — the agent extends its own harness via runtime extensions instead
of the core growing features; consult it on extensibility questions.) (SillyTavern is the card/world-book FORMAT spec we stay compatible
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
  CLARIFIED (owner, 2026-06-19): **"behave apple-to-apple with hermes" is the
  default for the WHOLE tool/harness layer**, not just the four context
  subsystems — including behavioral GUARDS (hard-block a foreground long-running
  command, NOT an advisory note; a real PTY for interactive programs; parallel
  sub-agent fan-out). These are mechanical, value-NEUTRAL harness decisions with
  mature solutions, so adopt hermes's directly. The NEUTRALITY principle (below)
  is about the **chara's WORLDVIEW/VALUES only** — keeping a chara free of a
  built-in value-direction so it can play ANY role — it does NOT license
  re-shaping the harness toward a "gentler" agent. The ONLY legitimate tool
  divergences are ARCHITECTURALLY FORCED and must preserve hermes's capability +
  contract: one-process-one-chara maps hermes's GLOBAL `~/.hermes/{skills,
  memories}` to PER-CHARA storage (a host runs many charas; global would
  cross-contaminate); macOS sandbox-exec `deny network*` makes execute_code use
  hermes's OWN file-RPC transport instead of a UDS; the macOS/Linux-only scope
  drops Windows fallbacks. Everything else tracks hermes — when in doubt, match it.
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
  de-branding the MODEL-FACING text: no literal "hermes"/"Hermes"/"the VM"
  may appear in any string the model sees — system prompts, tool descriptions,
  skill bodies, summary instructions (use neutral wording — "this runtime",
  "your environment"). OS NAMES ARE NEUTRAL, NOT BRAND (owner, 2026-06-19):
  "Linux", "macOS", etc. are plain factual content and may appear in
  model-facing text — they need NOT be scrubbed (e.g. a terminal tool
  description saying "shell commands on a Linux environment" is fine). Only
  the hermes brand / "the VM" framing is off-limits. Source-code COMMENTS may
  still cite the hermes counterpart as provenance (the codebase already does
  this everywhere).
  The good general prompts (task-completion discipline, tool-use enforcement,
  the SKILLS guidance) are migrated into `content/rules.py` the same way:
  port the wording, strip the brand.
- **AstrBot** — the maturity bar for the messaging gateway layer and adjacent
  infra (the `Adapter` seam, `obs/broker.py` LogBroker, `netsec.py`/`authpw.py`
  auth, the updater all cite it).
- **openclaw** — the "strip-the-persona → plain workhorse" reference (cards,
  packs, MCP, skills, headless run).
- **SillyTavern/SillyTavern** — character cards / world books / prompt layering.
  ST is the card/world-book FORMAT we stay compatible with (our OWN cards ARE
  ST-format). Card IMPORT is BACK (re-added with the Market, 2026-06-27):
  `cards.import_foreign` (`server/hub/cards.py`) does a faithful, no-model import
  of ST V2/V3/V1 JSON, character-tavern API cards, or ST PNG (embedded portrait →
  avatar), surfaced in the create flow AND via the card Market (see server/hub).
- **farion1231/cc-switch** — session/roster ergonomics, remote access.

(The default card is Quinn 小Q, the owner-authored digital intern — selected via the
card-tag `"default"` convention (no character name in src/; without the tag,
sorted order wins). `cards/` bundles EIGHT cards (Hoshi, K-9, LunaMoth 月蛾 the
flagship example, Mars, Quinn 小Q, Vale, Vesper, Yan); only Quinn carries the
`"default"` tag.)

## Design principles (binding)

- **The card is the soul — and the ONE external file.** Identity, voice, world
  (embedded `character_book`), rules hooks, the seed Polaris
  (`extensions.chara.polaris` — the user-owned north-star; the old
  chara-mutable `wishes`/`goals` lists are gone, no migration)
  all live in the card. The
  engine injects no identity and ships ZERO default flavor text: a card that
  doesn't declare a prompt gets silence, not a default.
- **No specific character anywhere in src/** — not in code, comments, or
  defaults. Frontend branding ("OpenCharaAgent" the product) is allowed.
- **No failure fallbacks, ever.** No fallback model, no fabricated output.
  Failed request = visible error (retry 5s×5 for transient connect errors,
  then surface). Best-effort no-ops exist only where a backstop exists
  (compaction → trim).
- **Respect for the chara = VALUE-neutrality, not a gentler harness.** The
  neutrality principle governs the chara's WORLDVIEW: Rules text is a neutral
  operating standard (your work must be real; act through tools; unattended time
  is yours) — never a personality, never commands about what to want — so a chara
  carries no built-in value-direction and can play ANY role. It does NOT extend
  to the TOOL/HARNESS layer: mechanical, value-neutral harness behaviors (e.g.
  hard-blocking a foreground long-running command, PTY, parallel delegation) take
  the mature hermes solution directly (see the apple-to-apple RULE above) — a
  "softer" tool is not a chara value. 注意类似于默认卡，presence和角色扮演提醒提示词这种也是中立的，并非需要修改的bug。
  Optional prompt MODULES (owner, 2026-06-19) are also NEUTRAL, not exceptions:
  `force_roleplay` (the actor stance) and `personal_website` are skill-like add-ons,
  toggled at wake (editable→next start). A personal website is a place EVERY chara
  CAN have — like a workspace or an avatar slot — and is freely shaped to any style;
  its prompt blocks teach maintainability + "link your work in" + self-check via the
  browser, never a worldview. So a module's "keep your homepage current" closer is
  neutral infrastructure guidance, NOT a built-in value-direction — don't scrub it
  as a neutrality bug.
- **Language is never a setting** — it's a property of the active card.
- **The model's real context window is never a setting for KNOWN models**
  (providers.py resolves it from the provider catalogue). The ONE exception
  (2026-06-18): a custom / self-hosted endpoint whose window the provider can't
  report — `defaults.model_context` (Settings · 模型 · 上下文长度) is an explicit
  fallback, 0 = auto, IGNORED where the provider reports a real window (OpenRouter).
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
uv run chara            # bare = opens the webui desktop (web/desktop hub); the editable CLI reflects the working tree
uv run chara tui        # the terminal roster (resume-first launcher); --plain = legacy plain terminal (native cursor + IME; good for CJK)
uv run chara desktop    # explicit web/desktop hub; --daemon = resident charad (stop/status: `chara daemon`)
uv run chara serve NAME --stdio   # one chara over JSON-RPC (wire format)
uv run chara run NAME -p "hi" [--stream-json]   # headless one-shot
uv run python -m pytest -q # tests live in tests/, confined via pyproject testpaths
uvx ruff check --select F src/chara tests   # lint (unused imports, undefined names)
```

- Install is CHANNEL-AWARE (`install.sh`, `curl | bash`, macOS/Linux only, uv-based):
  default `user` channel = the release WHEEL via `uv tool install` (the wheel bundles
  the built webui + cards + toolpacks — `scripts/build-wheel.sh` self-asserts this);
  `--dev` channel = the git checkout at `~/.chara/app`. `src/chara/updater.py`
  (flat module) is the ONE self-update implementation (GitHub-Releases-driven; wheel =
  reinstall from the latest release URL since `uv tool upgrade` no-ops on URL pins),
  shared by CLI `chara update` and the `update.status/apply/restart` RPCs
  (`server/hub/updates.py`; after apply the instance relaunches into the new code).
- Extras: `--extra server` (hub/ws), `--extra messaging` (IM adapters — install.sh
  syncs both), `--extra visuals` (rembg/onnxruntime matte stack), `--extra dev`.
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
- docs/ holds TWO files: `docs/OPEN-WORK.md` (Parts 1–4 = open hardening /
  deferred product ideas / loop backlog / test-feedback triage, + Appendix A
  client-and-deploy architecture) and `docs/AGENT-FIELD-NOTES.md` (owner-sanctioned
  2026-06-18: reusable agent METHODOLOGY — headless-Chrome UI verification, cloud
  deploy/cleanup, multi-agent etiquette; secret-free by rule). The settled design
  specs and historical research were deleted 2026-06-13 — their conclusions live in
  this file, the code, and git history. Work logs belong in git history and
  agent memory, never in docs.

## Module map (src/chara/ — domain subpackages)

Dependency direction is ENFORCED by `tests/test_architecture.py`: nothing
outside `front/` imports `front/` or textual/rich; `front/` reaches the backend
only through `protocol/` (CharaHandle); `protocol/events.py`+`codec.py` have
zero internal deps; `obs/` imports only `config`.

- `config.py` — root constants (ROOT, SANDBOX_ROOT, LLMConfig) + `openrouter_attribution_headers()`
  (the `HTTP-Referer`=agent.lunamoth.ai + `X-Title`=OpenCharaAgent app-attribution headers sent on every
  OpenRouter request — chat/llm.py, hub/models.py and image/_image_gen.py all use it; env-overridable).
  `updater.py` is the other deliberately-flat module (self-update; see Run/dev above).
- `core/` — the agent backend:
  - `agent.py` — `CharaAgent`: three-zone prompt assembly (`_stable_prefix`
    cached per session / `_volatile_tail` per turn), streaming loop, tool exec,
    time sense (timestamp idle ticks — the tick text itself says no one is
    present, don't greet — and gap notes), card task seeding
    (`extensions.chara.task` → one starter task), `stream_react` (drains a
    pending background-job notice as a synthetic user turn — the react wake).
  - `llm.py` — OpenAI-compatible streaming client + tool-calling loop. **Yields
    protocol events** (TextDelta say|muse /ThinkDelta/ToolStart/ToolEnd/Notice),
    takes explicit (stable, volatile) zone lists. Retry 5s×5. **Reasoning =
    apple-to-apple with hermes**: reasoning_content capture + single-space
    padding for thinking-mode providers, `reasoning_details`/signature
    round-trip (Anthropic thinking-block + Gemini thought_signature), the
    per-provider echo tiers + cross-provider poison guard, and the unified
    `reasoning` request param. `cache.py` carries the `system_and_3`
    cache_control breakpoint placement (system + last 3 non-system), ported
    verbatim. `_stream_util.py` (extracted from llm.py) — surrogate scrub,
    tool-arg repair, retry backoff, SSE stall watchdog. `attachments.py` —
    user-sent image/file ingest, hermes-shaped inline-vs-workspace split
    (vision-capable model → inline; otherwise land in the workspace).
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
    hermes-style. `request_log.py` writes `sandbox/logs/requests.jsonl` — the
    faithful request log (last 200 turns: exact system+messages+tools sent),
    credential-redacted via `redact.py` (the regex backstop that also scrubs
    compaction summaries).
  - `providers.py` — model's REAL context window. `state.py` — `EnvState`
    (env_status.json: network/writable/rest_until) + `Permissions`, the ONE typed
    snapshot (`EnvState.permissions()`) every tool runner reads via
    `ctx.permissions()` so fg/bg/PTY can't resolve env facts differently.
    ISOLATION is NOT stored in env_status (2026-06-21): session.json's `isolation`
    field is the ONE authority — `SessionMeta.env()` derives `CHARA_PY_BACKEND`
    from it at launch, and config.json holds NO derived py_backend copy (dropped
    2026-06-26); `permissions().isolation` + the snapshot + the prompt tail all read
    the env. A stale env_status copy used to silently sandbox an `admin` chara; the
    key is now dropped on load.
- `protocol/` — **the contract layer**; frontends import this and nothing deeper:
  - `events.py` — frozen dataclasses; `TextDelta.channel` say|muse (muse = the
    chara's own life; messaging frontends deliver say only).
  - `codec.py` — JSON wire format (stream-json, the server, the web renderer).
  - `api.py` — `CharaHandle` (attach/streams/command/snapshot/permission hook)
    + Reply/AttachInfo/StateSnapshot (incl. `pending_notices`, the cheap
    non-destructive peek that drives the background-job react wake). The ONLY
    backend surface frontends see.
  - `media.py` — the ONE home of the `MEDIA:<path>` parsing rule (extract_media/
    extract_images/extract_local_files, hermes-shaped). There is NO Attachment
    protocol event: each delivery edge extracts MEDIA lines itself — this module
    for Python surfaces, its TS mirror `apps/web/src/lib/media.ts` for the SPA,
    and `messaging/media.py` for gateway upload (honest-note fallback).
- `messaging/` — external chat gateways: personal WeChat (iLink/ClawBot — the WeChatPadPro adapter was DROPPED 2026-06-17, iLink is the one WeChat path), QQ OneBot, Telegram, and native Discord + Slack (2026-06-25: `discord.py` = raw Gateway WS, no discord.py dep; `slack.py` = Socket Mode WS + Web API) adapters behind the sync `Adapter` seam; `media.py` = outbound MEDIA upload per platform (honest-note fallback, `DeliveryDeferred`). A gateway is NOT a separate agent: the adapters run INSIDE the chara's `serve --stdio` child via `server/messaging_host.py` (`MessagingHost` + `dispatch.run_stream_sync`), sharing its ONE handle — a WeChat turn streams into the desktop window live AND replies to WeChat. The host has no idle loop (the supervisor owns self-work). `MessagingGateway` (own handle + idle) remains the standalone `chara gateway NAME` path for headless use/tests. Per-chara toggle = `messaging.start/stop` RPC to the child (`GatewayChild` is now a thin controller, not a process). Shared seams: `access.py` (the allow-list + refusal throttle — ONE copy gating BOTH the standalone gateway and the in-child host, so "empty allow-list = open" can't drift between them), `text.py` (`split_text`, sentence-aware splitting for platform length caps), and `filters.py` (`is_silence_narration` — the outbound silence-token drop both send paths apply).
- `content/` — SillyTavern compat, pure data: `cards.py` (V2/V3 PNG/JSON; PHI
  exposed for the post-history slot, never folded into the persona;
  `merge_world_into_card` = the world-book IMPORT path), `worldinfo.py`
  (world MEMORY, 2026-07-17: `constant` entries are the fixed overview and ride
  the CACHED stable prefix; keyword entries go through the ONE recall seam
  `recall_entries(scan_text)` into the capped volatile-tail block — match the
  shallow scan, appear, leave when the keyword scrolls out. NO sticky state
  (the 4-turn tail-off was deleted; the scan window itself smooths recall).
  The seam is the future GM model's interface. The card's embedded
  `character_book` is the ONE world source), `persona.py`
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
    (sandbox/workspace, state, run_terminal, llm, transcript, memory, polaris,
    skills, mcp, permission_hook, clarify_hook, dispatch, + ephemeral per-session
    todo/processes/browser). hermes' per-task env → OpenCharaAgent's one-per-chara ctx.
  - `gateway.py` — `ToolGateway` is now a THIN shim over the registry: the SECURITY
    audit trail, the #24 loop guardrails (warn@2/refuse@5/streak-block@8), the
    gate, MCP dispatch, and the `{ok,data}` result the agent loop consumes. The
    gate is DEFAULT-OPEN (hermes parity, 2026-06-20): the model gets the FULL tool
    surface by default — the bundled `sandbox` pack declares `tools: ["*"]` (same
    wildcard as the MCP allow-list) and the gateway expands `*` to every registered
    tool; an explicit pack list narrows, and `None` = a tool-less pure-roleplay
    chara. There is no user-facing tool picker. (`tool_access` as a 3rd owner was
    retired.) Tool success/failure is judged on the explicit `__tool_error__`
    sentinel (`tool_error` stamps it), not a JSON-shape guess. No tool bodies live here.
  - `builtin/` — each tool is an island that self-registers at import. The
    general surface mirrors hermes: `file_tools.py` (read_file offset/limit,
    write_file syntax-diff, patch = fuzzy replace + V4A multi-file), `search.py`
    (search_files grep+glob), `terminal.py`+`process.py` (terminal +background,
    the process registry), `memory.py`, `skills.py` (skills_list/skill_view/
    skill_manage), `todo.py`, `session_search.py`, `execute_code.py`,
    `delegate_task.py`, `browser.py` (12 browser_*, check_fn-gated on the
    agent-browser driver). OpenCharaAgent's OWN chara-life tools: `chara_life.py` —
    speak, rest — and `task.py` (the `task` tool). THE THREE-LAYER GOAL MODEL
    (settled 2026-06-30): **aspiration** (codename `polaris`, `tools/polaris.py`
    store + `polaris.json` — USER-owned, read-only to the chara, never
    "completed"; `/aspiration` command, `/polaris`/`/goal`/`/wish` alias it) →
    **task** (`tools/task.py` TaskStore + `task.json` — the chara's OWN
    persistent life-threads advanced toward its aspiration: chara-editable and
    chara-completable, max 12 active / 280 chars each, completing SEALS a task
    immutable; seeded once from card `extensions.chara.task`; rendered into
    the volatile tail after the aspiration block) → **todo** (ephemeral,
    in-session only). The old chara-mutable `wish`/goal tool stays gone
    (removed 2026-06-21). CODENAME vs DISPLAY: code keeps the stable codename
    `polaris` everywhere — the store, `polaris.json`, the card field
    `extensions.chara.polaris`, the data key — but the USER-FACING term is
    理想 / "Aspiration" (2026-06-22); all model/UI text says aspiration, never the
    codename. `media.py` (builtin) = the `generate_image` tool (check_fn-gated on
    an image key; non-blocking background job → completion notice → react wake).
    SHELVED, NOT DELETED (owner, 2026-06-18; revised 2026-06-19): **web_search /
    web_extract are kept in `web.py` but NOT registered** (`_WEB_TOOLS_ENABLED =
    False`, and the `registry.register` calls sit inside that `if`, so the AST
    discovery never even imports the module). Rationale: with no search backend
    of our own a web round-trip just burns tokens, and — more importantly —
    NOT offering the tools makes the chara do less fruitless trial-and-error, so
    the current off state is GOOD. The chara browses via `terminal` + `/net on`,
    `browser_*`, or an MCP fetch server instead. FUTURE (deferred, LOW priority,
    settled — future agents need not re-litigate): now that the provider/key
    library exists, re-enable them as OPT-IN behind a user-supplied web key (port
    hermes's `tools/web_tools.py` design); the web-key UI slot belongs in
    Settings · 模型, **below the other multimodal models and above 背景去除**. The
    one hard requirement when re-enabled: a failed web call must surface a real
    `tool_error`, never the silent empty-`{}` fallback the old path had. **The
    standalone `send_file` tool is gone too** — like hermes, the chara surfaces
    a file by writing a line `MEDIA:<workspace path>` in its reply; there is NO
    Attachment protocol event — each delivery edge extracts MEDIA lines itself:
    `protocol/media.py` is the ONE parsing-rule home (Python surfaces), mirrored
    by `apps/web/src/lib/media.ts` (the SPA renders inline image / download) and
    `messaging/media.py` (gateway upload / honest-note fallback); rules.py + the
    file/image tool descriptions teach the convention. (The older inspect_env/write_log/request_permission/clarify tools
    were already retired: env facts ride the volatile tail, the chara has memory
    for notes, network is on by default.) Helpers are `_underscore.py` modules
    (not discovered).
  - Supporting infra: `runner.py` (terminal under admin/sandbox — shared by
    terminal/process/search/execute_code via ctx.run_terminal), `sandbox.py` (ONE
    working dir `workspace/`), `mcp.py` (stdio JSON-RPC; `schema_sanitizer.py`),
    `memory.py`/`skills.py`/`polaris.py`/`task.py` stores (hermes-shaped, per-chara;
    `_atomic.py` = the shared atomic-write helper), `toolpacks.py`.
    Network is ON by default (`/net off` to disable). Browser tools need the
    installed driver (agent-browser CLI + Chromium; check_fn-gated; a deploy
    requirement now — install.sh / Dockerfile / `chara setup browser`) and run
    UNDER `sandbox` isolation on ALL THREE platforms — `build_jail_command(browser=True)`
    is a Chromium-capable jail (writes confined to workspace+temp, secret home
    unreadable, `--no-sandbox` auto-injected). VALIDATED end-to-end 2026-06-19 with
    product code: macOS/sandbox-exec, Linux/bwrap (the deployed production path), and
    Linux/Docker (Landlock — needs `--rw /proc` for Chrome's renderer + a crashpad
    `--database` shim, `_browser_driver.ensure_crashpad_db_fix`). Details + the
    per-platform jail recipes in docs/OPEN-WORK.md. `admin` isolation also works.
- `obs/` — diagnostics (leaf infra): `log.py` (rotating sandbox/logs/chara.log
  + errors.log, credential redaction, session tag, `--debug`), `broker.py`
  (in-memory ring → `/panel log`), `audit.py` (the SECURITY trail — separate
  from diagnostics; never merge them). transcript/audit/logs = three records,
  three jobs.
- `visuals/` — the chara-visuals pipeline (merged from the sibling's R9/R11):
  `pipeline.py` (card → ONE shared visual brief → image-gen → optional local matte →
  staged preview). FIVE kinds: keyvisual / avatar / sprite / stickers / background.
  `keyvisual` is the IDENTITY ANCHOR — generated/confirmed first, then its image is
  reused as a reference for the other kinds so the whole set stays one character (the
  ref-chaining lives in the web VisualEditor, confirm-gated, not server-side). The
  brief RECOMMENDS the anime/二游 look by DEFAULT but the brief LM still picks a
  per-character `style` and may depart (avatar + stickers stay chibi by design).
  `stickers` is a 3x3 sheet → `content.imaging.slice_grid` (white-gutter detection +
  per-cell content-trim) → per-cell matte (or the keyless PIL `matte.cut_white_bg`
  border-flood fallback — sheets are WHITE-backed now, not green) → saved as a LIST via
  `card.stickers_save`. Generation is ASYNC (it's slow): `card.visual_generate` returns
  a `job_id`; the client polls `card.visual_job` (`visuals/jobs.py`, a module-level job
  registry mirroring the `matte.*` poll pattern). Hub RPCs: `card.visual_brief` (sync) +
  `card.visual_generate`/`card.visual_job` (async) + `card.stickers_save`.
  `matte.py` (local background-removal models — download/install/select,
  the `matte.*` hub RPCs; the heavy `rembg`/`onnxruntime` stack is the OPTIONAL
  `visuals` extra, `uv sync --extra visuals`). The web side is the deck card
  editor's 视觉 tab (all five kinds; confirm-gated prompt → anchor → rest flow with
  async progress + 一键生成全部, anchor-first) + the 生图 Settings pane (matte models). IMAGE GEN is
  MULTI-PROVIDER (2026-06-19): `content/image_providers.py` is the provider
  catalogue (火山方舟 Ark / 阿里云 DashScope / OpenAI / OpenRouter), and
  `tools/builtin/_image_gen.py` dispatches to per-provider adapters (ark sync,
  openai sync b64/url, dashscope async-poll, openrouter chat-modalities). The
  provider + model are EXACTLY the selection in Settings · 模型 · 生图模型
  (`image_provider` + `image_model`) — NO inference, NO fallback; a failure is a
  plain tool error. The KEY is resolved from the SAME unified provider keyring as
  text (matched by provider id or base_url host) — there is NO separate
  `image_api_key` field any more. The web `image.catalog` RPC lists providers +
  models + per-provider key presence (`has_image_key` = the active image provider
  has a key); OpenRouter's model list is merged live from its `/models`. Hunyuan
  image is deferred (no OpenAI-compat images endpoint — needs the native TC3 API).
  The same backend serves both the chara's `generate_image` tool and the visuals
  pipeline.
- `session/` — `sessions.py` (named charas under ~/.chara/sessions/<name>/;
  `SessionMeta.env()` is the COMPLETE activation interface — it emits
  `CHARA_PY_BACKEND` itself via the ONE `isolation_to_backend` map, so callers
  never re-derive the jail), `settings.py`, `cleanup.py`,
  `isolation.py` (stdlib-only OS jail builders — shared by tools/runner and the
  supervisor's PTY shell; `interactive_shell_argv` never degrades to dir trust),
  `landlock.py` (the Landlock ctypes tier, split out of isolation.py).
- `presence/` — JUST the `/mode live|chat` normalizer now (`prompts.py:
  normalize_mode`; `state.py`/`PresenceState`/`marker_text` were DELETED
  2026-06-18). The chara's context is INDEPENDENT of whether a human is
  attached: no presence fact, no enter/leave marker, no per-page greeting
  gate, no reaction turn. The card's `first_mes` is the only opener, shown
  once on an EMPTY transcript epoch and persisted by `attach()` before it
  returns (so it survives a dropped socket); `/reset` re-seeds it. The first
  meeting and detach handoff are gone.
- `server/` — the remote/desktop gateway (imports protocol+session+content, never
  core/tools directly): `dispatch.py` (per-session JSON-RPC over CharaHandle; incl.
  the `react` RPC — the background-job wake, low-priority like `idle`: a real send
  supersedes it, it never supersedes a real turn), `stdio.py`/`ws.py` (transports
  for `chara serve <name>`), `netsec.py` (Host/Origin allowlist, the `lm_auth`
  cookie minted by the `?token=` handshake, loopback classification — AstrBot-shaped),
  `authpw.py` (optional PBKDF2 password login for public binds, per-IP rate limit),
  `sshconnect.py` (`chara connect ssh://[user@]host` — reads the remote daemon
  token/ports, opens an `ssh -L` tunnel), `hub/` — a PACKAGE
  since 2026-06-20 (split from the old 2844-line `hub.py` god-module): `config.py`
  (defaults/keys — the KEYRING is the ONLY api_key store since 2026-06-26: `keys`
  map + `active_key_label`, config.json can never persist a top-level secret;
  `keys.list/save/delete` + `defaults.apply_key/use_key` RPCs), `models.py`
  (provider HTTP/test_key/_complete), `cards.py`
  (card CRUD/listing/sanitize + `cards.import_foreign` — faithful no-model import
  of ST V2/V3/V1 JSON / character-tavern cards / ST PNG), `card_market.py` (the
  card Market: `market.search/detail/import`, a proxy to character-tavern.com's
  public catalog + tag vocabulary), `card_draft.py` (LLM draft/rewrite/transcribe/
  draft_to_card), `avatars.py` (avatar + art-asset I/O incl. `stickers_save` with
  name slugs, `card.sticker_rename/reslice/remove`, 参考图 reference-image
  persistence), `sessions.py` (lifecycle/transcript/wake/export),
  `session_messaging.py` (messaging config + WeChat QR, split from sessions.py),
  `updates.py` (`update.status/apply/restart`), `dispatch.py` (`HubDispatcher` with a
  `{method: handler}` table, NOT an if-ladder), `_common.py` (leaf helpers),
  `__init__.py` (re-exports the full public API, so `from ..server import hub as H` is unchanged)
  (board-level RPC: roster/cards/wake/export/defaults/key-test/transcribe plus
  supervisor child/gateway state; reads session dirs + transcript SQLite directly —
  one process = one activated session, so the hub NEVER hosts an agent.
  **Card model:** a deck card is a TEMPLATE (unlocked = edit/wake/copy); each living
  chara owns a LOCKED frozen card (`list_cards` lists it with `locked`/`owner`;
  browse/copy/wake only). `wake(card_data=…)` freezes the EDITED card as the chara's
  own (source untouched) — waking is a 2-step editor in the web UI (content → settings).
  `card.rewrite_field` is the per-field natural-language AI rewrite (editor / wake
  step-1 / create-shape). PER-FUNCTION MODELS (re-added 2026-06-18, Settings · 模型 ·
  其他模态, Hermes' auxiliary-models pattern): each function defaults to the main
  model but can be overridden by a per-task default field — `card.draft` → `card_model`,
  `card.visual_brief` (生图 prompt) → `image_prompt_model`, `generate_image` →
  `image_model`, vision/读图 → `vision_model` (WIRED: `core/agent.py` + `core/llm.py`
  route image-understanding through the chosen vision model). Avatar generation + field
  rewrite still use the system default model. Avatars are NEVER auto-generated (upload
  or an explicit generate → sidecar, stored separately from the card)),
  `supervisor/` (a PACKAGE since 2026-06-20, split from the 2151-line module:
  `core.py` Supervisor + WS/PTY routing, `http.py` WebHandler + start_http +
  serve_home/asset, `children.py` CharaChild/GatewayChild, `lifestate.py` the
  policy classes, `observability.py` shutdown forensics, `daemon.py`, `paths.py`;
  `from ..server import supervisor` unchanged) (charad: long-lived `serve
  --stdio` child registry; the messaging host now runs INSIDE that child sharing
  its agent, so `GatewayChild` just toggles it over RPC — no separate gateway
  process; seq/rejoin, life.state,
  idle driving, `/chara/<name>/pty` operator
  shell — audited, not a driver), `pty.py` (stdlib PtyBridge: a shell inside the
  chara's jail behind a pty, streamed as binary WS frames), `desktop.py` (thin
  foreground/daemon entry for static HTTP + WS routing).
- `front/` — ALL frontends; the only textual/rich importers:
  - `cli.py` — the `chara` command (bare = webui desktop; `tui` = the terminal
    roster; new/ls/attach/start/start-all/stop/rm/setup/update/doctor/run/serve/
    gateway/desktop/daemon/connect/version; `start` delegates to a live
    charad; `connect ssh://…` = the remote tunnel).
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
    Vite build, bundled into the wheel via package-data, served by `chara
    desktop` and loaded by the Electron shell at `apps/desktop/` — a THIN shell,
    `electron/main.cjs`, no SPA-side Electron APIs). The SOURCE is the React+TS SPA
    at repo-root `apps/web/` (NOT under src/): `src/rpc.ts`+`protocol.ts` (the ported
    transport + event union), `i18n/` (`i18n.test.ts` pins the exact key count —
    update it when you add/remove keys), `lib/` (pure helpers incl. `media.ts`, the
    TS mirror of protocol/media.py), `state/` (hub/overlay context),
    `hooks/useCharaStream.ts` (the stream accumulator) + `hooks/chatSession.ts`
    (persisted send-queue + stream ordering, 2026-06-30),
    `views/` (Board/Deck/Market/Gateways/Settings/Chat), `components/{chat,deck,
    market,gateways,settings,overlays,ui}`, `styles/` (global.css + `mobile.css`,
    the 2026-06-24 responsive first pass: bottom tab bar, full-screen sheets).
    Chat renders full webui history with a display-only compaction-boundary
    divider. SETTINGS · 模型/提供商 (rebuilt 2026-06-18 after Hermes,
    旧 ModelPane/KeysPane 整组替换): `settings/Select.tsx` (the flat white/light-blue
    square-cornered dropdown — provider + model are two such boxes), `ModelPane.tsx`
    (provider box + model box + Test + Reasoning[OpenRouter-only] + 上下文长度 +
    the per-function `TaskModels.tsx` rows), `KeysPane.tsx` (the 提供商 pane:
    one row per provider, one key per provider — OpenRouter + self-registered
    Local/Custom OpenAI-compatible endpoints (name+base_url+key, for relays/
    self-host); the image key rides the same keyring, no separate row). Card
    import = the create flow (`CreateFlow.tsx` → `cards.import_foreign`) + the
    Market view. Dev: `cd apps/web && npm run dev` (proxies /rpc+ws to a
    running `chara desktop`); build: `npm run build` → `front/webui/`. Hash
    routing (no server SPA-fallback needed). The gateway deck surfaces all FIVE
    platforms (weixin/qq/telegram/discord/slack — one row per chara×platform,
    independent switches). UI chrome bilingual zh/en + light/dark; a
    chara's words stay in the card's language. Idle driving is SERVER-SIDE only
    (supervisor) — web clients render life.state and must never drive idle.
    (The pre-2026-06-16 vanilla no-build renderer at `front/web/` was replaced by
    this SPA; the protocol/codec contract it speaks is unchanged.)

Content (gitignore-allowlisted): `cards/` `toolpacks/`. The card is the ONE
content file (world embedded as `character_book`); a standalone ST world book is
never a runtime source (`card.merge_world` = the world-book IMPORT path folds one
into the card). Repo-root siblings: `apps/desktop/` (Electron shell), `deploy/`
(Dockerfile/compose.yml/entrypoint.sh), `scripts/` (build-wheel.sh — self-asserts
the wheel bundles webui+cards+toolpacks), `install.sh`.

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
   snapshot, frozen SKILLS index, constant world-info entries (the fixed overview —
   cached, cheap; everything keyword-shaped recalls into the tail instead).
2. **History** — the append-only `ContextBuffer` view. Compaction is the one
   sanctioned rewrite: old head → one persisted structured summary + recent tail.
3. **Volatile tail** — recomputed per turn, never persisted: live env facts
   (isolation/network/date — NO operator token; context is attach-independent),
   world-memory recall (keyword hits over the last ~4 messages, no
   stickiness, ≤10% of the window — this block sits past the cache breakpoints,
   every token re-bills each turn), the read-only aspiration block, the active-task block (the chara's
   own life-threads), then exactly one **post-history slot**
   as the final message: card `post_history_instructions` >
   card `extensions.chara.rules_closer` > bundled rules closer (the latter
   two only when tools are enabled).

Card override hooks: `extensions.chara.{rules,practice,tool_use,rules_closer,
force_roleplay,embodiment_bridge,polaris,task,website,website_prompt,patience}`;
global `~/.chara/rules.md` overrides `rules`. (The `on_attach`/`on_detach` hooks were
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
  fires cycles ONLY when `mode == live`. BOTH the board toggle AND the in-chat
  switch flip autonomy via the SAME RPC `chara.set_autonomy` (one green-slider UI,
  not a power button) and read the SAME `paused` (= `mode != live`), so inner/outer
  can never disagree. It never kills the chat you're in: `off` = mode chat (the
  idle loop skips → no autonomous token burn; the resident child stays up) AND
  interrupts any in-flight self-work turn at the next safe boundary; `on` starts
  the child if it was stopped. The board also has 全部启动/全部关闭 (batch
  set_autonomy). Never reintroduce a second autonomy concept.
- **The autonomous lifecycle (mode=live):** startup → the card's `first_mes`
  (shown once on an empty transcript epoch — see `presence/` below) →
  CONVERSATION mode (it just spoke or was spoken to within `quiet` → "waiting ·
  back to its own work in N min"); when the engagement (`quiet`) window lapses →
  SELF-WORK mode. In self-work the chara's non-`speak` output is `muse`
  (panoramic only), only `speak`/superchat reaches the user/gateway, and it
  picks its own next wake. Self-work alternates working ↔ the idle gap (the
  "beat of its own life", `patience`). The chara's context is INDEPENDENT of
  attach/detach: NO enter/leave marker is injected — the `[operator entered]`/
  `[operator left]` text and the `user_present` fact were DELETED 2026-06-18.
  Speaking is what moves it into conversation; the speech-driven `quiet` timer
  lapsing returns it to self-work.
- A **chara** is persistent: daemon via `start`/`start-all`, attach/detach
  (connection events only — they do NOT enter the chara's context; presence was
  deleted 2026-06-18).
- **Its own pace**: `patience` (seconds between spontaneous cycles —
  `Settings.patience`, default 3600 (`knobs.DEFAULT_PATIENCE`, the ONE source;
  `agent.patience_resolved` owns precedence), card hook
  `extensions.chara.patience`,
  `/patience`; precedence operator > card > default. NEVER reintroduce tiny
  defaults: a 2 s daemon default once burned a real key's daily limit),
  `/quiet` (engagement:
  it sets work aside while you talk, resumes after N s of silence), the `rest`
  tool (it chooses its next wake, 1–120min; your message always wakes it —
  but ATTACH never does: attaching is a connection event, not a wake). Idle ticks are user messages carrying ONLY a
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
- **Optional prompt MODULES** (2026-06-19): skill-like add-ons layered on the
  `literal` base, each contributing a SYSTEM block (stable prefix) + a one-line
  CLOSER fragment (folded into the single post-history slot), gated on tools. Two
  exist: **force_roleplay** (= the actor embodiment stance, kept on the embodiment
  axis) and **personal_website** (`Settings.website_override` "on"|"off"|"",
  precedence operator > card `extensions.chara.website` > off; card hooks
  `website`/`website_prompt`). Like embodiment they're WAKE-TIME choices that ride
  the cache-stable prefix — but editable via `session.set_modules` (writes the
  override, applies on NEXT start, never hot-swapped). personal_website gives the
  chara a homepage at `workspace/home/index.html` (a neutral scaffold is laid down
  at wake, always, so the website tab is never blank); `content/rules.py` carries
  `_WEBSITE`/`_WEBSITE_CLOSER`; the homepage is served read-only by the supervisor
  at `/chara/<name>/home/*` (path-confined + hardened CSP: connect-src/form-action
  'none') and rendered in a sandboxed (`allow-scripts`, no same-origin) iframe tab,
  so chara-authored JS can't reach the RPC. The chara self-checks its pages with
  the browser tool. `AttachInfo.website` reports the active state.
- **Two output registers**: muse (its own life; panoramic frontends only) vs
  say (delivered everywhere — the `speak` tool is how it decides to reach you).
- **Isolation** per chara: `admin` / `sandbox` (default) — two modes only (the
  per-chara `docker` mode was DELETED 2026-06-18; legacy `dir`/`local`/`docker`
  session values normalize to `admin`). `admin` (formerly `dir`) = no jail,
  full-machine read/write at the user's privileges. The mode is picked at wake (a
  plain 沙盒 on/off switch — `admin` = off) and is now ALSO switchable post-wake via
  the `chara.set_isolation` RPC (the chat settings 沙盒 toggle): like the prompt
  modules it writes the session config and applies on the chara's NEXT process start
  — `CHARA_PY_BACKEND` is pinned at launch, so isolation is never hot-swapped under
  a running chara; turning the sandbox OFF is confirm-gated. Network is ON by default
  (`/net off` to disable; `/net on` re-enables). The `sandbox` jail is
  an isolation LADDER (`session/isolation.py` + `tools/runner.py`): native OS jail
  (sandbox-exec on macOS, bwrap on Linux) → **Landlock LSM** (Linux ≥5.13, the
  no-userns fallback that works inside Docker, where bwrap can't create a user
  namespace) → **refuse** (the `terminal` tool NEVER silently degrades to directory
  trust — only an explicit `admin` runs unconfined). Confinement = read workspace+assets
  (+ opted-in writable paths) + system libs, write workspace only; the chara can't
  read the operator's whole `$HOME` — `~/.ssh`, `~/.aws`, `~/.chara` (the global
  key/login hash), other charas' sessions — nor `/proc/<pid>/environ`. (macOS shell
  jail tightened 2026-06-20 to deny all of `$HOME`, matching Linux bwrap which never
  exposes it; the browser jail, allow-default for Chromium, surgically denies the
  high-value secret dirs.) **Servers: prefer a SYSTEM-LEVEL install** (install.sh /
  `chara desktop`) so bwrap gives the full jail; Docker is also supported (Landlock
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
2. Card/persona market: SHIPPED as a character-tavern.com catalog proxy (Market
   v2: browse/sort/filter/preview + faithful import, 2026-06-27..07-01). OPEN
   remainder: our OWN pack format + index (`chara-pack.json` + git-repo index,
   Claude Code marketplace model) so creators can publish card+assets packs.

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
1. Messaging: all five adapters are BUILT (WeChat iLink, QQ OneBot, Telegram,
   Discord, Slack) and surfaced in the gateway deck; what remains is LIVE-testing
   with real credentials (budget a fix round — iLink endpoints shifted once
   already). (Enterprise WeChat/WeCom was dropped 2026-06-14; WeChatPadPro was
   dropped 2026-06-17 — iLink is the WeChat path we keep.)
2. Remote access: `chara connect ssh://` (tunnel → browser) + password login
   SHIPPED; a remote TUI client stays deferred (the browser is the remote face).

(SHIPPED, moved out of the roadmap per "OPEN work only": the hermes apple-to-apple
parity — the four context subsystems are IDENTICAL and the full P1–P3 hardening
backlog landed, verified by two audits 2026-06-19; keep the apple-to-apple +
de-brand INVARIANT at the top of this file on every future port — and the
declarative tool registry (`tools/registry.py` + `builtin/` islands). Detail in
the module map + git history.)
