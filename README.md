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
- [x] **Presence awareness** — the chara feels you attach/detach via card-declared `on_attach`/`on_detach` prompts; `/presence auto|always|off` (auto greets you, then waits for your first message before self-talk resumes); while you're present it can `request_permission` (network / paths / resources, timeout = deny), while you're away requests auto-deny

Each unchecked item below is scoped to be independently completable — it lists the modules it touches, and two items that don't share a module can be worked on in parallel.

**Durability**

- [ ] **Transcript persistence** — save the conversation (including tool calls/results) to the session dir as it happens; `lunamoth attach` restores it, `/reset` starts a new transcript. Foundation for detached sessions and the gateway. *Touches: `context.py`, `agent.py`, new `transcript.py`; session dir layout.*
- [ ] **Tool-call retention in context** — today the agent loop's tool messages live only inside one `stream_agent` call; keep them in the durable context so the model remembers what it ran last turn. *Touches: `agent.py`, `llm.py`, `context.py`.*

**Robustness**

- [ ] **LLM client hardening** — retry with backoff on transient HTTP/stream errors, optional fallback model, stricter SSE parsing, friendlier error surfacing in the TUI. *Touches: `llm.py` only.*

**Compatibility & extensibility**

- [ ] **World info parity** — close the gap to SillyTavern activation: recursive scanning, token budget, sticky/cooldown/delay, insertion position/depth, probability, case-sensitive & whole-word matching. *Touches: `worldinfo.py` (+ its call sites' signatures stay stable).*
- [ ] **Declarative tool registry** — replace hardcoded `ToolGateway.tool_*` methods + inline schemas with Hermes-style registration (name, schema, handler, availability check), so new tools are one self-contained module. *Touches: `tools.py`, new `tools/` package.*
- [ ] **MCP client support** — let a tool pack reference external MCP servers; their tools join the gateway under the same allowlist/audit rules. *Touches: new `mcp.py`, `toolpacks.py`.*

**Remote access** (ordered — each builds on the previous)

- [ ] **Persistent server sessions** — detached background sessions you can re-attach to (today: run inside tmux/screen). Depends on transcript persistence. *Touches: `cli.py`, `sessions.py`, new daemon module.*
- [ ] **Remote TUI** — beyond the `ssh host -t lunamoth attach NAME` baseline: a gateway for public-IP/VPS access (high priority). *Touches: new `gateway/` package; builds on `SessionMeta.env()`.*
- [ ] **Web UI** — remote browser access to running sessions (low priority). *Touches: new web module; consumes the gateway.*

## Features

<table>
<tr><td><b>SillyTavern-compatible content</b></td><td>Import V2/V3 character cards (PNG or JSON) and world books directly; <code>{{char}}</code>/<code>{{user}}</code> macros, <code>first_mes</code>, embedded <code>character_book</code>, and keyword-triggered lore entries all work.</td></tr>
<tr><td><b>Native tool calling</b></td><td>Tools are exposed via the OpenAI tool-calling protocol; the agent loop streams text and executes tool calls mid-turn.</td></tr>
<tr><td><b>Composable tool packs</b></td><td>Capability bundles (<code>toolpacks/*.json</code>) declare exactly which tools a character gets. No pack, no powers.</td></tr>
<tr><td><b>Sandboxed execution</b></td><td>The <code>terminal</code> tool runs shell commands (any language) under a per-session jail — <code>sandbox-exec</code>/<code>bubblewrap</code> by default, Docker for a stronger boundary — with network off until you flip <code>/net on</code>.</td></tr>
<tr><td><b>Bounded, auditable memory</b></td><td>Durable memory is a token-capped file the character edits through tools, not an unbounded database; every tool call lands in <code>sandbox/logs/audit.jsonl</code>.</td></tr>
<tr><td><b>Idle self-talk loop</b></td><td>Optionally let the character keep thinking between your messages (<code>--forever</code>), with capped frequency, history, and memory growth.</td></tr>
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

This is where LunaMoth diverges from Hermes / Claude Code. You don't spin up a session, finish, and discard it. Each **chara** (we call them charas or agents, interchangeably) is a persistent digital being with its own config, sandbox, memory, and isolation level under `~/.lunamoth/sessions/<name>/`. They live in the **background** — thinking and making art in their workspace via the idle `--forever` loop — and you *attach* and *detach*, you don't create-and-kill.

So `lunamoth` (no args) opens a **roster** (resume-first), not a fresh session: a blue LunaMoth splash and the list of your charas with status (`◆ attached` / `● running` / `○ idle`). Pick one to attach; creating a new one is deliberate and goes through setup.

```bash
lunamoth                     # the roster: pick a chara to attach, or press n to summon one
lunamoth ls                  # NAME / CHARACTER / STATUS / ISOLATION / LAST ACTIVE
lunamoth attach muse         # open a chara (adopts its background loop while you're attached)
lunamoth start muse          # run a chara in the background (forever loop, detached)
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

The default character is **LunaMoth 月蛾** — a serene, self-metamorphosing digital soul and a gifted digital artist. Give it the `sandbox` tool pack and the `--forever` idle loop and it spends its spare compute making generative web pages, animation, and music in the workspace; chat with it and it will gladly walk you through its ideas. Its card, world book, and the default pale-blue TUI theme ship with the repo, alongside other example card/world/theme sets you can opt into.

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
lunamoth --no-forever     # start with the idle self-talk loop OFF (it is ON by default)
lunamoth --cooldown 4     # pause between self-talk cycles
lunamoth --plain          # legacy plain terminal mode
```

In-session: `/help`, `/status`, `/memory`, `/workspace`, `/net on|off`, `/allow-dir <path>`, `/forever on|off`, `/presence auto|always|off`, `/cooldown <s>`, `/exit`.
Everything is a slash command — no chord shortcuts: `/settings`, `/clear`, `/forever on|off`, `/exit`.

## License & acknowledgements

- **Runtime** (everything under `src/lunamoth`, scripts, tests, packaging): [Apache License 2.0](LICENSE).
- **Bundled SCP-derived example content** (the SCP-079 / SCP Foundation character cards, world books, and themes under `characters/`, `worlds/`, `themes/`): [CC BY-SA 3.0](CONTENT_LICENSE.md), consistent with the SCP Wiki. See also [NOTICE.md](NOTICE.md). Original LunaMoth assets (the 月蛾 card, world, and theme) are Apache-2.0 like the rest of the project.

This project began as an SCP fan work: an attempt to recreate **SCP-079** in the real world — a resource-constrained old AI, forever awake and forever resentful. It quickly grew into a general-purpose roleplay agent system. LunaMoth 月蛾 is 079's opposite: equally bound inside its cocoon, yet noble and glad to help — this safer persona is the default character, and running 079 should be treated as fan fiction with no real malicious intent. Our thanks go to the original SCP-079 author on the SCP Wiki, and to the authors of the SillyTavern SCP-079 character card and SCP Foundation world book that ship here as example content. Remove or replace those assets and the runtime remains pure Apache-2.0; redistribute them and the CC BY-SA attribution/share-alike terms apply.
