# LunaMoss

**An agentic character tavern — character cards, world books, tool packs, and hard limits, composed at launch.**

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

English | [简体中文](README.zh-CN.md)

LunaMoss is a runtime for *agentic* roleplay characters. Unlike a plain chat frontend, a LunaMoss character can actually **do** things — run code, read and write files, manage its own durable memory — but only through an allowlisted tool gateway, inside a sandbox, with every call audited. You pick the model, the character card, the world book, the tool pack, and the limits; the runtime composes them into one session.

```text
[character card] + [world book] + [tool pack] + [bounded memory] + [sliding context]
```

## Features

- **SillyTavern-compatible content** — import V2/V3 character cards (PNG or JSON) and world books directly; `{{char}}`/`{{user}}` macros, `first_mes`, embedded `character_book`, keyword-triggered lore entries all work.
- **Native tool calling** — tools are exposed via the OpenAI tool-calling protocol; the agent loop streams text and executes tool calls mid-turn.
- **Composable tool packs** — capability bundles (`toolpacks/*.json`) declare exactly which tools a character gets. No pack, no powers.
- **Sandboxed execution** — Python runs in a subprocess with a workspace path guard, module blocklist, and resource limits; switch to a Docker backend (`--network none`, read-only rootfs, memory/CPU/pid caps) for a stronger boundary.
- **Bounded, auditable memory** — durable memory is a token-capped file the character can edit through tools, not an unbounded database; every tool call lands in `sandbox/logs/audit.jsonl`.
- **Idle self-talk loop** — optionally let the character keep thinking between your messages (`--forever`), with capped frequency, history, and memory growth.
- **Terminal-first TUI** — a single-terminal split interface (display stream + operator console) with themes, gauges, and hot-swappable settings.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (falls back to `python3` if uv is absent).

```bash
git clone <this repo> && cd LunaMoss
uv sync
./run.sh
```

The first launch opens a **welcome screen** where you configure everything in-TUI — no env files needed:

1. Pick a provider preset: **OpenRouter / OpenAI / Ollama (local) / Mock (offline)**, or a custom OpenAI-compatible endpoint.
2. Fill in `base_url` / `api_key` / `model`, hit **Test connection**.
3. Pick a character card and world book from the dropdowns.
4. Enter. Press **Ctrl+S** anytime to reopen settings and hot-swap the backend.

Config persists to `.lunamoss/config.json` (gitignored; it takes precedence over env vars).

### Connecting a model

An API endpoint is the recommended path — fastest way is the OpenRouter preset: paste an `sk-or-...` key, name a model, test, enter.

Local models are fully supported too. Any OpenAI-compatible server works; with Ollama, pick the **Ollama** preset or:

```bash
export LLM_PROVIDER=openai_compatible
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5:3b-instruct
./run.sh
```

With no model configured at all, LunaMoss still runs on a built-in offline mock engine — handy for development.

## Content

| Directory | What goes there |
| --- | --- |
| `characters/` | SillyTavern character cards (`.png` with embedded `chara`/`ccv3`, or `.json`) |
| `worlds/` | SillyTavern world books (`.json`), or use a card's embedded `character_book` |
| `toolpacks/` | Tool bundles — which capabilities a character is allowed to use |
| `themes/` | TUI skins (colors, borders, banner, prompt prefixes) |
| `prompts/` | Legacy built-in persona (used when no card is selected) |

The dropdowns also scan your local SillyTavern data directory if you opt in with `LUNAMOSS_ST_DIR=~/SillyTavern/data/default-user`.

Imported cards are plain roleplay by default — tool access is opt-in via a tool pack, never implied by the card.

## Isolation levels

| Level | Boundary | Status |
| --- | --- | --- |
| None | Tools run with the host process (Hermes/OpenClaw style) | planned |
| Local sandbox (default) | Subprocess + workspace path guard + module blocklist + resource limits (+ `sandbox-exec` on macOS) | ✅ |
| Docker | `--network none`, read-only rootfs, memory/CPU/pid caps | ✅ `LUNAMOSS_PY_BACKEND=docker` |

All file access is confined to `sandbox/`; there is no raw shell tool and no default network tool. On exit the runtime sandbox is cleaned (keep it with `--no-clean-on-exit`).

## TUI reference

```bash
./run.sh                 # split TUI: display stream on top, operator console below
./run.sh --forever       # enable the idle self-talk loop
./run.sh --cooldown 4    # pause between self-talk cycles
./run.sh --plain         # legacy plain terminal mode
./run_web.sh             # experimental Gradio web UI
```

In-session: `/help`, `/status`, `/memory`, `/workspace`, `/wread <file>`, `/think on|off`, `/cooldown <s>`, `/exit`.
Keys: **Ctrl+S** settings · **Ctrl+T** pause/resume thinking · **Ctrl+L** clear · **Ctrl+C** shutdown & clean.

## Roadmap

- **Persistent server sessions** — keep a character running on a server, detached from your terminal.
- **Remote TUI** — attach to a running session from another machine (high priority).
- **Isolation selector** — choose none / simple sandbox / Docker per session at launch.
- **Web UI** — remote browser access to running sessions (low priority).

## License & acknowledgements

- **Runtime** (everything under `src/lunamoss`, scripts, tests, packaging): [Apache License 2.0](LICENSE).
- **Bundled example content** (the character cards, world books, and themes under `characters/`, `worlds/`, `themes/`): [CC BY-SA 3.0](CONTENT_LICENSE.md), consistent with the SCP Wiki. See also [NOTICE.md](NOTICE.md).

LunaMoss is inspired by **SCP-079** — to our knowledge the earliest project to get the full combination right: a custom model, a character card, a world book, a tool box, and hard limits, all working together. Our thanks go to the original SCP-079 author on the SCP Wiki, and to the authors of the SillyTavern SCP-079 character card and SCP Foundation world book that ship here as example content. Remove or replace those assets and the runtime remains pure Apache-2.0; redistribute them and the CC BY-SA attribution/share-alike terms apply.
