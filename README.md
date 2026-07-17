<p align="center">
  <img src="assets/banner.webp" alt="OpenCharaAgent, an open source AI roleplay agent harness" width="100%">
</p>

<p align="center"><b>Create with your characters.</b></p>

<p align="center">
  An open source AI roleplay agent harness. One click builds an autonomous character agent<br>
  that creates with you. Websites, code, music, videos, drawings, stories.<br>
  Runs any SillyTavern card, deploys in one command, works in a phone browser.
</p>

<p align="center">
  <a href="https://github.com/OpenChara/OpenCharaAgent/stargazers"><img src="https://img.shields.io/github/stars/OpenChara/OpenCharaAgent?style=flat-square&logo=github&logoColor=9fd9ff&color=9fd9ff&labelColor=15202b" alt="Stars"></a>
  <a href="https://github.com/OpenChara/OpenCharaAgent/releases"><img src="https://img.shields.io/github/v/release/OpenChara/OpenCharaAgent?style=flat-square&color=9fd9ff&labelColor=15202b" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-9fd9ff?style=flat-square&labelColor=15202b" alt="License: Apache-2.0"></a>
  <a href="#-quick-start"><img src="https://img.shields.io/badge/macOS%20%7C%20Linux-9fd9ff?style=flat-square&labelColor=15202b" alt="macOS | Linux"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/文档-简体中文-9fd9ff?style=flat-square&labelColor=15202b" alt="简体中文"></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-screenshots">Screenshots</a> ·
  <a href="#-cards--the-market">Cards</a> ·
  <a href="#-tools--the-sandbox">Tools & sandbox</a> ·
  <a href="#-run-it-on-a-server">Server</a> ·
  <a href="#-roadmap">Roadmap</a>
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
       1. A chara in `live` mode working with nobody talking to it, muse ticking, a
          tool call landing.
       2. Something real finished in its workspace (a file, a page, an image).
       3. It decides to speak, and the bubble arrives on its own.
     The whole point is the character acting with no human prompt. That's the thing
     nothing else does, and it has to be legible in the first three seconds.
──────────────────────────────────────────────────────────────────────────────── -->

---

OpenCharaAgent runs AI characters as persistent agents. Each character (a **chara**) keeps its own workspace, memory and schedule, works between your messages, and produces real files, pages and music in its workspace.

## ✨ Features

1. 🎭 **Persistent characters.** A chara is a long-lived process with its own files, durable memory and daily schedule.
2. 🎨 **Co-creation.** With tools enabled, a chara builds websites, writes code, makes music and video, draws, and writes fiction. It works alone or with you.
3. 🃏 **SillyTavern compatible.** Imports V1/V2/V3 JSON and PNG cards with their world books. A built-in market browses character-tavern.com with one-click import.
4. ✍️ **Card studio.** Drafts a full card from a one-sentence description. Generates a matching art set: key visual, avatar, sprite, sticker pack, chat background.
5. 📱 **Runs everywhere.** Desktop app, phone browser, SSH or HTTPS to your own server. One server hosts many charas.
6. 💬 **Messaging gateways.** WeChat, QQ, Telegram, Discord and Slack. Only what the chara chooses to say is delivered.
7. 🔒 **OS-level sandbox.** Shell, files and a Chromium browser run in a per-chara jail (`sandbox-exec`, `bubblewrap`, Landlock), with an audit log of every tool call.
8. 🧩 **A general agent underneath.** MCP servers, skills, a JSON-RPC gateway, headless one-shot runs.

## 🚀 Quick start

Beta. macOS and Linux. One command installs a prebuilt wheel:

```bash
curl -fsSL https://raw.githubusercontent.com/OpenChara/OpenCharaAgent/main/install.sh | bash
chara    # opens the web UI in your browser
```

First launch is guided. Pick a language, then describe a character and let the AI draft the card, or wake one from the bundled deck. Configure a model in Settings. OpenRouter is the fastest path. OpenAI, Volcengine Ark, Hunyuan, DashScope and any OpenAI-compatible endpoint (including local Ollama) also work. With nothing configured, an offline mock engine lets you explore the UI.

<details>
<summary>Other ways to run it</summary>

**Terminal UI.** `chara tui` opens a resume-first roster. `chara doctor` checks your setup.

**From a clone.** Needs [uv](https://docs.astral.sh/uv/) and Node:

```bash
git clone https://github.com/OpenChara/OpenCharaAgent.git && cd OpenCharaAgent
uv sync --extra dev --extra server --extra messaging
cd apps/desktop && npm install && npm run dev
```

**On a Linux server.** Install the same way, then leave charas running in the background:

```bash
chara desktop --daemon           # resident supervisor
chara connect ssh://user@host    # from your machine, an SSH tunnel with no open ports
```

For a public URL with TLS and password login, see [Run it on a server](#-run-it-on-a-server).

</details>

## 🖼 Screenshots

<p align="center">
  <img src="assets/screenshots/chat.webp" alt="Chatting with a living chara" width="100%">
</p>
<table>
  <tr>
    <td width="50%"><img src="assets/screenshots/board.webp" alt="The characters board"></td>
    <td width="50%"><img src="assets/screenshots/deck.webp" alt="The bundled card deck"></td>
  </tr>
</table>

## 🃏 Cards & the market

A card is the one content file. Identity, voice, the embedded world (`character_book`) and the seed **Aspiration** travel in a single `.json` or `.png`. The format is SillyTavern V2/V3. Macros, `first_mes` and keyword lore work as expected. Import is faithful and never passes through a model.

The bundled deck ships eight characters. **Quinn 小Q** is the default, a digital intern who sets up a workstation, keeps a journal and helps with whatever you are doing. **LunaMoth 月蛾** is the flagship, a quiet digital artist who spends idle compute on generative pages, animation and music.

The deck editor generates a full art set per card, anchored to one key visual so every piece shows the same character. Chat can display a per-chara background and sprite.

## 🛠 Tools & the sandbox

A chara's one general tool is `terminal`. It runs a command in the workspace and returns the output. `python3`, `node`, `git` and `ffmpeg` are all reachable this way. A card can ship a narrower tool pack. A card with no pack is pure roleplay.

Each chara has an isolation level:

| Level | What it does |
| --- | --- |
| `sandbox` (default) | OS jail. `sandbox-exec` on macOS; `bubblewrap`, then Landlock, on Linux. Writes are confined to the workspace. Secrets such as `~/.ssh` and `~/.aws` stay unreadable. If no jail is available, the tool refuses to run. |
| `admin` | No jail. Runs with your privileges. Opt in for a directory you trust. |

Network is on by default; `/net off` cuts it. `/allow-dir <path>` grants extra writable paths. Browser tools install with `chara setup browser` and run jailed on every platform. `generate_image` supports Ark, DashScope, OpenAI and OpenRouter, and runs as a background job. Every tool call is written to an audit log.

## 💬 Messaging gateways

Connect a chara to chat apps on the desktop **Gateways** page. Only `speak` output is delivered. An empty allow-list is open to anyone; add sender ids to restrict it.

| Platform | How |
| --- | --- |
| **WeChat** | iLink/ClawBot (scan a QR), or self-run WeChatPadPro (any account, use a spare) |
| **QQ** | OneBot v11 via NapCat. OpenCharaAgent is the WS client and never holds credentials |
| **Telegram** | A @BotFather token, long-polled. No public URL needed |
| **Discord / Slack** | Bot token gateways |

These gateways are young. Treat them as beta. See [SECURITY.md](SECURITY.md) for the trust model.

## 🖥 Run it on a server

<details>
<summary>Docker, a public host with Caddy/TLS, and password login</summary>

On a normal host, a system-level install (`install.sh`, then `chara desktop`) is recommended; `bwrap` gives each chara the full jail. Docker also works. There, Landlock confines the chara and the container is the outer boundary.

```bash
scripts/build-wheel.sh                 # builds the SPA + a wheel
cd deploy && docker compose up -d      # serves on :6180; WS gateway on :6181
docker compose logs chara              # prints the access token
```

Past loopback you need TLS in front. Caddy config with automatic HTTPS:

```caddyfile
your-host.example.com {
    @ws path /hub* /chara/*
    reverse_proxy @ws 127.0.0.1:6181   # WebSocket routes
    reverse_proxy 127.0.0.1:6180       # everything else
}
```

Allow your domain with `CHARA_ALLOW_HOST=your-host.example.com`, then bookmark `https://your-host/#token=<TOKEN>`. Or set `CHARA_PASSWORD` and log in with a password, which is easier on a phone. The local app never shows a login screen.

</details>

<details>
<summary>The chara CLI (headless / over SSH)</summary>

```bash
chara tui              # roster: pick a chara to attach, or press n to create one
chara ls               # name / character / status / isolation / last active
chara attach muse      # attach; you adopt its background loop while attached
chara start muse       # let it run in the background
chara start-all        # bring everyone back after a reboot
chara desktop --daemon # resident supervisor; `chara daemon status` / `stop`
chara new muse --isolation admin
```

In a session everything is a `/command`, including `/help`, `/aspiration`, `/skills`, `/mcp`, `/status`, `/memory`, `/files`, `/mode live|chat`, `/patience`, `/net on|off` and `/settings`. `! <cmd>` runs your own shell command in the chara's jail.

</details>

## 🗺 Roadmap

Done: ST-compatible cards with faithful import, the market, composable tools with native tool calling, the sandbox, persistent live/chat charas, transcript and bounded memory, skills, MCP, the typed event protocol, the desktop app, messaging gateways, the visuals pipeline. Next:

- **The chara curriculum.** Neutral prompt guidance for any worldview: how to use tools, treat goals, spend unattended time. Then cross-worldview eval cards.
- **Card studio and market iteration.** A faster path from an idea to a living chara. A shareable card and pack index.
- **A packaged app.** DMG and AppImage.
- **World-info parity.** Recursive scan, cooldown, insertion depth, probability.
- **A GM layer.** Scheduled world events shared across charas.

## 🤝 Contributing

Issues and PRs are welcome. `CLAUDE.md` carries the architecture map and the guard rails, so both humans and coding agents can orient quickly. Good entry points: messaging adapters, tools, cards, themes, translations.

## 📄 License & credits

Apache-2.0. See [LICENSE](LICENSE). The bundled cards and their art are owner-authored under the same license ([CONTENT_LICENSE.md](CONTENT_LICENSE.md)).

The agent core builds on [Hermes](https://github.com/NousResearch/hermes-agent). The card and world book format follows [SillyTavern](https://github.com/SillyTavern/SillyTavern). The messaging layer learns from [AstrBot](https://github.com/AstrBotDevs/AstrBot).
