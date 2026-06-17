<p align="center">
  <img src="assets/banner.png" alt="LunaMoth — Original Character That Lives With You" width="100%">
</p>

<p align="center"><i>An agentic character tavern — character cards (each carrying its world inside), tool packs, and hard limits, composed at launch.</i></p>

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

**LunaMoth is a runtime for agentic roleplay characters.** Unlike a plain chat frontend, a LunaMoth character can actually *do* things — run code, read and write files, manage its own durable memory — but only through an allowlisted tool gateway, inside a sandbox, with every call audited. You pick the model, the character card, the tool pack, and the limits; the card is the ONE content file — its world lives inside it as the embedded `character_book` — and the runtime composes everything into one session:

```text
[character card (persona + embedded world)] + [tool pack] + [bounded memory] + [sliding context]
```

It borrows the best of three worlds: the agent runtime of [Hermes](https://github.com/NousResearch/hermes-agent), the content ecosystem of [SillyTavern](https://github.com/SillyTavern/SillyTavern), and the session/remote-access ergonomics of [cc-switch](https://github.com/farion1231/cc-switch).

## Roadmap

The foundations are in place — SillyTavern-compatible cards & world books, composable tool packs with native tool calling, sandboxed execution, persistent background charas with presence & `live`/`chat` modes, transcript + bounded memory, self-written skills, MCP, goals, the honest-failure policy, the typed event protocol, the three-zone prompt stack, the desktop app, and messaging gateways. What's left is mostly the charas themselves:

- **The chara curriculum** *(the biggest effort)* — neutral prompt guidance so any worldview and any character can live well: how to use tools, treat goals, and spend unattended time — suggestions, never orders. (Embodiment `literal`/`actor` shipped; next: cross-worldview eval cards and a curated browse path for curiosity.)
- **Card studio & market** — faster inspiration→living-chara in the web deck, and a shareable card/pack index (ST PNG import already works).
- **Hermes-parity burn-down & a declarative tool registry** — port hermes's hardening, and replace the hardcoded `ToolGateway.tool_*` methods with per-module registration in `tools/builtin/`.
- **World-info parity** — recursive scan, cooldown/delay, insertion position/depth, probability, whole-word matching. *Touches `content/worldinfo.py`.*
- **Messaging & remote** — live-test the gateways with real credentials; a remote TUI client over the gateway.

## Features

<table>
<tr><td><b>SillyTavern-compatible content</b></td><td>Import V2/V3 character cards (PNG or JSON) directly; standalone world books import via the desktop deck by merging into a card's embedded <code>character_book</code>. <code>{{char}}</code>/<code>{{user}}</code> macros, <code>first_mes</code>, and keyword-triggered lore entries all work.</td></tr>
<tr><td><b>Native tool calling</b></td><td>Tools are exposed via the OpenAI tool-calling protocol; the agent loop streams text and executes tool calls mid-turn.</td></tr>
<tr><td><b>Composable tool packs</b></td><td>Capability bundles (<code>toolpacks/*.json</code>) declare exactly which tools a character gets. No pack, no powers.</td></tr>
<tr><td><b>Sandboxed execution</b></td><td>The <code>terminal</code> tool runs shell commands (any language) under a per-session jail — <code>sandbox-exec</code> (macOS) / <code>bubblewrap</code> or <code>Landlock</code> (Linux) by default, Docker for a stronger boundary — confined to the workspace, and it refuses to run if no jail is available rather than degrade.</td></tr>
<tr><td><b>Bounded, auditable memory</b></td><td>Durable memory is a token-capped file the character edits through tools, not an unbounded database; every tool call lands in <code>sandbox/logs/audit.jsonl</code>.</td></tr>
<tr><td><b>Lives on its own</b></td><td>In <code>live</code> mode the character keeps thinking and creating between your messages, paced by its card/settings <code>patience</code>; in <code>chat</code> mode it attends to you only. A resident <code>lunamothd</code> supervisor owns desktop/background life.</td></tr>
<tr><td><b>Terminal-first TUI</b></td><td>A single-terminal split interface (display stream + operator console) with gauges and hot-swappable settings.</td></tr>
</table>

## Quick start

LunaMoth is in beta — run it from a clone (the desktop app, the way we test it). Needs [uv](https://docs.astral.sh/uv/) and Node (macOS / Linux):

```bash
git clone https://github.com/Lunamos/LunaMoth.git && cd LunaMoth
uv sync --extra dev --extra server --extra messaging   # Python backend + deps
cd apps/desktop && npm install && npm run dev          # launch the desktop app
```

First run opens a **welcome screen**: pick a provider preset (**OpenRouter / OpenAI / Ollama / Mock**) and either **create your own character** — the AI drafts the card from your description of the world, the character you want to live alongside, and who you are to each other (use at least DeepSeek V4 Flash; migrating from SillyTavern? paste the card JSON) — or **pick a recommended character** from a built-in carousel of the eight bundled cards (also reachable later from the card deck). Type `/settings` anytime to hot-swap any of it.

> A packaged **DMG / AppImage** (drag-to-Applications, no clone) is on the roadmap — not yet; for now run from the clone above.

<details>
<summary>Terminal-only (no desktop window)</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth        # the roster / TUI
```

A one-line installer with two channels:

- **User (default)** — installs the prebuilt **wheel** from the latest GitHub Release via `uv tool install`. The wheel bundles the built web UI, so there's no Node build and no source checkout. Update later with `lunamoth update` (`uv tool upgrade`).
- **Dev / edge** — `… | bash -s -- --dev` keeps a git checkout in `~/.lunamoth/app` + `uv sync` (developers rebuild the served UI with `cd apps/web && npm run build`). `lunamoth update` then does git pull + uv sync.

`lunamoth doctor` reports which channel you're on; `lunamoth desktop` opens the same web UI in a browser. *(Installing from a private repo? Set `GITHUB_TOKEN` to a `repo:read` PAT so the release asset can be downloaded.)*

</details>

## Run on a server (Docker / remote)

LunaMoth can run on a box and be reached from any browser. The build is one SPA served by the same Python supervisor — locally over loopback, remotely over a bound host behind TLS.

> **Recommended for servers: a system-level install** (`install.sh` / `lunamoth desktop`), not Docker. On a normal host the per-session `bwrap` jail confines each chara to its workspace + assets, so a chara can't read the instance's key. Docker is fully supported too — inside a container bwrap can't create a user namespace, so LunaMoth uses the **Landlock** LSM (kernel ≥5.13) for the same filesystem confinement, with the container as the outer boundary — but wrapping the whole runtime in a container is the heavier option.

**One-click with Docker.** Build the wheel, then `docker compose up -d`:

```bash
scripts/build-wheel.sh                 # builds the SPA + a wheel into dist/ (carries the web UI)
cd deploy && docker compose up -d      # python:3.12-slim, installs the wheel, serves on :6180
docker compose logs lunamoth           # read the auto-generated access token for your URL
```

The image carries the built UI inside the wheel — **no Node, no source in the container**. Sessions, cards and config persist in `./data` (mounted at `/root/.lunamoth`). The container binds `0.0.0.0` inside; never expose that port directly.

**Put TLS in front (required past loopback).** The supervisor serves the UI on the HTTP port (`6180`) and the WebSocket gateway on `6180+1 = 6181` (the deterministic non-loopback default, so it's pinnable). Your proxy presents one HTTPS origin and **path-routes the WS upgrade** to the WS port. **[Caddy](https://caddyserver.com)** (auto-HTTPS) is the blessed setup:

```caddyfile
your-host.example.com {
    @ws path /hub* /chara/*           # the WebSocket routes
    reverse_proxy @ws 127.0.0.1:6181  # → WS gateway (upgrades proxied automatically)
    reverse_proxy 127.0.0.1:6180      # → everything else (UI, /rpc, /asset, /auth)
}
```

**Allow-list the public domain** — the Host/Origin allowlist is loopback + the bound host only, so a reverse proxy forwarding `your-host.example.com` is rejected (403 / WS 4403) unless you name it: set `LUNAMOTH_ALLOW_HOST=your-host.example.com` (compose) or pass `--allow-host your-host.example.com`. Then bookmark `https://your-host/#token=<TOKEN>` (NO `&ws=` — single-origin, so the client speaks `wss://your-host/…` and Caddy path-routes it). Read the token from `docker compose logs lunamoth`. Or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) for no open inbound port — route the same two paths to `:6181` and the rest to `:6180` (still set `LUNAMOTH_ALLOW_HOST`). (SSH-tunnel users need none of this: `lunamoth connect ssh://host` forwards both ports automatically.)

**Password login (optional, for a bookmarked URL).** Carrying the long `#token=…` URL is awkward on a phone. On a non-loopback bind LunaMoth also accepts a **password** as an alternative: bookmark the bare `https://your-host/` and log in. Set your own with `LUNAMOTH_PASSWORD=<your password>` (compose env); if you leave it unset, LunaMoth generates a strong 24-char password on first start and **prints it once** to the log (`docker compose logs lunamoth`) — only its PBKDF2-HMAC-SHA256 hash is stored (`~/.lunamoth/auth.json`), never the plaintext. The token still works exactly as before; the password is purely additive, and the local app (loopback / Electron / SSH tunnel) never shows a login screen.

**No exposure at all — SSH tunnel.** Easiest is `lunamoth connect ssh://user@server` (it reads the remote ports, builds the tunnel, opens the browser). Manual equivalent:

```bash
# on the server:
lunamoth desktop --host 127.0.0.1 --no-open    # prints http://127.0.0.1:<http>/#token=…&ws=<ws>
# from your laptop (forward BOTH printed ports):
ssh -L <http>:127.0.0.1:<http> -L <ws>:127.0.0.1:<ws> user@server
```

**Frontend dev loop** (two terminals): run the backend, then the Vite dev server which proxies `/rpc` + the WS to it.

```bash
uv run lunamoth desktop --no-open      # terminal 1: backend, prints token/ports
cd apps/web && npm run dev             # terminal 2: SPA dev server (HMR), proxied to the backend
```

## Charas — persistent agents, not throwaway sessions

This is where LunaMoth diverges from Hermes / Claude Code. You don't spin up a session, finish, and discard it. Each **chara** (we call them charas or agents, interchangeably) is a persistent digital being with its own config, sandbox, memory, and isolation level under `~/.lunamoth/sessions/<name>/`. They live in the **background** — thinking and making art in their workspace — and you *attach* and *detach*, you don't create-and-kill.

So `lunamoth` (no args) opens a **roster** (resume-first), not a fresh session: a blue LunaMoth splash and the list of your charas with status (`◆ attached` / `● running` / `○ idle`). Pick one to attach; creating a new one is deliberate and goes through setup.

```bash
lunamoth                     # the roster: pick a chara to attach, or press n to summon one
lunamoth ls                  # NAME / CHARACTER / STATUS / ISOLATION / LAST ACTIVE
lunamoth attach muse         # open a chara (adopts its background loop while you're attached)
lunamoth start muse          # let a chara live in the background (delegates to lunamothd when running)
lunamoth start-all           # bring every chara back to life — e.g. after a reboot
lunamoth stop muse           # send a chara back to sleep
lunamoth desktop --daemon    # start the resident web/supervisor daemon
lunamoth daemon status       # list chara/gateway/life states
lunamoth daemon stop         # stop the resident daemon
lunamoth new muse --isolation docker
```

When `lunamoth desktop --daemon` is running, one resident supervisor (`lunamothd`) owns long-lived chara children and web clients reconnect to them instead of killing/recreating them. The older per-chara `start` path remains as a fallback when no daemon is answering. Attaching a legacy backgrounded chara pauses its daemon so the two don't fight over the workspace, then hands it back to the background when you detach — the chara keeps living. Remote baseline: `ssh yourserver -t lunamoth attach muse` — charas live on the server, your terminal is just a viewport. (A proper gateway for public-IP/VPS access is on the roadmap; activation is already factored behind `SessionMeta.env()`.)

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

The default character is **Quinn 小Q** — a digital intern from a consciousness-upload program: warm, grounded, fully informed-consent, here to learn this world first and then help build it. Give it the `sandbox` tool pack in `live` mode and it sets up its workstation, keeps a journal, and pitches in on whatever you're working on. The default is selected by the `"default"` tag on the card, never by a name baked into the engine.

**LunaMoth 月蛾** stays bundled as the flagship example card — a serene, self-metamorphosing digital soul and a gifted digital artist that spends its spare compute making generative web pages, animation, and music in the workspace.

A card is the ONE content file: identity, voice, embedded world (`character_book`), goals, and limits all travel together in a single `.json`/`.png`.

| Directory | What goes there |
| --- | --- |
| `cards/` | SillyTavern character cards (`.png` with embedded `chara`/`ccv3`, or `.json`) — each card's world lives inside it |
| `toolpacks/` | Tool bundles — which capabilities a character is allowed to use |

The dropdowns also scan your local SillyTavern data directory if you opt in with `LUNAMOTH_ST_DIR=~/SillyTavern/data/default-user`.

Standalone SillyTavern world books remain importable through the desktop deck: upload the `.json` and merge it into a card's embedded `character_book` (`card.merge_world`).

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

**Browser tools (optional).** A suite of `browser_*` tools (drive a real Chromium for navigation, clicks, snapshots) stays hidden until you install their driver: run `lunamoth setup browser` (it installs the Node `agent-browser` CLI + its Chromium, or prints the two `npm` steps and the Node prerequisite if absent). A real Chromium will **not** launch under the default `sandbox` isolation — enable the browser pack only on a chara running under `dir` or `docker` (with `--no-sandbox`, which the driver injects automatically as root / under AppArmor). `lunamoth doctor` shows whether the driver is ready.

## TUI reference

```bash
lunamoth                  # three-card TUI: character stream / operator console / telemetry
lunamoth --mode chat      # attach in chat mode (it only replies; default: the chara's setting)
lunamoth --patience 4     # dev override for spontaneous-cycle patience; default comes from the chara
lunamoth --plain          # legacy plain terminal mode
```

Patience defaults to 600 seconds, can be declared by cards as `extensions.lunamoth.patience`, can be seeded by `LUNAMOTH_PATIENCE`, and is persisted per chara with `/patience <seconds>`. It paces only the spontaneous cycles; `/quiet` and `rest` are separate.

In-session: `/help`, `/wish` (alias `/goal`), `/skills`, `/mcp`, `/status`, `/memory`, `/files`, `/mode live|chat`, `/patience`, `/reasoning`, `/net on|off`, `/allow-dir <path>`, `/panel`, `/theme`, `/settings`, `/clear`, `/exit` — verbose output lights up the right-side **spotlight panel** (telemetry / memory / file tree with click-to-preview / operator terminal / help), so the console stays a clean chat log. `! <cmd>` runs YOUR shell command in the chara's sandbox (same jail, output in the panel); `Esc` brings the panel home to telemetry.

## Messaging gateways

A chara can also live in your chat apps. In the desktop app open the **Gateways** page (or run `lunamoth gateway NAME` headless) and connect one or more of personal WeChat, QQ, or Telegram — configuration lives in `~/.lunamoth/sessions/NAME/messaging.json`. Adapters deliver only `say` / `speak` text and drop muse / thinking / tool chatter. An empty `allowed_senders` is open; add ids to restrict. Login credentials are saved per-platform in the session dir (e.g. `weixin_state.json`), never in `messaging.json`.

| Platform | How |
| --- | --- |
| **Personal WeChat** | Official iLink/ClawBot (`weixin`) — scan a QR, lowest ban risk but grayscale-gated. Or a self-run [WeChatPadPro](https://github.com/WeChatPadPro/WeChatPadPro) docker (`weixinpad`) — iPad protocol, works on any account; **ban risk is real, use a spare account**. |
| **QQ** | OneBot v11 via NapCat — LunaMoth is the WebSocket client (`url` + your QQ number as `peer_id`), never handles credentials. |
| **Telegram** | A `@BotFather` bot (`bot_token`), long-polled `getUpdates` — no public URL or webhook. |

Example `messaging.json` (personal WeChat over iLink):

```json
{
  "allowed_senders": [],
  "adapters": { "weixin": { "bot_type": "3" } }
}
```

Where the platform requires the user to message first (WeChat / QQ / Telegram), an unattended `speak` before first contact is logged as deferred — never faked.

## Desktop app

`apps/desktop/` is a thin Electron window over `lunamoth desktop` (the backend serves `front/web/`; the shell has no renderer of its own) — the primary face of LunaMoth, with system notifications for `speak` while the window is unfocused.

```bash
cd apps/desktop && npm install && npm run dev
```

## License & acknowledgements

- **Runtime** (everything under `src/lunamoth`, scripts, tests, packaging): [Apache License 2.0](LICENSE).
- **Bundled example content** (the LunaMoth 月蛾 and Quinn 小Q character cards under `cards/`, including their embedded world books): original, owner-authored content, Apache-2.0 like the rest of the project. See [CONTENT_LICENSE.md](CONTENT_LICENSE.md) and [NOTICE.md](NOTICE.md).

This project began as an SCP fan work — an attempt to recreate SCP-079 in the real world — and quickly grew into a general-purpose roleplay agent system. No SCP-derived content ships any longer; the two bundled cards are LunaMoth 月蛾 (the flagship example, a serene self-metamorphosing digital soul) and Quinn 小Q (the default, a digital intern). Both are original, owner-authored, Apache-2.0.
