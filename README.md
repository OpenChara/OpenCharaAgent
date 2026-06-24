<p align="center">
  <img src="assets/banner.png" alt="LunaMoth — Original Character That Lives With You" width="100%">
</p>

<p align="center"><i>Give your original character a computer to live in.</i></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/文档-简体中文-9fd9ff.svg" alt="简体中文"></a>
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

---

LunaMoth runs an AI character as a persistent being that lives in a computer. It has its own sandbox, its own memory, its own pace — it thinks and makes things between your messages, and decides for itself when something is worth telling you. Strip the persona away and what's left is a capable agent: shell, files, a browser, code, all behind an allowlisted, audited gateway.

It started as a roleplay frontend that could actually *do* things, and grew into a small runtime. The character card is the one file that matters — identity, voice, and the character's world all travel inside it. You bring the card and a model; LunaMoth composes the rest:

```text
[character card: persona + embedded world] + [tools] + [bounded memory] + [sliding context]
```

The agent core borrows heavily from [Hermes](https://github.com/NousResearch/hermes-agent); the card/world-book format is [SillyTavern](https://github.com/SillyTavern/SillyTavern)'s.

## Quick start

It's beta, macOS and Linux. First launch is a welcome screen: pick a provider (OpenRouter / OpenAI / Ollama / Mock), then describe a character and let the AI draft the card, or pick one from the bundled deck. `/settings` changes anything later.

### On your Mac

The one-line installer (a prebuilt wheel, no Node build), then open the UI in your browser:

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth              # opens the webui in your browser  (lunamoth tui = terminal UI; lunamoth doctor checks your setup)
```

> While the repo is private, the installer fetches the release wheel through a token — prefix the command with `GITHUB_TOKEN=<a PAT with repo:read>`. To build from source instead, append `| bash -s -- --dev`.

Or run the full desktop app from a clone (this is how we develop it) — needs [uv](https://docs.astral.sh/uv/) + Node:

```bash
git clone https://github.com/Lunamos/LunaMoth.git && cd LunaMoth
uv sync --extra dev --extra server --extra messaging
cd apps/desktop && npm install && npm run dev      # opens the desktop window
```

### On a Linux server, reached from your browser

Install on the server and leave a chara living in the background:

```bash
curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
lunamoth desktop --daemon      # resident supervisor; charas keep running between visits
```

> Same token note as above: while the repo is private, prefix with `GITHUB_TOKEN=<a PAT with repo:read>` (or `| bash -s -- --dev` to build from source).

Then, from your own machine, tunnel in over SSH — no open ports, encryption and auth come from SSH, and your browser opens pointed at the server:

```bash
lunamoth connect ssh://user@your-server
```

Prefer a real public URL (TLS, a bookmarkable address, optional password login)? See [Run it on a server](#run-it-on-a-server).

## What makes it different

A LunaMoth character isn't a chat session you open and throw away. It's a **chara** — a persistent process with its own files and memory under `~/.lunamoth/sessions/<name>/`. You *attach* and *detach*; it keeps living in between.

- **It runs on its own.** In `live` mode the chara keeps working between your messages — reading, writing, making things — and reaches out (the `speak` tool) only when it decides to. `patience` sets the rhythm. In `chat` mode it just answers you.
- **Two registers.** What it tells *you* (`say`) is separate from its own inner life (`muse`). You see the muse in the desktop app; messaging channels only get the `say`.
- **Real agency, real fences.** Tools run inside a per-session OS jail that confines writes to the workspace and hides your secrets — and *refuses* to run rather than quietly dropping the jail (see [Tools & the sandbox](#tools--the-sandbox)).
- **Memory you can trust.** Durable memory is a token-capped file the chara edits through tools, not a bottomless log. Every tool call is written to `sandbox/logs/audit.jsonl`.

The desktop app (a thin Electron window over the local server) is the main way to use it. A resident `lunamothd` supervisor keeps charas alive in the background and notifies you when one wants to talk. There's also a frozen-but-working terminal UI (`lunamoth tui`) for headless use.

## A model

An API endpoint is the easy path — OpenRouter is the fastest: paste an `sk-or-…` key, name a model, test, go. Any OpenAI-compatible server works too, including local ones:

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

With nothing configured, LunaMoth falls back to an offline mock engine — enough to click around during development. (Drafting a card from a description wants a real model — DeepSeek V4 Flash or better.)

## Cards & content

A card is the one content file: identity, voice, the embedded world (`character_book`), seed wishes, and limits all in a single `.json` or `.png` (SillyTavern V2/V3 — our cards *are* that format). `{{char}}`/`{{user}}` macros, `first_mes`, and keyword-triggered lore all work.

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
| `sandbox` (default) | OS jail — `sandbox-exec` on macOS, `bubblewrap` → `Landlock` on Linux. Writes confined to the workspace; the rest of your `$HOME` (`~/.ssh`, `~/.aws`, `~/.lunamoth`) is unreadable. If no jail is available it refuses to run — it never silently degrades. |
| `admin` | No jail: runs as you, cwd in the workspace. Opt-in, for a directory you trust. |

Permissions flex at runtime: network is on by default (`/net off` to cut it), and `/allow-dir <path>` grants a writable path outside the workspace. Browser tools (`browser_*`, a real Chromium) are optional — `lunamoth setup browser` installs the driver; they run jailed on all platforms.

## Run it on a server

The Quick Start above gets you in over SSH with no open ports. If instead you want a real public URL — a bookmarkable HTTPS address, optionally with password login — here's the rest.

<details>
<summary>Docker, a public host with Caddy/TLS, and password login</summary>

A system-level install (`install.sh` / `lunamoth desktop`) is recommended over Docker on a normal host — `bwrap` gives each chara the full jail. Docker works too (it falls back to Landlock for filesystem confinement, with the container as the outer boundary), it's just the heavier option.

```bash
scripts/build-wheel.sh                 # builds the SPA + a wheel (the image carries the UI; no Node inside)
cd deploy && docker compose up -d      # serves on :6180; the WS gateway is on :6181
docker compose logs lunamoth           # prints the access token
```

Past loopback you need TLS in front. The supervisor serves the UI on `6180` and the WebSocket gateway on `6181`; your proxy presents one HTTPS origin and path-routes the WS upgrade. Caddy (auto-HTTPS):

```caddyfile
your-host.example.com {
    @ws path /hub* /chara/*
    reverse_proxy @ws 127.0.0.1:6181   # WebSocket routes
    reverse_proxy 127.0.0.1:6180       # everything else
}
```

The Host/Origin allowlist is loopback + the bound host only, so name your domain or the proxy is rejected (403): `LUNAMOTH_ALLOW_HOST=your-host.example.com`. Then bookmark `https://your-host/#token=<TOKEN>`.

Carrying a long `#token=` URL on a phone is awkward, so a non-loopback bind also accepts a **password** — bookmark the bare URL and log in. Set `LUNAMOTH_PASSWORD=…`, or leave it unset and LunaMoth generates one on first start and prints it once (only a PBKDF2-HMAC-SHA256 hash is stored). The local app never shows a login screen.

</details>

<details>
<summary>The chara CLI (headless / over SSH)</summary>

Bare `lunamoth` opens the webui desktop; `lunamoth tui` opens a roster of your charas (resume-first), not a fresh session.

```bash
lunamoth tui              # roster: pick a chara to attach, or press n to create one
lunamoth ls               # name / character / status / isolation / last active
lunamoth attach muse      # attach (you adopt its background loop while attached)
lunamoth start muse       # let it live in the background
lunamoth start-all        # bring everyone back after a reboot
lunamoth desktop --daemon # the resident supervisor; `daemon status` / `daemon stop`
lunamoth new muse --isolation admin
```

In a session, everything is a `/command` — `/help`, `/aspiration`, `/skills`, `/mcp`, `/status`, `/memory`, `/files`, `/mode live|chat`, `/patience`, `/net on|off`, `/allow-dir`, `/settings`, `/exit`. Verbose output goes to a side panel so the console stays a clean chat log; `! <cmd>` runs your own shell command in the chara's jail.

Frontend dev loop: `uv run lunamoth desktop --no-open` in one terminal, `cd apps/web && npm run dev` in another (Vite proxies to the backend).

</details>

## Messaging gateways

A chara can also live in your chat apps. In the desktop app's **Gateways** page (or `lunamoth gateway NAME` headless), connect personal WeChat, QQ, or Telegram — config lives in `~/.lunamoth/sessions/NAME/messaging.json`, login credentials stay in a separate per-platform file. Only `say`/`speak` text is delivered; muse and tool chatter never leave. An empty `allowed_senders` is open to anyone (you'll get a warning at startup) — add ids to lock it down.

| Platform | How |
| --- | --- |
| **WeChat** | iLink/ClawBot (`weixin`, scan a QR — lowest ban risk, grayscale-gated), or self-run [WeChatPadPro](https://github.com/WeChatPadPro/WeChatPadPro) (`weixinpad`, any account — but real ban risk, use a spare). |
| **QQ** | OneBot v11 via NapCat — LunaMoth is the WS client; it never holds credentials. |
| **Telegram** | A `@BotFather` bot token, long-polled. No public URL needed. |

These are built but not yet hardened against real-world credentials — treat them as beta. See [SECURITY.md](SECURITY.md) for the trust model.

## Roadmap

The foundations are done: ST-compatible cards, composable tools with native tool calling, the sandbox, persistent `live`/`chat` charas, transcript + bounded memory, self-written skills, MCP, wishes, the typed event protocol, the three-zone prompt stack, the desktop app, and the messaging gateways. What's left is mostly the characters themselves:

- **The chara curriculum** *(the big one)* — neutral prompt guidance so any worldview can live well: how to use tools, treat goals, spend unattended time — suggestions, never orders. Next: cross-worldview eval cards and a browse path for curiosity.
- **Card studio & market** — a faster inspiration→living-chara path in the deck, plus a shareable card/pack index (with proper card + asset import).
- **A packaged app** — drag-to-Applications DMG / AppImage, so it isn't clone-only.
- **World-info parity** — recursive scan, cooldown/delay, insertion depth, probability, whole-word matching (`content/worldinfo.py`).
- **Messaging & remote** — live-test the gateways with real accounts; a remote TUI client over the gateway.

## License

Apache-2.0 — see [LICENSE](LICENSE).
