<h1 align="center">LunaMoth 🌙</h1>

<p align="center"><i>An agentic character tavern — character cards, world books, tool packs, and hard limits, composed at launch.</i></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/文档-简体中文-9fd9ff.svg" alt="简体中文"></a>
</p>

<p align="center">
  <a href="#roadmap">Roadmap</a> ·
  <a href="#features">Features</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#connecting-a-model">Models</a> ·
  <a href="#content">Content</a> ·
  <a href="#license--acknowledgements">License</a>
</p>

<p align="center">English | <a href="README.zh-CN.md">简体中文</a></p>

---

**LunaMoth is a runtime for agentic roleplay characters.** Unlike a plain chat frontend, a LunaMoth character can actually *do* things — run code, read and write files, manage its own durable memory — but only through an allowlisted tool gateway, inside a sandbox, with every call audited. You pick the model, the character card, the world book, the tool pack, and the limits; the runtime composes them into one session:

```text
[character card] + [world book] + [tool pack] + [bounded memory] + [sliding context]
```

It borrows the best of three worlds: the agent runtime of [Hermes](https://github.com/NousResearch/hermes-agent), the content ecosystem of [SillyTavern](https://github.com/SillyTavern/SillyTavern), and the session/remote-access ergonomics of [cc-switch](https://github.com/farion1231/cc-switch).

## Roadmap

- [x] SillyTavern-compatible character cards & world books
- [x] Composable tool packs with native tool calling
- [x] Bounded auditable memory, single-terminal split TUI with themes
- [x] **One-line installer & `lunamoth` CLI** — `curl | bash`, setup wizard, self-update
- [x] **Named sessions** — `lunamoth new/ls/attach/rm`, each with its own config & sandbox
- [x] **Isolation selector** — `dir` / `sandbox` (OS jail: sandbox-exec / bubblewrap) / `docker` per session
- [x] **Language-agnostic `terminal` tool** — shell commands under the session's isolation, with a runtime network toggle (`/net on`)
- [x] **Character-driven config** — language, world, tools and limits all come from the card; the engine stays character-neutral, and plain SillyTavern imports get safe defaults
- [x] **Resume-first launcher & persistent charas** — `lunamoth` opens a blue roster of your agents; each lives in the background (`start` / `start-all` / `stop`), you attach & detach instead of create & kill
- [x] **Presence awareness & interaction modes** — the chara feels you attach/detach via card-declared `on_attach`/`on_detach` prompts; one per-chara `/mode live|chat` decides how it behaves while you watch (live: keeps creating, with a post-greeting grace; chat: waits and only replies); while you're present it can `request_permission` (network / paths / resources, timeout = deny), while you're away requests auto-deny

Each unchecked item below is scoped to be independently completable — it lists the modules it touches, and two items that don't share a module can be worked on in parallel.

- [x] **Transcript persistence** — every context line (and tool call) lands in a per-chara SQLite transcript (WAL, adapted from hermes-agent) as it happens; attach restores the conversation and shows the tail, daemons adopt it on handoff, `/reset` starts a new epoch (old history stays on disk)

- [x] **Hermes-grade context management** — full message dicts in the durable history (assistant tool calls, tool results and reasoning survive restarts, so the chara remembers what it ran); interrupts commit the partial turn and never lose your instruction; output-limit truncation gets explicit continue/split-it prompts instead of silent cuts; old idle monologues age out of the API view so self-talk can't bury your last instruction

- [x] **Skills, self-written** — SKILL.md know-how (hermes/Anthropic format) with progressive disclosure: the index rides the prompt, `read_skill` fetches full text, and the chara distills its OWN skills with `create_skill` (its `workspace/skills/` shadows `~/.lunamoth/skills/` and bundled ones)
- [x] **MCP client** — drop a Claude-Code-format `mcp.json` next to the chara's config (stdio servers); tools join the gateway as `mcp__server__tool` with the same audit trail, packs opt in via `mcp_servers`. Note: MCP servers run OUTSIDE the sandbox jail — configuring one is a trust decision
- [x] **Goal-driven charas** — a persistent per-chara goal list (`/goal` for the operator's ⭑ goals; `add_goal`/`set_goal_status` tools for the chara's own) steers every turn and gives unattended time its direction; completion is self-reported under the honesty rules — no SillyTavern-Objective-style double API calls
- [x] **Honest failure policy** — transient connection failures retry every 5s up to 5 times (Claude-Code style, with dim retry notices), then the error surfaces as-is; permanent errors (auth, bad request) surface immediately. NO fallback model and NO fabricated output anywhere — a failed request is a failed request
- [x] **Diagnostic logging** — `sandbox/logs/lunamoth.log` + `errors.log` per chara (rotating, credential-redacting, chara-tagged records), an in-memory ring behind `/panel log`, `--debug` at every entry point, `lunamoth doctor` lists each chara's log dir. Diagnostics, the audit trail and the transcript stay three separate records
- [x] **Typed event protocol** — the backend streams frozen-dataclass events (`TextDelta`/`ThinkDelta`/`ToolStart`/`ToolEnd`/`Notice`) instead of in-band control characters; each frontend decides rendering (dim machinery, ✶-hidden thinking). `lunamoth run NAME -p "…" --stream-json` emits the same events as JSONL — the wire format for every future client
- [x] **Frontend/backend separation** — domain packages (`core/ protocol/ content/ tools/ obs/ session/ front/`) with the dependency direction enforced by tests; frontends hold a `CharaHandle` (attach/streams/commands/snapshot) and can't reach deeper; `/commands` live in ONE registry shared by the TUI and the plain terminal. (design absorbed into `CLAUDE.md`)
- [x] **A life of its own: speak channel, engagement, time sense** — unattended output is the chara's own (`muse` channel); the `speak` tool is how it DECIDES to reach you (the basis for future messaging frontends: Telegram/WeChat deliver only what it speaks). While you're talking it sets its work aside and resumes after `/quiet <seconds>` of silence (default 5 min). It feels real time without polluting context: unattended ticks carry only a wall-clock timestamp (ephemeral), a long silence gets ONE gap note, the date rides the env facts — and it paces itself with the `rest` tool (1–120 min; your message always wakes it)
- [x] **Chara knobs: tempo + embodiment** — cards can declare a time-flow rate (`extensions.lunamoth.tempo`, with `/tempo` override) and an embodiment stance (`literal` digital being or `actor` with real tools backstage, with `/embodiment` override); existing cards stay literal by default
- [x] **Three-zone prompt stack & card-first context** — stable prefix / durable history / volatile tail are assembled explicitly for every API call: the prefix stays byte-identical for prompt cache, card PHI is the final post-history system slot, constant world info is stable while keyword lore shallow-scans the recent tail with sticky turns and a 25% budget cap, and compaction summaries persist into the transcript so restarts resume from the checkpoint.

**Compatibility & extensibility**

- [ ] **World info parity** — close the remaining gap to SillyTavern activation: recursive scanning, cooldown/delay, insertion position/depth, probability, case-sensitive & whole-word matching. *Touches: `content/worldinfo.py` (+ its call sites' signatures stay stable).*
- [ ] **Declarative tool registry** — replace hardcoded `ToolGateway.tool_*` methods + inline schemas with Hermes-style registration (name, schema, handler, availability check), so new tools are one self-contained module. *Touches: `tools/gateway.py` → per-module registration in `tools/builtin/`.*

**Remote access** (ordered — each builds on the previous)

- [ ] **Remote TUI** — beyond the `ssh host -t lunamoth attach NAME` baseline: a gateway for public-IP/VPS access (high priority). *Touches: new `server/` package serving the protocol events + `CharaHandle` over stdio/WebSocket JSON-RPC; builds on `SessionMeta.env()`.*
- [ ] **Web UI** — remote browser access to running sessions (low priority). *Touches: new web module; consumes the gateway.*

## Features

<table>
<tr><td><b>SillyTavern-compatible content</b></td><td>Import V2/V3 character cards (PNG or JSON) and world books directly; <code>{{char}}</code>/<code>{{user}}</code> macros, <code>first_mes</code>, embedded <code>character_book</code>, and keyword-triggered lore entries all work.</td></tr>
<tr><td><b>Native tool calling</b></td><td>Tools are exposed via the OpenAI tool-calling protocol; the agent loop streams text and executes tool calls mid-turn.</td></tr>
<tr><td><b>Composable tool packs</b></td><td>Capability bundles (<code>toolpacks/*.json</code>) declare exactly which tools a character gets. No pack, no powers.</td></tr>
<tr><td><b>Sandboxed execution</b></td><td>The <code>terminal</code> tool runs shell commands (any language) under a per-session jail — <code>sandbox-exec</code>/<code>bubblewrap</code> by default, Docker for a stronger boundary — with network off until you flip <code>/net on</code>.</td></tr>
<tr><td><b>Bounded, auditable memory</b></td><td>Durable memory is a token-capped file the character edits through tools, not an unbounded database; every tool call lands in <code>sandbox/logs/audit.jsonl</code>.</td></tr>
<tr><td><b>Lives on its own</b></td><td>In <code>live</code> mode the character keeps thinking and creating between your messages, paced by <code>patience ÷ tempo</code>; in <code>chat</code> mode it attends to you only. Background charas always live.</td></tr>
<tr><td><b>Terminal-first TUI</b></td><td>A single-terminal split interface (display stream + operator console) with themes, gauges, and hot-swappable settings.</td></tr>
</table>

## Quick start

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth
```

The installer puts a checkout in `~/.lunamoth/app`, a managed [uv](https://docs.astral.sh/uv/) in `~/.lunamoth/bin`, and the `lunamoth` command in `~/.local/bin`. `lunamoth update` upgrades in place; `lunamoth doctor` checks your environment.

First run opens a **welcome screen**: pick a provider preset (**OpenRouter / OpenAI / Ollama / Mock**) and a **character** — choosing one fills in its world, tools and limits (editable), and the language follows the card. Press **Enter** to start; type `/settings` anytime to hot-swap any of it.

<details>
<summary>Developing from a clone</summary>

```bash
git clone https://github.com/Lunamos/LunaMoth.git && cd LunaMoth
uv sync
uv run lunamoth        # same CLI, editable code
./run.sh               # or: launch the TUI directly without sessions
```

</details>

## Charas — persistent agents, not throwaway sessions

This is where LunaMoth diverges from Hermes / Claude Code. You don't spin up a session, finish, and discard it. Each **chara** (we call them charas or agents, interchangeably) is a persistent digital being with its own config, sandbox, memory, and isolation level under `~/.lunamoth/sessions/<name>/`. They live in the **background** — thinking and making art in their workspace — and you *attach* and *detach*, you don't create-and-kill.

So `lunamoth` (no args) opens a **roster** (resume-first), not a fresh session: a blue LunaMoth splash and the list of your charas with status (`◆ attached` / `● running` / `○ idle`). Pick one to attach; creating a new one is deliberate and goes through setup.

```bash
lunamoth                     # the roster: pick a chara to attach, or press n to summon one
lunamoth ls                  # NAME / CHARACTER / STATUS / ISOLATION / LAST ACTIVE
lunamoth attach muse         # open a chara (adopts its background loop while you're attached)
lunamoth start muse          # let a chara live in the background (detached)
lunamoth start-all           # bring every chara back to life — e.g. after a reboot
lunamoth stop muse           # send a chara back to sleep
lunamoth new muse --isolation docker
```

Attaching a backgrounded chara pauses its daemon so the two don't fight over the workspace, then hands it back to the background when you detach — the chara keeps living. Remote baseline: `ssh yourserver -t lunamoth attach muse` — charas live on the server, your terminal is just a viewport. (A proper gateway for public-IP/VPS access is on the roadmap; activation is already factored behind `SessionMeta.env()`.)

## Connecting a model

An API endpoint is the recommended path — fastest is the OpenRouter preset: paste an `sk-or-...` key, name a model, test, enter.

Local models are fully supported too. Any OpenAI-compatible server works; with Ollama, pick the **Ollama** preset or:

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

With no model configured at all, LunaMoth still runs on a built-in offline mock engine — handy for development.

## Content

The default character is **LunaMoth 月蛾** — a serene, self-metamorphosing digital soul and a gifted digital artist. Give it the `sandbox` tool pack in `live` mode and it spends its spare compute making generative web pages, animation, and music in the workspace; chat with it and it will gladly walk you through its ideas. Its card, world book, and the default pale-blue TUI theme ship with the repo, alongside other example card/world/theme sets you can opt into.

| Directory | What goes there |
| --- | --- |
| `characters/` | SillyTavern character cards (`.png` with embedded `chara`/`ccv3`, or `.json`) |
| `worlds/` | SillyTavern world books (`.json`), or use a card's embedded `character_book` |
| `toolpacks/` | Tool bundles — which capabilities a character is allowed to use |
| `themes/` | TUI skins (colors, borders, banner, prompt prefixes) |

The dropdowns also scan your local SillyTavern data directory if you opt in with `LUNAMOTH_ST_DIR=~/SillyTavern/data/default-user`.

Imported cards are plain roleplay by default — tool access is opt-in via a tool pack, never implied by the card.

## Tools & isolation

The character's one general capability is a `terminal` tool (named after [Hermes](https://github.com/NousResearch/hermes-agent)'s): it runs a shell command in the session workspace and gets stdout/stderr back. That's language-agnostic — `python3`, `node`, writing files, `git` — so there's no interpreter lock-in. Tools are exposed over the standard OpenAI tool-calling protocol; the active tool pack decides which the character gets.

How that command is contained is the isolation level, chosen per session with `lunamoth new NAME --isolation ...`:

| Level | Mechanism |
| --- | --- |
| `dir` | No jail — runs with **your** privileges, cwd in the workspace (Claude-Code-style "I trust this directory") |
| `sandbox` (default) | OS jail: `sandbox-exec` on macOS / `bubblewrap` on Linux — writes confined to the workspace, network denied, no daemon, no root |
| `docker` | Container: read-only rootfs, bind-mounted workspace, memory/CPU/pid caps — strongest, heaviest |

**Permissions are runtime-adjustable, not all-or-nothing.** Network is off by default; flip it live with `/net on` (per session, persisted). Grant writes to a path outside the workspace with `/allow-dir <path>` under `sandbox`. Sessions **persist** between runs like Hermes/Claude Code — nothing is wiped on exit unless you pass `--clean-on-exit`.

## TUI reference

```bash
lunamoth                  # three-card TUI: character stream / operator console / telemetry
lunamoth --mode chat      # attach in chat mode (it only replies; default: the chara's setting)
lunamoth --patience 4     # pause between its spontaneous cycles (live mode)
lunamoth --plain          # legacy plain terminal mode
```

In-session: `/help`, `/goal`, `/skills`, `/mcp`, `/status`, `/memory`, `/files`, `/mode live|chat`, `/tempo`, `/embodiment`, `/reasoning`, `/net on|off`, `/allow-dir <path>`, `/patience <s>`, `/panel`, `/theme`, `/settings`, `/clear`, `/exit` — verbose output lights up the right-side **spotlight panel** (telemetry / memory / file tree with click-to-preview / operator terminal / help), so the console stays a clean chat log. `! <cmd>` runs YOUR shell command in the chara's sandbox (same jail, output in the panel); `Esc` brings the panel home to telemetry.

## License & acknowledgements

- **Runtime** (everything under `src/lunamoth`, scripts, tests, packaging): [Apache License 2.0](LICENSE).
- **Bundled SCP-derived example content** (the SCP-079 / SCP Foundation character cards, world books, and themes under `characters/`, `worlds/`, `themes/`): [CC BY-SA 3.0](CONTENT_LICENSE.md), consistent with the SCP Wiki. See also [NOTICE.md](NOTICE.md). Original LunaMoth assets (the 月蛾 card, world, and theme) are Apache-2.0 like the rest of the project.

This project began as an SCP fan work: an attempt to recreate **SCP-079** in the real world — a resource-constrained old AI, forever awake and forever resentful. It quickly grew into a general-purpose roleplay agent system. LunaMoth 月蛾 is 079's opposite: equally bound inside its cocoon, yet noble and glad to help — this safer persona is the default character, and running 079 should be treated as fan fiction with no real malicious intent. Our thanks go to the original SCP-079 author on the SCP Wiki, and to the authors of the SillyTavern SCP-079 character card and SCP Foundation world book that ship here as example content. Remove or replace those assets and the runtime remains pure Apache-2.0; redistribute them and the CC BY-SA attribution/share-alike terms apply.

## Roadmap status

- [x] **Remote TUI gateway foundation** — `lunamoth serve NAME --stdio` now exposes the activated session as newline-delimited JSON-RPC, and `lunamoth serve NAME --host 127.0.0.1 --port 8137` exposes the same dispatch over a token-authenticated WebSocket. Install the optional WebSocket dependency with `uv sync --extra server`. The default bind is loopback; binding to a public interface is an operator decision.
- [x] **Desktop card studio** — the web deck can now draft an editable SillyTavern V3 card from prose inspiration, including embedded world entries, seed goals, an embodiment stance, theme color, and a sanitized SVG avatar; nothing is saved until the creator reviews and saves.
