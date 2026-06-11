# LunaMoth — project memory for Claude Code

LunaMoth is an **agentic character tavern / runtime**: pick a model + a character
card + world book + tool pack + limits, and it composes them into one running
"chara" — a persistent digital agent that can actually *do* things (run shell
commands, read/write files, manage state) through an allowlisted, sandboxed,
audited tool gateway. It is the combination of three projects, and you should
**always consult them when designing anything** (clone them under `reference/`,
which is gitignored):

- **NousResearch/hermes-agent — the most important reference.** Agent runtime,
  terminal backends, context management, install/CLI UX, the `terminal` tool
  name, SOUL.md/MEMORY.md ideas, prompt-cache discipline.
- **SillyTavern/SillyTavern** — character cards / world books / prompt layering
  (we are card- and world-book-compatible).
- **farion1231/cc-switch** — session/roster ergonomics, remote access.

History: started as an SCP-079 fan recreation (a contained, resentful old AI),
then generalized. The default chara is now **LunaMoth 月蛾**, 079's benign
opposite (a serene digital-artist soul). SCP-079 ships as an opt-in example.
SCP is mentioned only in the license/acknowledgements; the engine is
character-neutral.

## Run / dev / test

```bash
uv sync
uv run lunamoth            # the CLI (editable; reflects the working tree)
uv run lunamoth --plain    # legacy plain terminal (native cursor + IME; good for CJK)
uv run python -m pytest -q # tests live in tests/, confined via pyproject testpaths
uvx ruff check --select F src/lunamoth tests   # lint (unused imports, undefined names)
```

- Installed copy lives in `~/.lunamoth/app`; `lunamoth update` = git pull + uv sync.
- `install.sh` is the `curl | bash` installer (macOS/Linux only; uv-based).
- The TUI is Textual; you can headless-test it with `app.run_test()` pilots
  (see how tests mount `LunaMothTUI(patience=...)`).

## Conventions

- **Commit messages** end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
  Commit/push only when asked. Keep commits scoped to your own files.
- **Two agents sometimes edit this repo at once.** If `git status` shows files
  you didn't touch, a sibling agent is mid-edit — DO NOT `git add -A`/`git checkout`
  those; stage only your files, and never clobber someone's uncommitted work.
- Platforms: **macOS first, then Linux.** No Windows.
- Language is **never a setting** — it's a property of the active card (`.zh` →
  zh, `.en` → en, else CJK detection). Engine + tools are language-agnostic.
- **No chord shortcuts in the TUI** — everything is a `/command` (`/settings`,
  `/clear`, `/mode`, `/net`, `/reset`, `/exit`, …). Ctrl+C is the only key (safety quit).
- README is split EN (`README.md`) / zh (`README.zh-CN.md`).

## Module map (src/lunamoth/)

- `cli.py` — the `lunamoth` command. Default opens the **roster** (resume-first);
  `new/ls/attach/start/start-all/stop/rm/setup/update/doctor`. Owns the daemon
  helpers (`_start_daemon`/`_stop_daemon`) and session env activation.
- `roster.py` — the launcher. Plain-terminal, inline (never full-screen): blue
  splash + arrow-key (↑/↓/Enter) menu, line-input fallback on non-tty.
  - Uses the **compact block wordmark** (the figlet `standard` "LunaMoth") — this
    is the preferred look; do NOT switch the launcher to the wide serif one.
  - Reads keys via raw-mode **`os.read(fd)`**, NOT `sys.stdin.read` — the text
    buffer swallows the `[A` after ESC and makes every arrow key read as a bare
    Esc (which quit the launcher). Don't reintroduce that. Verified with a
    `pty.fork()` test (arrows decode to up/down).
- `sessions.py` — named charas under `~/.lunamoth/sessions/<name>/` (config.json,
  sandbox, transcript.db). `SessionMeta.env()` is the stable activation interface.
- `wizard.py` — plain-terminal first-run setup (provider → key → model → test →
  character menu). Runs BEFORE the full-screen TUI; only `/settings` mid-session
  uses the Textual welcome screen.
- `tui.py` — the split TUI (character stream / operator console / spotlight
  sidebar). Steady (non-blinking) caret. **Actively evolving; a sibling agent
  often edits this — read it fresh before touching.**
- `terminal.py` — legacy plain-terminal loop; also what the background daemon runs.
- `agent.py` — `LunaMothAgent`: composes persona + world + tools + rules into the
  system prompt (`_build_system_messages`), runs the streaming agent loop, owns
  memory/state/transcript/context_limit.
- `llm.py` — OpenAI-compatible streaming client + the tool-calling agent loop
  (tool_calls retention, reasoning_content captured-not-replayed, truncation
  handling, interrupt-safe record()).
- `cards.py` — SillyTavern V2/V3 card loader (PNG/JSON). `.defaults()` reads
  `extensions.lunamoth` (world / toolpack / memory_chars / rules / rules_closer).
- `worldinfo.py` — world book / lorebook activation + `{{char}}`/`{{user}}` macros.
- `rules.py` — the **Rules layer** (see Prompt stack below).
- `tools.py` — `ToolGateway`: the allowlisted tool dispatch (`terminal`,
  read/write files, memory, inspect_env). Validates required args.
- `runner.py` — runs the `terminal` tool's shell command under the isolation
  mechanism (dir / sandbox-exec|bwrap / docker); reads net/writable perms per call.
- `state.py` — `EnvState`: neutral per-session runtime state (env_status.json:
  isolation, network_access, writable_paths, tool_access). No SCP framing.
- `providers.py` — model **context window** from the provider (OpenRouter
  `/models` lookup, cached; default 32768 elsewhere). NOT a setting.
- `context.py` — `ContextBuffer`: sliding window of full message dicts (tool_calls
  survive). `THINK_WINDOW` limits idle self-talk in the API view.
- `transcript.py` — per-chara SQLite conversation log (WAL+fallback, epochs for
  /reset), restored on attach. The durable source of truth.
- `memory.py` — `MemoryStore`: Hermes-style durable memory. Two `§`-delimited
  stores (`memory` = notes-to-self, `user` = facts about the operator), file-backed
  under `SANDBOX_ROOT/memory/`. Edited via the one `memory` tool (add/replace/remove
  × memory/user). The agent injects a FROZEN snapshot (taken at session start, see
  `agent._freeze_memory`) into the system prompt — mid-session writes hit disk + the
  tool response but NOT the prompt, so the cache prefix stays stable. `memory_chars`
  is still card-settable (079's tiny memory is characterful).
- `presence/` — attach/detach awareness + the `/mode live|chat` interaction mode.
- `art.py` — the blue LunaMoth wordmark (rich Text, gradient, moonlight sweep).
- `themes.py`, `toolpacks.py`, `audit.py`, `cleanup.py`, `config.py`,
  `settings.py`, `persona.py`, `sandbox.py` — supporting layers.

Content (gitignore-allowlisted): `characters/` `worlds/` `toolpacks/` `themes/`.

## The prompt stack (key design)

Built fresh each turn in `agent._build_system_messages`, in this order:

1. **Character card = the soul.** Identity/voice/autonomy come entirely from the
   card. The engine injects NO identity of its own. (An earlier "soul" layer was
   removed because it overlapped the card.)
2. **Rules** (`rules.py`) — neutral, character-agnostic operating standard, **only
   when the chara has tools**: you have authority over your sandbox; your work
   must be REAL (anti-fabrication: don't claim a poem/file/page is done unless it
   truly exists — fixes "claims it wrote a poem it never made"); act through
   tools, not narration; an empty user message = nobody's talking, do as you wish.
   A tool-less pure-roleplay chara gets NO rules → free to narrate fiction.
3. Tool nudge + live env facts; world info.
4. **Closer** — short post-history reminder (SillyTavern style), last = strongest,
   only with tools.

Override hooks (cards leave them empty by default): `extensions.lunamoth.rules`,
`.rules_closer`; global `~/.lunamoth/rules.md`.

## Charas, isolation, context

- A **chara** is a persistent agent (not a throwaway session). It lives in the
  background via `--forever`/`start`; you attach/detach. `start-all` revives all.
- **Isolation** per session (`--isolation`): `dir` (no jail, your privileges) /
  `sandbox` (default; sandbox-exec on macOS / bubblewrap on Linux — net off,
  writes confined) / `docker`. Network is runtime-toggleable (`/net on`), not all-or-nothing.
- **Three memory-ish things, kept distinct:** context window (sliding, sent each
  turn, sized to the model's real window) · transcript (full SQLite log, restore)
  · durable memory (Hermes-style memory/user stores, frozen-snapshot into the
  prompt — see `memory.py`).
- **Context window = the model's real window** (providers.py), never a setting or
  card knob. Memory size (`memory_chars`) IS still card-settable (079's tiny
  memory is characterful).

## Parked work (decided, not yet built)

1. ✅ **Context compaction (Hermes-style)** — DONE (`compaction.py`). When the
   window nears its usable budget (`max_tokens − trim_buffer`, ~75%), the old head
   is summarized into one `kind="summary"` system message (neutral factual voice
   via `llm.raw_complete`), the recent tail kept verbatim; the prior summary sits
   at messages[0] so it folds into the next one (iterative for free). Tied to the
   ContextBuffer's own budget so it fires BEFORE `trim()` hard-drops. Runs auto in
   `agent._context_view` and via `/compact`. Best-effort (offline/failure → no-op,
   trim is the backstop). Remaining polish (optional): a cheap tool-output-pruning
   pass before the LLM call; persist the summary into the transcript so restore
   loads "summary + tail" instead of re-compacting.
2. ✅ **Replace the legacy memory doc with Hermes-style memory** — DONE. The old
   single always-injected/rewritten document (which mutated the system prompt every
   turn → broke prompt cache) is gone. Replaced by `memory.py`'s two `§`-delimited
   stores (memory + user), one `memory` tool (add/replace/remove), and a FROZEN
   snapshot injected into the system prompt (loaded at session start, never rebuilt
   mid-session → cache-stable). Storage moved from `workspace/memory.txt` to
   `SANDBOX_ROOT/memory/{memory,user}.md`.

## Roadmap (remote, ordered)

Persistent server sessions (detached + reattach) → remote TUI / public-IP gateway
(builds on `SessionMeta.env()`) → web UI (low priority). No Hugging Face.

### Messaging gateway + desktop — design to adopt (studied AstrBot + Hermes)

Two reference projects clone into `reference/` (gitignored): `AstrBot` (multi-
platform chatbot framework — WeChat/QQ/Telegram/…) and `hermes-agent`.

**Connecting to bots (WeChat etc.) — copy AstrBot's adapter pattern**
(`AstrBot/astrbot/core/platform/`): a `Platform` base class (impl `run()` —
push events to a shared `asyncio.Queue` — and `meta()`), registered via a
`@register_platform_adapter("name", ...)` decorator; one gateway process loads
the enabled adapters from config and an EventBus consumes the queue. Incoming
messages normalize to one `AstrBotMessage` + a `MessageChain` of components
(Plain/Image/Record/File/At/…). This maps cleanly onto LunaMoth: a message →
route to a chara session (cf. `unified_msg_origin`); the chara's reply →
`adapter.send`. Our per-chara sandbox/transcript IS that session.
  - WeChat reality: **personal WeChat (`weixin_oc`) uses an unofficial QR-bridge
    (OpenClaw) → ban risk** — make it opt-in only. Prefer the SAFE official paths:
    Official Account (`weixin_official_account`, webhook+wechatpy), WeChat Work
    (`wecom`). Start with Telegram/Discord (official, easiest to verify).

**Desktop / web — copy Hermes's protocol seam, NOT AstrBot's monolith.**
AstrBot's Quart web dashboard IS the core (tightly coupled → hard to add other
UIs). Hermes keeps the core headless and exposes ONE protocol —
newline-delimited JSON-RPC via `tui_gateway.dispatch`, served over BOTH stdio
(the TUI) AND WebSocket (`/api/ws`, `hermes_cli/web_server.py`). The web
dashboard (FastAPI+React) and the Electron desktop (`apps/desktop`, a thin shell
that spawns the backend subprocess + embeds the web UI/PTY) are just clients of
that one dispatch — zero logic duplication.

The official **Hermes Desktop** (`apps/desktop`, Electron, shipped v0.15.2) is
NOT a separate product — it's a thin native shell that installs the same runtime
into `~/.hermes` and whose renderer talks to a `hermes dashboard` backend over
the standard gateway APIs. Hermes's model is THREE pieces: (a) the **agent
backend** (`hermes dashboard` server — clients attach, local OR remote over
`/api/ws`+auth, e.g. VPS/Tailscale); (b) the **messaging gateway** (Telegram/etc,
a separate long-running process); (c) the **clients** (TUI / web / desktop). The
desktop attaching to a *remote* backend IS exactly LunaMoth's "remote TUI /
public-IP gateway" goal — so the JSON-RPC seam + a `lunamoth serve`-style backend
is the prerequisite for ALL of it (remote, web, and desktop).

**Build order for LunaMoth:** (1) wrap the agent in a small JSON-RPC dispatch
(stdio + WebSocket) so the current Textual TUI becomes a client of it; (2)
Telegram adapter; (3) Official-Account / WeChat-Work; (4) web panel (backend
serves static, AstrBot-style); (5) only then an Electron shell. Do NOT start with
personal WeChat (ban risk) or with Electron (premature).
