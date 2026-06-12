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

It is the synthesis of three projects — clone them under `reference/`
(gitignored) and **always consult them when designing**:

- **NousResearch/hermes-agent** — the most important. Agent runtime, context
  management, prompt-cache discipline, skills, plugin/registry patterns.
- **SillyTavern/SillyTavern** — character cards / world books / prompt layering
  (we stay card- and world-book-compatible; ST content is the import format).
- **farion1231/cc-switch** — session/roster ergonomics, remote access.

(History: began as an SCP-079 fan recreation, long since generalized; SCP is
mentioned only in license/acknowledgements. The default card is Quinn 小Q,
the owner-authored digital intern — selected via the card-tag `"default"`
convention (landing with the cards-one-file wave; until then filename order
still picks LunaMoth). LunaMoth 月蛾 stays bundled as the flagship example.)

## Design principles (binding)

- **The card is the soul — and the ONE external file.** Identity, voice, world
  (embedded `character_book`), rules hooks, permissions (toolpack), memory
  size, seed goals (`extensions.lunamoth.goals`) all live in the card. The
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
  never a personality, never commands about what to want.
- **Language is never a setting** — it's a property of the active card.
- **The model's real context window is never a setting** (providers.py).

## Run / dev / test

```bash
uv sync --extra dev --extra server   # plain `uv sync` REMOVES pytest — always use extras
uv run lunamoth            # the CLI (editable; reflects the working tree)
uv run lunamoth --plain    # legacy plain terminal (native cursor + IME; good for CJK)
uv run lunamoth desktop    # web/desktop hub (serves front/web + per-chara gateways)
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
- docs/ root holds ONLY the active cross-machine handover/task files (one
  per running track; delete once absorbed). Settled design specs and research
  live under `docs/archive/` (`context-design.md` = the prompt machine,
  `design.md` + `supervisor.md` = web/desktop). Work logs belong in git
  history and agent memory, never in docs.

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
    takes explicit (stable, volatile) zone lists. Retry 5s×5; reasoning policy
    (OpenRouter-only unified param; echo-back for DeepSeek).
  - `commands.py` — THE /command registry (one implementation for every frontend;
    Reply.verbose marks panel-worthy output; legacy aliases live here too).
  - `context.py` — `ContextBuffer` (full message dicts; THINK_WINDOW pruning).
  - `compaction.py` — Hermes-style summary compaction; summaries persist as
    transcript `kind="summary"` rows; restore = latest summary + tail (no re-LLM).
  - `transcript.py` — per-chara SQLite log (WAL+fallback, epochs for /reset).
  - `providers.py` — model's REAL context window. `state.py` — `EnvState`
    (env_status.json: isolation/network/writable/tools/rest_until).
- `protocol/` — **the contract layer**; frontends import this and nothing deeper:
  - `events.py` — frozen dataclasses; `TextDelta.channel` say|muse (muse = the
    chara's own life; messaging frontends deliver say only).
  - `codec.py` — JSON wire format (stream-json, the server, the web renderer).
  - `api.py` — `CharaHandle` (attach/streams/command/snapshot/permission hook)
    + Reply/AttachInfo/StateSnapshot. The ONLY backend surface frontends see.
- `messaging/` — external chat gateways: WeCom, personal WeChat iLink/ClawBot, and QQ OneBot adapters behind the sync `Adapter` seam.
- `content/` — SillyTavern compat, pure data: `cards.py` (V2/V3 PNG/JSON; PHI
  exposed for the post-history slot, never folded into the persona), `worldinfo.py`
  (two-tier: constant vs keyword entries, shallow scan + sticky + cap),
  `persona.py`, `rules.py` (the neutral Rules layer), `themes.py` (TUI skins).
- `tools/` — the tool domain: `gateway.py` (`ToolGateway`, allowlisted dispatch,
  `call(name, /)` positional-only), `runner.py` (terminal under dir/sandbox/docker),
  `sandbox.py`, `mcp.py` (stdio JSON-RPC client), `skills.py` (SKILL.md +
  create_skill self-improvement), `goals.py`, `memory.py` (frozen-snapshot
  two-store), `toolpacks.py`. Chara-life tools: `speak` (deliver to the user),
  `rest` (self-paced wake, 1–120min), `request_permission` (presence-gated).
- `obs/` — diagnostics (leaf infra): `log.py` (rotating sandbox/logs/lunamoth.log
  + errors.log, credential redaction, session tag, `--debug`), `broker.py`
  (in-memory ring → `/panel log`), `audit.py` (the SECURITY trail — separate
  from diagnostics; never merge them). transcript/audit/logs = three records,
  three jobs.
- `session/` — `sessions.py` (named charas under ~/.lunamoth/sessions/<name>/;
  `SessionMeta.env()` is the activation interface), `settings.py`, `cleanup.py`.
- `presence/` — attach/detach awareness + `/mode live|chat`.
- `server/` — the remote/desktop gateway (imports protocol+session+content, never
  core/tools directly): `dispatch.py` (per-session JSON-RPC over CharaHandle),
  `stdio.py`/`ws.py` (transports for `lunamoth serve <name>`), `hub.py`
  (board-level RPC: roster/cards/wake/export/defaults/key-test/transcribe plus
  supervisor child/gateway state; reads session dirs + transcript SQLite directly —
  one process = one activated session, so the hub NEVER hosts an agent),
  `supervisor.py` (lunamothd: long-lived `serve --stdio` child registry, gateway
  supervision, seq/rejoin, life.state, idle driving), `desktop.py` (thin
  foreground/daemon entry for static HTTP + WS routing).
- `front/` — ALL frontends; the only textual/rich importers:
  - `cli.py` — the `lunamoth` command (roster default; new/ls/attach/start/stop/rm/
    setup/update/doctor/run/serve/desktop; daemon helpers).
  - `tui/` — the split TUI (app.py + welcome.py): character stream / operator
    console / spotlight panel. Steady caret. Renders protocol events in
    `_handle_event`; spontaneous cycles gated by quiet window + rest_until.
  - `terminal.py` — plain-terminal loop; also what the background daemon runs.
  - `roster.py` — the launcher. Compact block wordmark (do NOT switch to the wide
    serif one). Raw-mode **`os.read(fd)`** key reads, NOT `sys.stdin.read` (that
    breaks ESC sequences — every arrow read as bare Esc and quit the launcher).
  - `wizard.py` — plain-terminal first-run setup. `art.py` — the blue wordmark.
  - `web/` — the desktop renderer (no build step: index.html/style.css/i18n.js/
    rpc.js/app.js), a pure protocol client served by `lunamoth desktop`. Design:
    `docs/archive/design.md`. UI chrome bilingual zh/en + light/dark; a chara's
    words stay in the card's language. Idle driving MUST keep terminal.py's
    gating (quiet window + rest_until) or it burns tokens nonstop.

Content (gitignore-allowlisted): `characters/` `worlds/` `toolpacks/` `themes/`.

## The prompt stack (the machine that runs a chara — full spec: docs/archive/context-design.md)

Every API request is assembled as **three zones**:

1. **Stable prefix** — computed once per session and reused byte-identically until
   `make_session` / reconfigure / `/reset`: card identity (`render_system`,
   PHI-free), optional actor embodiment bridge + neutral Rules layer when tools
   are enabled, the static tool-use nudge, toolpack note, frozen memory snapshot,
   frozen SKILLS index, constant world-info entries.
2. **History** — the append-only `ContextBuffer` view. Compaction is the one
   sanctioned rewrite: old head → one persisted summary + recent tail.
3. **Volatile tail** — recomputed per turn, never persisted: live env facts
   (isolation/network/operator/date), shallow-scanned keyword world info
   (last ~4 messages, sticky 4 turns, ≤25% of the window), the mutable goals
   block, then exactly one **post-history slot** as the final message:
   card `post_history_instructions` > card `extensions.lunamoth.rules_closer`
   > bundled rules closer (the latter two only when tools are enabled).

Card override hooks: `extensions.lunamoth.{rules,rules_closer,embodiment,
embodiment_bridge,tempo,goals,world,toolpack,memory_chars,on_attach,on_detach}`;
global `~/.lunamoth/rules.md`.

## Chara life (what already exists — build on it, don't reinvent)

- A **chara** is persistent: daemon via `start`/`start-all`, attach/detach.
  Presence is a fact (`user_present`); `/mode live|chat` is how it behaves
  while watched.
- **Its own pace**: `patience` (seconds between spontaneous cycles),
  `/quiet` (engagement: it sets work aside while you talk, resumes after
  N s of silence), the `rest` tool (it chooses its next wake, 1–120min; your
  message always wakes it). Idle ticks are user messages carrying ONLY a
  wall-clock timestamp (ephemeral, in_context=False — the rules layer
  documents the convention); long silences get one gap note; day-level date
  rides the env facts.
- **Tempo**: a chara's time-flow rate is card-declarable
  (`extensions.lunamoth.tempo`) and operator-overridable (`/tempo`,
  `Settings.tempo`; precedence operator > card > 1.0). It scales ONLY the pause
  between spontaneous cycles (`patience ÷ tempo`), never `/quiet` or `rest`.
- **Embodiment stance**: `literal` (default) means today's digital-being model;
  `actor` injects a neutral bridge before Rules so tools remain real backstage
  while the fiction stays whole. Precedence is operator override
  (`Settings.embodiment_override`, `/embodiment`) > card declaration > literal.
- **Two output registers**: muse (its own life; panoramic frontends only) vs
  say (delivered everywhere — the `speak` tool is how it decides to reach you).
- **Isolation** per chara: `dir` / `sandbox` (default; sandbox-exec/bwrap) /
  `docker`; network runtime-toggleable (`/net on`), `request_permission` while
  you're present.
- Three memory-ish things, distinct: context window (sliding) · transcript
  (SQLite, restore) · durable memory (frozen-snapshot two-store + `memory` tool).

## Roadmap (organized by audience; ordered within each)

**A. For OC creators (inspiration → living chara, fast)**
1. **AI-assisted card creation** in the desktop deck: prose inspiration → draft
   card (persona, greeting, world entries, seed goals) + a small generated SVG
   avatar + theme color — every field user-editable before save; the human
   keeps full authorship. (The web deck's create flow is the seed.)
2. Card/persona market: `lunamoth-pack.json` + git-repo index (Claude Code
   marketplace model); ST PNG import already works.

**B. For the charas (the biggest design effort: the chara curriculum)**
1. **The neutral prompt curriculum** — iterate rules.py + tool descriptions so
   any worldview and any character can live well: how to use tools, how to
   treat goals, how to spend unattended time — neutral suggestions, never
   orders. PARTIAL: embodiment shipped as `literal|actor` with a neutral bridge
   for actor stance; the rest of the curriculum remains open.
2. **Tempo** — SHIPPED as card hook `extensions.lunamoth.tempo`, Settings
   override + `/tempo`, and frontend scheduling (`patience ÷ tempo`) without
   changing quiet/rest.
3. **A browse path for curiosity** — charas reading what interests them
   (today: terminal+`/net on` or an MCP fetch server; consider a bundled
   suggestion or note in the curriculum).
4. Later: a **GM layer** — scheduled/world events injected through the
   existing `stream_event` channel to make charas aware of a living world.

**C. For developers / agent users**
1. Declarative tool registry (hermes-style `tools/registry.py`, builtin/ split).
2. Messaging adapters: Telegram first (AstrBot Platform pattern; deliver the
   say channel only — `speak` is already the seam). Personal WeChat = ban risk,
   opt-in only; prefer Official Account / WeChat Work.
3. Remote TUI client over the gateway (`--connect`); Electron shell last
   (web renderer already exists).
4. hermes leftovers in core/llm.py: stream stall detection, tool-call args
   repair, parallel tool execution.
