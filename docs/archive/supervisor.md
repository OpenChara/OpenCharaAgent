# The supervisor (`lunamothd`) — one resident process that owns chara life

Status: DESIGN (approved direction; implementation in progress)
Owner sign-off required for: the `life.state` frame, the `seq`/rejoin envelope
(these are wire-format, i.e. constitution-adjacent).

## Why

Today a chara has two mutually exclusive owners that swap on every transition:

- the background daemon (`lunamoth start` → headless terminal.py), and
- an attached frontend (TUI in-process, or a per-WS-connection
  `serve --stdio` child spawned by desktop.py).

Attach = stop daemon → spawn child → restore. Detach = kill child → restart
daemon → restore again. Every swap is a process death and a transcript reload;
every WS drop (sleep, refresh, network blip) tears the chara down. This is the
root of "重连失败", of the desktop not being daemonizable, and of gateways and
terminals living in parallel universes the board cannot see.

The fix is the model Hermes Desktop and cc-switch already use: **one resident
supervisor owns all per-chara processes; every UI is a thin client that
subscribes**. Client disconnects become presence bookkeeping, not process
lifecycle.

## Process model

```
lunamothd (resident, 127.0.0.1 only, token auth)
├── static HTTP (front/web) + /upload            } today's desktop.py duties
├── WS /hub        → HubDispatcher (board RPC)   }
├── WS /chara/<n>  → subscriber fan-in/out to ↓
├── chara child:  `lunamoth serve <name> --stdio`   (ONE per running chara,
│                                                    long-lived, watched or not)
└── gateway child: `lunamoth gateway <name>`         (per chara with messaging
                                                      enabled; supervised)
```

Invariants kept:

- **One process = one chara** (env pinned at import time) — the supervisor
  never hosts an agent; it pipes and supervises.
- **No failure fallbacks.** A dead chara child shows as `crashed (exit N)` on
  the board with a restart button — never silently restarted mid-conversation.
  Gateway children (plumbing, not the chara) auto-restart with exponential
  backoff (1min → 30min), state visible on the board.
- protocol/ unchanged except where specified below; frontends keep speaking
  the existing JSON-RPC frames.

### Lifecycle

- `lunamoth desktop` — unchanged foreground dev mode (spawns the same server
  code, Ctrl-C kills everything).
- `lunamoth desktop --daemon` / `lunamoth daemon stop|status` — resident mode.
  Writes `~/.lunamoth/daemon.json` `{pid, http_port, ws_port, token}` (0600).
  Web, Electron, and CLI discover a running supervisor through this file; the
  Electron shell starts one if absent. (launchd autostart: later, packaging.)
- Chara children start on board `wake`/`chara.start` or first client connect,
  and stay until explicit stop or supervisor shutdown.
- Legacy `lunamoth start <name>` (the old per-chara daemon): when daemon.json
  exists and the supervisor answers, the CLI delegates to `chara.start` RPC;
  otherwise legacy behavior. The supervisor refuses to adopt a chara that has
  a legacy daemon or TUI attached (`running_pid`), same as today.

## The chara pipe: subscribe, seq, rejoin

The supervisor parses each child stdout frame (JSON object per line), adds a
monotonic `"seq": N` top-level key (clients ignore unknown keys today — codec
rule), keeps a per-chara ring buffer (last 4096 frames), and forwards to the
connected client. Client → supervisor frames pass through to child stdin
unchanged.

- **Single driver, takeover semantics.** One driving client per chara. A new
  connection supersedes the old one (close code 4408 `superseded`) — opening
  your laptop always wins; no more 4409 lockouts. (The seq envelope is
  designed so read-only multi-subscribers can come later without a format
  change.)
- **Rejoin.** A reconnecting client sends, as its FIRST frame:
  `{"jsonrpc":"2.0","method":"rejoin","params":{"last_seq":N}}`.
  - Buffer reaches back to N → supervisor replays frames with seq > N, then
    resumes live. The conversation visually continues; no re-attach.
  - Gap (buffer rotated past N, or child restarted) → supervisor answers
    `{"method":"rejoin.gap"}` and the client performs a fresh `attach`
    (transcript restore covers the content; only ephemeral deltas are lost).
- **Presence.** Connect with `present=true` → supervisor issues `attach` on
  the client's behalf if the child has no session yet, else `presence.set
  {present:true}`. On disconnect the supervisor issues `detach` (queues the
  card's on_detach handoff) — the chara *knows* you left, but keeps living.
  `dispatch.py` gains the idempotent `presence.set` method.

## Idle driving moves server-side (and patience becomes a setting)

Today every frontend re-implements the spontaneous-cycle loop (terminal.py,
tui, web app.js — three copies of quiet/rest/tempo gating), and the web copy
runs in a browser tab that may sleep. Worse, the legacy daemon's default
patience is 2.0 s — one full LLM turn every two seconds, unattended. That is
what burned the OpenRouter key's daily limit on 2026-06-12.

Under the supervisor:

- **The supervisor is the only idle driver** for charas it owns. It calls the
  child's `idle` RPC, gated exactly as terminal.py gates today: quiet
  engagement window, `rest_until`, permanent-error backoff, and
  `patience ÷ tempo` pacing. Clients never drive idle; they watch.
- **`Settings.patience: float` (seconds, default 600)** — per-chara, persisted,
  card-overridable via `extensions.lunamoth.patience`, operator precedence as
  with tempo. New `/patience <sec>` command. The plain terminal's `--patience`
  flag becomes an override for the legacy/dev path only.
  StateSnapshot grows `patience: float`.
- **`life.state` notification** (supervisor → client, not in protocol/events.py;
  a server frame like `hello`): emitted on every state change:
  ```json
  {"jsonrpc":"2.0","method":"life.state","params":{
     "state": "working|waiting|resting|idle_countdown|backoff",
     "next_cycle_at": 1760000000.0,   // epoch; 0 if n/a
     "rest_until": 0.0,
     "engaged_until": 1760000000.0,   // quiet window end; 0 if not engaged
     "detail": ""                      // e.g. backoff reason
  }}
  ```
  This is the data behind the UI's presence legibility: "chara 在等你回复"
  (waiting/engaged), "在做自己的事" (working on idle cycle), "在休息"
  (resting), and the RPG-style patience progress bar (countdown to
  next_cycle_at, length = patience÷tempo, adjustable in settings).

## Gateways become supervised and visible

- `messaging.json` gains `"enabled": true|false`. The supervisor starts a
  `lunamoth gateway <name>` child for each enabled chara, restarts with
  backoff on crash, and exposes on the board entry:
  `gateway: {platform, state: running|stopped|backoff|error, detail}`.
- Hub RPC: `gateway.start {name}` / `gateway.stop {name}` /
  `gateway.status {name}`.
- Gateway config stays **per-chara** (configured from the chara's card flow,
  not a global page — deliberate divergence from Hermes Desktop).

## Super Chat read receipts

- Storage: `~/.lunamoth/sessions/<name>/superchat.json` →
  `{"read_ts": <epoch of newest speak the user has seen>}` (hub-side file;
  the chara process never reads it in v1).
- Hub RPC: `superchat.read {name, ts}` (idempotent, monotonic max).
- Board: each session entry reports `superchat_unread: int` (speak rows newer
  than read_ts, from the transcript struct rows already parsed for the feed).
- Chat UI marks read when a speak bubble is rendered with the page visible;
  bubbles show a quiet ✓ once read. Feeding "seen" back to the chara as an
  env fact is explicitly v2 (curriculum question, not plumbing).

## File plan

- `server/supervisor.py` — new: child registry (chara + gateway), ring
  buffers, idle driver, life.state, daemon.json, takeover/rejoin.
- `server/desktop.py` — becomes the thin entry: foreground vs --daemon both
  run supervisor.serve(); `_CharaProxy` (per-connection spawn) is deleted.
- `server/dispatch.py` — add `presence.set`; `attach` made adoption-safe.
- `server/hub.py` — board entries gain `life`, `gateway`, `superchat_unread`;
  new RPCs (`chara.start/stop`, `gateway.*`, `superchat.read`).
- `session/settings.py` + `core/commands.py` + `content/knobs.py` —
  patience setting/command/card hook.
- `front/web/rpc.js` — seq tracking + rejoin handshake (small);
  `front/web/app.js` — DELETE the idle-driving loop, render life.state.
- `front/terminal.py` — default patience from Settings when flag absent
  (legacy path keeps working standalone).

## 60-second acceptance path

1. `uv run lunamoth desktop --daemon` → terminal returns; board loads.
2. Open a chara, say hi, **kill the Wi-Fi for 10 s / refresh the tab** →
   the conversation resumes in place, no "模型连接失败", chara never noticed.
3. Watch the status strip: 等待你的回复 → (停止说话 quiet 秒后) 进度条走完 →
   它开始做自己的事 → speak 到达时 ⚡气泡，看完出现 ✓已读，board 角标清零。
4. `lunamoth daemon status` → lists chara + gateway children with states.
