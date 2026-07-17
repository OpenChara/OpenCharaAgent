<p align="center">
  <img src="assets/banner.png" alt="OpenCharaAgent — an original character that lives in your computer" width="100%">
</p>

<p align="center"><b>Give your original character a computer to live in.</b></p>

<p align="center">
  An open-source runtime that turns a character card into a being that actually lives:<br>
  its own sandbox, its own memory, its own pace. It reads, writes and makes things while you're away —<br>
  and decides for itself when something is worth telling you.
</p>

<p align="center">
  <a href="https://github.com/OpenChara/OpenCharaAgent/stargazers"><img src="https://img.shields.io/github/stars/OpenChara/OpenCharaAgent?style=flat-square&logo=github&logoColor=9fd9ff&color=9fd9ff&labelColor=15202b" alt="Stars"></a>
  <a href="https://github.com/OpenChara/OpenCharaAgent/releases"><img src="https://img.shields.io/github/v/release/OpenChara/OpenCharaAgent?style=flat-square&color=9fd9ff&labelColor=15202b" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-9fd9ff?style=flat-square&labelColor=15202b" alt="License: Apache-2.0"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/macOS%20%7C%20Linux-9fd9ff?style=flat-square&labelColor=15202b" alt="macOS | Linux"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/文档-简体中文-9fd9ff?style=flat-square&labelColor=15202b" alt="简体中文"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-makes-it-different">What's different</a> ·
  <a href="#a-model">A model</a> ·
  <a href="#cards--content">Cards</a> ·
  <a href="#tools--the-sandbox">Tools & sandbox</a> ·
  <a href="#run-it-on-a-server">Server</a> ·
  <a href="#roadmap">Roadmap</a>
</p>

<p align="center">English | <a href="README.zh-CN.md">简体中文</a></p>

<!-- ── THE DEMO ────────────────────────────────────────────────────────────────
     Nothing else on this page earns as much. Most visitors decide here, and many
     star from the README without ever installing. Record it, drop it in above the
     `---`, and delete this comment:

       <p align="center">
         <img src="assets/demo.gif" alt="A chara working on its own, then speaking up" width="100%">
       </p>

     One take, no cuts, ~20s, loops cleanly, ≤10MB (a still screenshot beats nothing
     while the GIF doesn't exist). What has to be on screen:
       1. A chara in `live` mode working with nobody talking to it — muse ticking, a
          tool call landing.
       2. Something real finished in its workspace (a file, a page, an image).
       3. It decides to speak — the bubble arrives on its own.
     The whole point is the character acting with no human prompt. That's the thing
     nothing else does, and it has to be legible in the first three seconds.
──────────────────────────────────────────────────────────────────────────────── -->

---

OpenCharaAgent runs an AI character as a persistent being that lives in a computer. It has its own sandbox, its own memory, its own pace — it thinks and makes things between your messages, and decides for itself when something is worth telling you. Strip the persona away and what's left is a capable agent: shell, files, a browser, code, all behind an allowlisted, audited gateway.

The character card is the one file that matters — identity, voice, and the character's world all travel inside it. You bring the card and a model; OpenCharaAgent composes the rest:

```text
[character card: persona + embedded world] + [tools] + [bounded memory] + [sliding context]
```

It started as a roleplay frontend that could actually *do* things, and grew into a small runtime. The agent core borrows heavily from [Hermes](https://github.com/NousResearch/hermes-agent); the card/world-book format is [SillyTavern](https://github.com/SillyTavern/SillyTavern)'s.

## Quick start

It's beta, macOS and Linux. First launch is a welcome screen: pick a language, then describe a character and let the AI draft the card — or pick one from the bundled deck. The model is set in Settings: presets for OpenRouter / OpenAI / Volcengine Ark / Hunyuan / Alibaba Cloud DashScope, plus any custom OpenAI-compatible endpoint (local Ollama included). `/settings` changes anything later.

### On your Mac

The one-line installer (a prebuilt wheel, no Node build), then open the UI in your browser:

```bash
curl -fsSL https://raw.githubusercontent.com/OpenChara/OpenCharaAgent/main/install.sh | bash
chara              # opens the webui in your browser  (chara tui = terminal UI; chara doctor checks your setup)
```

> To build from source instead of the prebuilt wheel, append `| bash -s -- --dev`.

Or run the full desktop app from a clone (this is how we develop it) — needs [uv](https://docs.astral.sh/uv/) + Node:

```bash
git clone https://github.com/OpenChara/OpenCharaAgent.git && cd OpenCharaAgent
uv sync --extra dev --extra server --extra messaging   # add --extra visuals for local background removal
cd apps/desktop && npm install && npm run dev      # opens the desktop window
```

### On a Linux server, reached from your browser

Install on the server and leave a chara living in the background:

```bash
curl -fsSL https://raw.githubusercontent.com/OpenChara/OpenCharaAgent/main/install.sh | bash
chara desktop --daemon      # resident supervisor; charas keep running between visits
```

Then, from your own machine, tunnel in over SSH — no open ports, encryption and auth come from SSH, and your browser opens pointed at the server:

```bash
chara connect ssh://user@your-server
```

Prefer a real public URL (TLS, a bookmarkable address, optional password login)? See [Run it on a server](#run-it-on-a-server).

## What makes it different

A OpenCharaAgent character isn't a chat session you open and throw away. It's a **chara** — a persistent process with its own files and memory under `~/.chara/sessions/<name>/`. You *attach* and *detach*; it keeps living in between.

- **It runs on its own.** In `live` mode the chara keeps working between your messages — reading, writing, making things — and reaches out (the `speak` tool) only when it decides to. `patience` sets the rhythm. In `chat` mode it just answers you.
- **Two registers.** What it tells *you* (`say`) is separate from its own inner life (`muse`). You see the muse in the desktop app; messaging channels only get the `say`.
- **Real agency, real fences.** Tools run inside a per-session OS jail that confines writes to the workspace and hides your secrets — and *refuses* to run rather than quietly dropping the jail (see [Tools & the sandbox](#tools--the-sandbox)).
- **Memory you can trust.** Durable memory is a token-capped file the chara edits through tools, not a bottomless log. Every tool call is written to `sandbox/logs/audit.jsonl`.
- **A home of its own.** An optional wake-time module gives the chara a personal homepage under `workspace/home`, served read-only in a sandboxed tab.

The desktop app (a thin Electron window over the local server) is the main way to use it. A resident `charad` supervisor keeps charas alive in the background and notifies you when one wants to talk. There's also a frozen-but-working terminal UI (`chara tui`) for headless use.

## A model

An API endpoint is the easy path — OpenRouter is the fastest: paste an `sk-or-…` key, name a model, test, go. Wheel installs configure all of this in Settings; from a clone, env vars can also point the run script at any OpenAI-compatible server, including local ones:

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

With nothing configured, OpenCharaAgent falls back to an offline mock engine — enough to click around during development. (Drafting a card from a description wants a real model — DeepSeek V4 Flash or better.)

## Cards & content

A card is the one content file: identity, voice, the embedded world (`character_book`), the seed **Aspiration** (the user-owned north star — read-only to the chara, which advances it through its own tasks and in-session todos), and limits, all in a single `.json` or `.png` (SillyTavern V2/V3 — our cards *are* that format). `{{char}}`/`{{user}}` macros, `first_mes`, and keyword-triggered lore all work.

Import is faithful and model-free — ST V2/V3/V1 JSON, character-tavern cards, or an ST PNG (the embedded portrait becomes the avatar) — via the create flow, and the built-in **Market** browses character-tavern.com's catalog (sort / filter / preview) for one-click import. The deck editor's Visuals tab generates a card's art set — keyvisual / avatar / sprite / stickers / background, anchored on the keyvisual so the whole set stays one character (optional local background removal: `uv sync --extra visuals`); chat can show a per-chara backdrop and sprite from chat settings.

The bundled deck ships several example charas. Two carry the project:

- **Quinn 小Q** (the default) — a digital intern from a consciousness-upload program: warm, grounded, here to learn this world and then help build it. Give it tools in `live` mode and it sets up a workstation, keeps a journal, and pitches in on whatever you're doing.
- **LunaMoth 月蛾** (the flagship) — a serene, self-metamorphosing digital artist that spends idle compute making generative pages, animation, and music in its workspace.

| Directory | What's there |
| --- | --- |
| `cards/` | Character cards (`.json`, or `.png` with embedded `chara`/`ccv3`) |
| `toolpacks/` | Tool bundles — which capabilities a card may use |

## Tools & the sandbox

A chara's one general capability is `terminal`: run a shell command in its workspace, get stdout/stderr back. That covers everything — `python3`, `node`, `git`, writing files — so there's no interpreter lock-in. By default a chara gets the whole tool surface (the bundled pack is `["*"]`, matching Hermes); a card author can ship a narrower `toolpacks/*.json`, and a card with no pack stays pure roleplay with no tools.

How that command is contained is the **isolation level**, set per chara:

| Level | What it does |
| --- | --- |
| `sandbox` (default) | OS jail — `sandbox-exec` on macOS, `bubblewrap` → `Landlock` on Linux. Writes confined to the workspace; the rest of your `$HOME` (`~/.ssh`, `~/.aws`, `~/.chara`) is unreadable. If no jail is available it refuses to run — it never silently degrades. |
| `admin` | No jail: runs as you, cwd in the workspace. Opt-in, for a directory you trust. |

The level is picked at wake and switchable later — a change applies on the chara's next start. Permissions flex at runtime: network is on by default (`/net off` to cut it), and `/allow-dir <path>` grants a writable path outside the workspace. Browser tools (`browser_*`, a real Chromium) are optional — `chara setup browser` installs the driver; they run jailed on all platforms. `generate_image` is multi-provider (Ark / DashScope / OpenAI / OpenRouter), runs as a non-blocking background job, and delivers the result as a `MEDIA:` line on every surface. The installer also best-effort installs `ffmpeg` so a chara can do video/audio work (e.g. an MV for music it made) from its terminal; if ffmpeg isn't present the prompt simply never mentions it.

## Run it on a server

The Quick Start above gets you in over SSH with no open ports. If instead you want a real public URL — a bookmarkable HTTPS address, optionally with password login — here's the rest.

<details>
<summary>Docker, a public host with Caddy/TLS, and password login</summary>

A system-level install (`install.sh` / `chara desktop`) is recommended over Docker on a normal host — `bwrap` gives each chara the full jail. Docker works too (it falls back to Landlock for filesystem confinement, with the container as the outer boundary), it's just the heavier option.

```bash
scripts/build-wheel.sh                 # builds the SPA + a wheel (the image carries the UI; no Node inside)
cd deploy && docker compose up -d      # serves on :6180; the WS gateway is on :6181
docker compose logs chara           # prints the access token
```

Past loopback you need TLS in front. The supervisor serves the UI on `6180` and the WebSocket gateway on `6181`; your proxy presents one HTTPS origin and path-routes the WS upgrade. Caddy (auto-HTTPS):

```caddyfile
your-host.example.com {
    @ws path /hub* /chara/*
    reverse_proxy @ws 127.0.0.1:6181   # WebSocket routes
    reverse_proxy 127.0.0.1:6180       # everything else
}
```

The Host/Origin allowlist is loopback + the bound host only, so name your domain or the proxy is rejected (403): `CHARA_ALLOW_HOST=your-host.example.com`. Then bookmark `https://your-host/#token=<TOKEN>`.

Carrying a long `#token=` URL on a phone is awkward, so a non-loopback bind also accepts a **password** — bookmark the bare URL and log in. Set `CHARA_PASSWORD=…`, or leave it unset and OpenCharaAgent generates one on first start and prints it once (only a PBKDF2-HMAC-SHA256 hash is stored). The local app never shows a login screen.

</details>

<details>
<summary>The chara CLI (headless / over SSH)</summary>

Bare `chara` opens the webui desktop; `chara tui` opens a roster of your charas (resume-first), not a fresh session.

```bash
chara tui              # roster: pick a chara to attach, or press n to create one
chara ls               # name / character / status / isolation / last active
chara attach muse      # attach (you adopt its background loop while attached)
chara start muse       # let it live in the background
chara start-all        # bring everyone back after a reboot
chara desktop --daemon # the resident supervisor; `daemon status` / `daemon stop`
chara new muse --isolation admin
```

In a session, everything is a `/command` — `/help`, `/aspiration`, `/skills`, `/mcp`, `/status`, `/memory`, `/files`, `/mode live|chat`, `/patience`, `/net on|off`, `/allow-dir`, `/settings`, `/exit`. Verbose output goes to a side panel so the console stays a clean chat log; `! <cmd>` runs your own shell command in the chara's jail.

Frontend dev loop: `uv run chara desktop --no-open` in one terminal, `cd apps/web && npm run dev` in another (Vite proxies to the backend).

</details>

## Messaging gateways

A chara can also live in your chat apps. In the desktop app's **Gateways** page (or `chara gateway NAME` headless), connect personal WeChat, QQ, Telegram, Discord, or Slack — config lives in `~/.chara/sessions/NAME/messaging.json`, login credentials stay in a separate per-platform file. Only `say`/`speak` text is delivered; muse and tool chatter never leave. An empty `allowed_senders` is open to anyone (you'll get a warning at startup) — add ids to lock it down.

| Platform | How |
| --- | --- |
| **WeChat** | iLink/ClawBot (`weixin`) — scan a QR. Lowest ban risk, grayscale-gated. |
| **QQ** | OneBot v11 via NapCat — OpenCharaAgent is the WS client; it never holds credentials. |
| **Telegram** | A `@BotFather` bot token, long-polled. No public URL needed. |
| **Discord** | A bot token over the native Gateway WebSocket — enable the Message Content intent. |
| **Slack** | Socket Mode — an app-level `xapp-` token plus a bot `xoxb-` token. No public URL needed. |

These are built but not yet hardened against real-world credentials — treat them as beta. See [SECURITY.md](SECURITY.md) for the trust model.

## Roadmap

The foundations are done: ST-compatible cards, composable tools with native tool calling, the sandbox, persistent `live`/`chat` charas, transcript + bounded memory, self-written skills, MCP, the aspiration → task goal model, the typed event protocol, the three-zone prompt stack, the desktop app, and the messaging gateways. What's left is mostly the characters themselves:

- **The chara curriculum** *(the big one)* — neutral prompt guidance so any worldview can live well: how to use tools, treat goals, spend unattended time — suggestions, never orders. Next: cross-worldview eval cards and a browse path for curiosity.
- **Card packs** — the Market and faithful card import shipped; what's left is our own shareable pack format + index (`chara-pack.json`) so creators can publish card+asset packs.
- **A packaged app** — drag-to-Applications DMG / AppImage, so it isn't clone-only.
- **World-info parity** — recursive scan, cooldown/delay, insertion depth, probability, whole-word matching (`content/worldinfo.py`).
- **Messaging & remote** — live-test the gateways with real accounts; a remote TUI client over the gateway.

## License

Apache-2.0 — see [LICENSE](LICENSE).
