# LunaMoth — open work

This is the open-work doc under `docs/` (owner rule, re-affirmed 2026-06-17:
everything condenses here; settled plans/specs/build-logs get deleted once their
conclusions live in `CLAUDE.md`, the code, and git history; the one sibling file
is `AGENT-FIELD-NOTES.md`, the owner-sanctioned agent-methodology playbook).
What's kept is only what's still *open* or worth remembering:

- **Part 1** — engineering hardening (hermes-parity): the 33-item backlog has
  LANDED; only the genuine remainder is kept.
- **Part 2** — deferred product ideas.
- **Part 3** — active `/loop` backlog (owner requests).
- **Part 4** — 2026-06-17 test-feedback triage (what's open from it).
- **Appendix A** — client + deploy architecture reference (the only durable bits
  of the now-deleted CLIENT-AND-DEPLOY-PLAN; the runbook proper is README + `deploy/`).

---

# Part 1 — Engineering hardening (hermes-parity)

All P1–P3 hermes-parity items (the 33-item backlog) landed and were verified by two
read-only audits on 2026-06-19 (P1s 13/13, P2/P3 17/17). Item detail lives in git
history. The one deliberately-unbuilt item, **worth remembering**:

- **#3 parallel tool execution** (P3 throughput only) — serial execution is
  load-bearing for the audit trail's ordering. Revisit only if throughput becomes a
  real problem.

- **`delegate_task` — RE-ENABLED as a NON-BLOCKING background job** (owner, 2026-06-30).
  Was briefly shelved for hanging the turn; now fixed and live. The fan-out runs on a
  daemon thread and reports via the process-registry completion queue (the same
  background-job shape `generate_image` uses): the call returns a `{status: submitted}`
  receipt immediately, the subagents run alongside the main agent, and the aggregated
  results are drained as a synthetic user message at the next turn boundary. The
  per-child timeout is ENFORCED from each child's OWN start (`_run_fanout`, rewritten
  in the 2026-07-02 sweep P2.4: daemon threads + a semaphore, late results discarded,
  never-started tasks reported honestly past the batch deadline — the earlier
  executor + `fut.result(timeout=…)`/`shutdown(wait=False)` shape is gone).
  Caveat (documented in code): a Python thread can't be
  force-killed, so a stuck worker runs to its own end in the background — but it never
  blocks the parent. To shelve again: wrap the top-level `registry.register` in
  `if False:` (the discovery AST-scan only imports modules with a top-level register).

- **Background-job completion WAKE regardless of mode — DONE (2026-06-30).** A finished
  async job (image gen / delegate / background terminal) now wakes the chara to react,
  in BOTH live and chat mode, exactly like a user message — the same logic for both,
  since a job finishing IS a synthetic user turn. Mechanics: `StateSnapshot.pending_notices`
  is a cheap non-destructive peek (`gateway.has_pending_notifications`); the supervisor's
  idle loop reads it BEFORE the mode branch and drives a one-shot `react` RPC →
  `agent.stream_react`, which drains the notice into context (user role) and runs the
  responsive reply loop (a no-op if nothing pending). `react` is a low-priority stream
  kind like `idle`: a real user `send` supersedes it; `react` never supersedes a real
  turn (it raises -32011 when one is in flight, and the supervisor skips — that turn
  drains the notice itself). Not full self-work — chat mode stays reply-only; the wake
  fires only when there's a real completion to react to.

## Structural root-causes — landed (kept as a record)

The 2026-06-16 diagnosis catalogued a family of drift bugs (Smell A: one fact owned
in several places that drift; Smell B: distinct meanings collapsed into one flag).
All of it landed (isolation single-source incl. the `env_status` runtime copy
removed 2026-06-21; the tool allowlist collapsed to `registry ∩ pack`; explicit tool
status `__tool_error__` + real exit codes; macOS/Linux jail hardening; presence
removed; `LifeState` typed dataclass; `mode`/autonomy single-sourced; hub + supervisor
god-modules split into packages; tools default-open; CI + macOS runner). The last
remainder — **`patience` precedence** — collapsed 2026-06-22: `agent.patience_resolved`
is the ONE precedence owner (commands delegates to it; the TUI consumes the already
resolved `StateSnapshot.patience = effective_patience()`), and the default value + the
explicit-source rule are single-sourced in `knobs.{DEFAULT_PATIENCE,patience_is_explicit}`
(read by both `agent` and `settings.load` — no more duplicated default literal;
DEFAULT_PATIENCE is 3600 today).

---

# Part 2 — Deferred product ideas (worth considering)

Salvaged from the deleted desktop design doc, the webui needs register, and the
hermes-desktop study — product directions deliberately deferred. These overlap
with `CLAUDE.md`'s roadmap (card market, remote access, the chara curriculum);
that roadmap is the source of truth for *direction*, this is the concrete UI/
product backlog behind it.

- **Menu-bar resident** *(the designer's "most wanted")* — a mac menu-bar moth
  icon; close the window and the chara stays alive; a badge = a chara is waiting
  for you; click = a mini board. The ultimate "lives in your computer" form.
  The Electron shell shipped; this is the next shell step.
- **Card-defined custom life-state words** — let a card override the displayed
  `life.state` word (a statue's "resting" could read "weathering"), via
  `extensions.lunamoth`. The engine keeps factual defaults; the card customizes.
- **Artifacts backtrace** — file → the tool call / session message that produced
  it; inline "work cards" in the chat stream. Needs a backend file↔tool-call
  mapping (today works live only on the drawer shelf).
- **Let the chara paint its own portrait** — when the avatar slot is left empty
  at creation, the chara's first act can be "paint myself a portrait." An
  artist's rite of passage that also solves the asset problem.
- **Weekly digest** — a quiet weekly summary (from the transcript) instead of an
  infinite feed.
- **Notifications & quiet hours** — waiting-for-you → system notification;
  respect night DND (a slot is already reserved in Settings · General).
- **Remote VPS residence** — over the existing `serve` WS+token: the desktop
  connects to a backend on an always-on server; the chara lives there, the
  desktop is just a window. (Same as the roadmap's remote-TUI-client item.)
- **Card / pack marketplace** — SHIPPED as the Market view (character-tavern.com
  catalog proxy: browse/sort/filter/preview + faithful import, 2026-06-27..07-01).
  Remaining: our OWN pack format + shareable index (`lunamoth-pack.json`, git-repo
  index) so creators can publish card+asset packs.
- **Multi-chara visiting** — charas on one machine visiting each other; the
  `say|muse` protocol already supports multiple audiences. Far-future.
- **Voice (STT/TTS)** — hermes has the full chain to port; a big "aliveness"
  boost, but only after the core stabilizes.
- **Multimodal output** — image UNDERSTANDING (read_file → vision route) AND image
  GENERATION (`generate_image`, non-blocking background job → MEDIA: delivery on
  every surface) shipped; richer output (audio/video beyond ffmpeg-in-terminal)
  is still deferred.
- **Panel polish leftovers** — memory entry-level editing in the chat drawer is
  still read-only; a board-level context ring + ⚡ high-load chip needs `serve` to
  expose a lightweight last-activity / resource sample.
- **In-character closer for tool-less cards** *(low priority)* — the post-history
  closer (`content/rules.py:_CLOSER`) carries two reminders: stay-in-character AND
  make-real-things. But the whole slot is tool-gated (`agent.py:_post_history_slot`
  returns "" with no tools, asserted by `test_rules.py`), so a pure-roleplay card
  with no tools gets the in-character anchor only at the TOP (render_system), never
  at the closer. Fix later: split the closer so the in-character half fires even
  tool-less while the no-fabrication half stays tool-gated. Deferred — tool-less
  pure-roleplay is not a current focus.
- **Self-contained desktop app (signed DMG / AppImage)** — the consumer install
  should be "drag LunaMoth.app to /Applications, double-click, it works" — not
  the `curl|bash` CLI install (that stays the dev/terminal path). The hard part:
  `apps/desktop` is a THIN Electron shell that spawns the Python backend
  (`lunamoth desktop`); today `electron-builder` (`npm run dist`) bundles only
  the shell, and `main.cjs` finds the backend via a dev checkout or a (currently
  mismatched) installed path — so the DMG today is NOT self-contained and shows
  "No backend found". Plan:
  1. **Freeze the backend** — PyInstaller/py2app into a standalone `lunamoth`
     binary, OR ship a uv-managed standalone Python + the venv as a folder.
     KEY constraint: the supervisor RE-INVOKES the backend as subprocesses
     (`lunamoth serve NAME --stdio` per chara) — the frozen binary must support
     re-exec, and the spawn command must point at the bundled binary. The OS-jail
     isolation (`sandbox-exec` on macOS, the `isolation.py` argv builders) must
     also work from inside the .app bundle.
  2. **Bundle it** — electron-builder `extraResources` puts the frozen backend
     in the .app; `main.cjs`, when `app.isPackaged`, spawns
     `process.resourcesPath/backend/…` instead of the dev/installed discovery.
  3. **Sign + notarize** (Apple Developer ID) — otherwise Gatekeeper blocks the
     .app on any other Mac. Linux: AppImage has the same bundling shape, no
     signing.
  - **Bug to fix regardless** (independent of the DMG work; still open 2026-07-02):
    `apps/desktop/electron/main.cjs` `installedLauncher()` looks for
    `~/.lunamoth/bin/lunamoth`, but `install.sh` links the shim at
    `~/.local/bin/lunamoth` (and the default `user` channel is now a uv tool
    install, whose bin dir is uv's). Fix the discovery to check
    `~/.local/bin/lunamoth` + `uv tool` bin.
  - Icon assets already exist (`apps/desktop/assets/icon.png` + the menu-bar
    `trayTemplate*`). The menu-bar-resident idea (above) composes with this.

### Mobile / responsive webui — DIRECTION DECIDED (owner 2026-06-24), build deferred ("再议")

Decision: **make the existing webui responsive; do NOT build a native app.** If "installable /
push / app-store" is wanted later, wrap the SAME responsive SPA in a PWA / Capacitor shell —
near-zero rewrite — rather than a from-scratch native client.

Why (grounded in the code, 2026-06-24):
- The SPA has **no Electron-specific API usage** (`apps/web/src`: no `ipcRenderer`/`window.electron`/
  `require`) — Electron is just a shell, so the same SPA already runs in a phone browser.
- `index.html` already ships the viewport meta (`width=device-width … viewport-fit=cover`).
- A **partial mobile foundation already exists**: `@media (max-width:680px)` in `global.css`
  already does board→1-col, deck→2-col, settings→stacked nav, chat right-panel→overlay. It's
  incomplete, not absent.
- **Reachability is the same for web and native**: the hub + chara processes live on the user's
  computer, so a phone must reach it over LAN (`http://<ip>:<port>` + the existing password login)
  or a tunnel (the `connect ssh://` / cc-switch remote story). Native does NOT avoid this.
- A native app would re-implement the whole UI + the protocol client (`rpc.ts` transport, the
  event union, `useCharaStream`) + i18n + state, ×2 platforms, for a backend that still lives on
  the desktop = mostly duplication for marginal gain + permanent parallel maintenance.

Scoped build plan (when resumed) — incremental, one commit per area, mostly CSS + a tiny
`useIsMobile()` hook for the few drawer-vs-pane conditionals; standardize on ONE breakpoint
(reuse/raise the existing 680px, or 768px) so the layout flips at a single cutoff:
1. **Shell** (CSS-only, no component change): left `.sidebar` (`<aside>` in `Sidebar.tsx`) →
   fixed bottom tab bar (`#app` is a flex row → it/`.main` get `padding-bottom` + safe-area);
   hide or fold `#statusbar` (the conn dot) on mobile.
2. **Chat** (the core view): hide the `.chat-bg` background on mobile, full-width bubbles, the
   right Profile `.panel` → a slide-up drawer / tab (the 680px block already half-does this).
3. **Deck + cards**: `DeckModal` (`cardview`/`wide` = card editor + wake) → full-screen sheet on
   mobile; deck grid → 1 col; touch-friendly editor tabs; the visuals stage/rail already stacks
   at 680px (tune touch sizes + the sticker grids).
4. **Board / Settings / Gateways / overlays**: board + settings already stack — verify, polish,
   touch-target (44px) pass, safe-area insets.
Effort: medium (a focused pass per view), NOT weeks; reuses all logic/protocol/state. Can't be
device-tested from here, so resume by piloting Chat first and testing on a real phone, then fan out.

STATUS (2026-06-24, d8b46d3): a FIRST pass landed in a new `apps/web/src/styles/mobile.css`
(imported after global.css; kept separate to avoid the concurrently-edited global.css). Done at
`@media(max-width:680px)`: shell left-rail → fixed bottom tab bar (+ `.main` bottom padding,
status strip hidden); cards/editor/wake/overlays → full-screen sheets; chat drops the bg image
+ veil. Plus the pre-existing 680px rules (board 1-col, deck 2-col, settings stacked, chat panel
overlay). REMAINING per-view polish: touch-target (44px) sweep; the visuals stage/rail + sticker
grids on mobile; card-editor tabs / world-book editor; the Profile right-panel as a proper drawer
(today it's an overlay); composer + attachment tray sizing; deck spine actions; landscape. Resume
by testing the deployed instance on a phone and tightening per-view from there.

### Web tools — FUTURE (owner 2026-06-19, deferred LOW; settled — do not re-litigate)
web_search/web_extract are intentionally OFF (`_WEB_TOOLS_ENABLED = False`). Keeping
them off makes the chara do less fruitless trial-and-error, and we run no search
backend, so the current state is GOOD. When the time comes, re-enable as OPT-IN behind
a **user-supplied web key** (port `reference/hermes-agent/tools/web_tools.py`); the
web-key UI slot goes in Settings · 模型, below the other multimodal models and above
背景去除. HARD requirement on re-enable: a failed web call must return a real
`tool_error`, NOT the silent empty-`{}` fallback the old path produced.

### Hunyuan image generation — DEFERRED (research kept; the other 4 providers shipped)
Multi-provider image gen shipped for 火山方舟 Ark / 阿里云 DashScope / OpenAI /
OpenRouter (`content/image_providers.py` + `tools/builtin/_image_gen.py`). **Hunyuan
image is the one not done**: its OpenAI-compatible endpoint
`https://api.hunyuan.cloud.tencent.com/v1` does NOT expose `images/generations`
(chat/completions + vision only — confirmed via Tencent doc 1729/111007). Image gen is
the **native Tencent Cloud API** (TC3-HMAC-SHA256 signed, async Submit/Query
HunyuanImageJob) — the OpenAI-compat key can't sign it. The risky adapter (signing +
async); confirm the request-level format before implementing.

---

# Part 3 — Active loop backlog (owner requests, 2026-06-16)

Managed by the `/loop` dev cycle: each iteration picks the top OPEN item, writes
its plan + acceptance here, implements (parallel subagents where independent),
runs tests, has an **audit subagent** verify against the acceptance bar (and
parity with `reference/hermes-agent` for commodity surfaces), confirms
**functionality with a live Quinn** (wake → self-check tools → read its jsonl →
delete), then **removes the item from this list**. Anyone (incl. subagents) may
add a diagnosed problem here with a priority. Independent items run in parallel.

## 2026-07-02 audit sweep — RESOLVED 2026-07-03 (P1+P2+P3 all fixed)

Every finding from the six-agent audit was fixed, tested, and adversarially
re-audited (a read-only review agent tried to refute each core fix; no
high/critical issues survived). Full original findings live in git history.
Concise resolved record:

### P1 — correctness/security (all 5 fixed)
1. FIXED — `core/llm.py` tracks unanswered `tool_call` ids (`pending_tools`);
   the `finally` synthesizes `[interrupted]` tool results for leftovers, so an
   abandoned generator can't leave N tool_calls with <N results. Also: a
   stream/HTTP EXCEPTION now gets an honest `ERROR_CUT_MARK` instead of the
   fabricated "cut off by the operator" mark. Test: `test_llm_hardening.py`.
2. FIXED — per-turn interrupt Event in `server/dispatch.py` (+ messaging host);
   a join-timeout zombie can't un-interrupt or swallow the new turn's flag.
   Tests: `test_server.py`, `test_messaging_host.py`.
3. FIXED — gateway `_dispatch_lock` narrowed to guard-record + audit; execute_code
   child RPCs no longer deadlock; delegate workers truly parallel.
   Test: `test_gateway_boundary.py`.
4. FIXED — durable proactive-speak destination (`remember_peer`) moves only
   AFTER the allow-list passes (host + standalone gateway + all adapters).
   Tests: `test_messaging.py`, `test_discord_slack.py`, `test_messaging_host.py`.
5. FIXED — `browser_console`/`browser_cdp` (eval + navigate-shaped CDP methods)
   screen through the same scheme+SSRF+secret guard as `browser_navigate`.
   Test: `test_browser_url_guard.py`.

### P2 — wrong behavior (all 13 fixed)
1. FIXED — `/model`//`provider`/reconfigure resync the live window via the ONE
   `agent.sync_context_window()` (max_tokens + trim buffer together; the zero
   trim-target wipe is impossible). Test: `test_provider_swap.py`.
2. FIXED — mutating commands (`/compact`, `/model <id>`, `/provider <label>`,
   `/reasoning <level>`) refuse while a turn is in flight and hold the stream
   slot while running (`commands.is_exclusive` + dispatch claim). Read-only
   forms stay concurrent; `interrupt` during a command honestly reports
   `interrupted:false`. Test: `test_server.py`.
3. FIXED — compaction's tail re-append persists as `kind='replay'`: `load()`
   restores from it, display/export skip it — no more duplicated history on
   every reopen/export. Tests: `test_compaction.py`, `test_transcript.py`.
4. FIXED — delegate fan-out rewritten: per-child timeout from each child's OWN
   start (daemon threads + semaphore), late results discarded, never-started
   tasks reported honestly, no executor shutdown wedge.
   Test: `test_execute_delegate.py`.
5. FIXED — `browser_vision` screenshots land under `workspace/screenshots/`
   (jail-writable) and the response advertises the workspace-RELATIVE path so
   MEDIA delivery works; agent resolves it for the vision follow-up.
   Test: `test_browser.py`.
6. FIXED — `search_files` surfaces "Search did not complete: <runner note>"
   when the RC sentinel is missing (timeout/jail refusal/runner error) instead
   of a clean false "0 matches"; incomplete probes aren't cached.
   Test: `test_search.py`.
7. FIXED — QQ send correlates OneBot action responses by `echo`; non-zero
   retcode → DeliveryDeferred; ack timeout → visible warning.
   Test: `test_messaging.py`.
8. FIXED — adapter crash flips the host status to per-platform `error` (no more
   stale "running"); weixin/telegram/slack one-shot startup checks moved into
   retry loops (auth-shaped failures stay fatal). Tests: `test_messaging_host.py`,
   `test_messaging.py`.
9. FIXED — Discord/Slack outbound: bounded retries honoring Retry-After on 429
   and backoff on 5xx/network; permanent errors defer immediately; a failed
   middle part stops the message with a "parts dropped" log, never a silent
   hole. Test: `test_discord_slack.py`.
10. FIXED — `gateway.stop` materializes `enabled:false` into every adapter
    block, so `session_messaging`'s recompute can't flip the kill-switch back.
    Test: `test_supervisor.py`.
11. FIXED — `set_autonomy(off)` treats an active messaging turn as a
    conversation (host `_turn_active` → supervisor guard); it still halts
    idle/self-work. Tests: `test_supervisor.py`, `test_messaging_host.py`.
12. FIXED — web: fresh attach syncs `lastSeq` and never replays (no duplicated
    turns on re-enter); `lastSeq` no longer persisted across visits.
    Tests: `apps/web/src/rpc.test.ts`.
13. FIXED — web: chara WS auto-reconnects forever with backoff; in-place rejoin
    resumes the stream, a declared gap or missing anchor triggers a clean
    re-attach epoch instead of a silent hole.
    Tests: `apps/web/src/hooks/chatSession.test.ts`.

### P3 — polish / defense-in-depth (all fixed)
- `core/llm.py` honest cut marks (see P1.1). `core/agent.py`
  `stream_react`/`stream_event` refresh `_last_turn_wall` — no spurious
  time-gap note after a react turn.
- `core/request_log.py` elides inline base64 image bytes (placeholder keeps the
  shape) and trims byte-aware (4 MiB cap, seek-based tail, no whole-file
  readlines). Test: `test_request_log.py`.
- `core/providers.py`: a failed OpenRouter catalogue fetch is never memoized —
  120 s cooldown then retry. Test: `test_max_output.py`.
- `core/transcript.py` `reset()` derives the new epoch inside the write
  connection — a transient read failure can no longer rewind the epoch.
  Test: `test_transcript.py`.
- `server/hub/cards.py` `card.save`/`card.delete` confine on the RESOLVED path.
  Test: `test_desktop_hub.py`.
- Network default drift fixed: a missing `network_access` key defaults ON in
  both `core/state.py` (backfilled via the migration path) and
  `session/isolation.py`. Tests: `test_state.py`, `test_pty.py`.
- `tools/mcp.py` `_save_media` sanitizes name/mime components and confines the
  resolved path under the media dir. Test: `test_mcp.py`.
- Web: stale `pendingSuper` after restore; force-stop orphan `finally`
  clobbering a newer turn; time separators/autoscroll on a mutated array;
  gateway clear-a-field not persisting; matte-install poll freeze; slack
  `allow_bot` wired; `split_text` counts UTF-16 units + avoids cutting inside
  code fences. Tests: `apps/web` vitest suite + `test_messaging.py`.

### Web UX residue — RESOLVED 2026-07-03
All five fixed (239 vitest green, build clean): superchat read-state now
fail-OPEN with retries (`lib/superchat.ts`); stop button visible while
streaming even with a draft (Composer); Market mobile CSS in `mobile.css`
(sheet-filling detail + sticky action bar); keyboard/AT basics on board/deck/
chat-panel rows + DeckModal dialog role/focus trap/Escape/focus restore;
chat backdrop/sprite prefs got their UI (聊天背景/立绘 controls in the chat
settings pane, per-chara localStorage, live-reactive).

## 2026-07-03 second sweep — RESOLVED (fresh five-track audit + UI review)

A second full audit (core/protocol, server/session, tools/visuals/content, web,
prompt/harness) plus a Playwright visual review of every view × theme × viewport,
run AFTER v0.1.11. All findings fixed, adversarially re-verified, tests green.
Highlights (full detail in git history):

- **[HIGH] `ContextBuffer.token_count()` raced `trim()`** — the token memo was
  written from the transport thread (snapshot polling) while the worker thread
  iterated it → `RuntimeError: dictionary changed size`. A per-instance lock now
  guards every memo touch. Test: `test_context.py` (threaded stress).
- **[HIGH] the keyring (`desktop.json`, every provider secret) was written
  non-atomically with racy read-modify-writes** — a mid-write crash could
  truncate all keys; concurrent RPCs lost updates. Now atomic (temp+replace,
  0600) + one RLock around every RMW incl. the legacy-key migration path.
  Known residue: a TUI-process writer can still last-write-wins a hub write
  (cross-process; needs flock; pre-existing, LOW).
- **[HIGH, prompt] `execute_code` advertised the shelved `web_search`/
  `web_extract`** in its model-facing schema — scripts dead-ended on the RPC
  allowlist. Shelved from `SANDBOX_ALLOWED_TOOLS`; the schema no longer teaches
  them. Also dropped the `hermes_tools.py` staging twin (de-brand hygiene).
- **[MEDIUM] `rejoin` dropped frames pushed during the replay window** —
  handle_rejoin now loops until caught up before the driver joins live.
- **[MEDIUM] bwrap ignored `terminal(workdir=…)`** — the jail argv hardcoded
  `--chdir <workspace>`; the validated workdir is now threaded through (shell +
  PTY). macOS/Landlock/admin were already correct.
- **[MEDIUM, prompt] `terminal` schema claimed over-limit timeouts are
  "rejected" (they're clamped)** — wording fixed; **the volatile tail re-shipped
  two static sentences every turn** (workspace/works/assets prose already in the
  cached prefix) — reduced to the dynamic facts only; the art note's duplicate
  "assets read-only" clause trimmed.
- **[MEDIUM, web] `ChatSession`'s immediate stream callbacks lacked the `dead`
  guard** — a disposed session's late frames could write into the successor's
  model; **pre-attach sends now queue** instead of surfacing a raw "not
  connected"; chat settings sticky toggles reconcile on snapshot/name change;
  Gateways empty state centered with a single CTA.
- **[LOW]** `sync_context_window` now trims immediately (idle/react/event paths
  could ship over-window after a narrow swap); request log never self-empties on
  one oversized record; `_await_supervisor` cancels the coroutine on timeout;
  gateway's MCP-denied audit write joined `_dispatch_lock`; search REFUSES
  (tool error) instead of silently degrading to unjailed py-fallback when the
  jail is refused; worldinfo lowercases the scan text once per scan.
- **UI visual review**: dark/light × desktop/mobile × all six views verified
  clean (the "white board in dark mode" turned out to be a headless-Chromium
  stale-paint artifact — documented in AGENT-FIELD-NOTES §1 scars, not an app
  bug).

## 2026-07-03 third sweep — RESOLVED (front/CLI/install layer + docs + interactive UI)

The last unaudited layer (front/ CLI, updater, install.sh, deploy, CI) plus a
bilingual README accuracy pass and an interactive Playwright walk. All findings
fixed, adversarially re-verified (the review's own 5 counter-findings fixed too):

- **[HIGH, supply chain] install.sh's SHA256SUMS verification was dead code on
  the default public path**, and `lunamoth update` installed the wheel with no
  checksum at all. Both now download → verify sha256 → install from the
  verified local file; mismatch refuses loudly; missing manifest / missing
  sha-tool get an honest NOTE. One wall-clock budget spans download+install so
  the webui's RPC ceiling holds.
- **[MEDIUM] wizard's first-run character menu was EMPTY on the wheel channel**
  (scanned repo-root cards/ instead of `content_dir("cards")`).
- **[MEDIUM] daemon pid hygiene**: stale `daemon.pid` across reboots could make
  `start-all` skip a chara or `stop` killpg an unrelated reused pid. Liveness
  now includes an identity check with a per-session `--session` argv marker
  (sibling charas distinguished; markerless pre-upgrade daemons still pass);
  starts claim the pid file O_EXCL before spawning; `stop` no longer deletes an
  in-flight start claim.
- **READMEs (EN+zh, lockstep)**: five-platform messaging table (WeChatPadPro
  row dropped, Discord/Slack added), real first-run flow, aspiration replaces
  the removed "wishes", the Market + card import documented as shipped, image
  gen / visuals pipeline / personal website / backdrop-sprite prefs added.
- **Interactive UI walk** (create flow, dirty guard, generate-failure path,
  chat settings): all correct — one fix, the first-run welcome now closes on
  Escape like every other layer.

## 2026-07-03 fourth sweep — RESOLVED (final wrap: shell/content/zh-UI/leftovers)

The wrap-up round covered the last never-audited surfaces. Electron shell
security posture verified sound (contextIsolation/sandbox/no-nodeIntegration,
off-origin navigation blocked, single-instance, clean quit semantics; the one
finding — the startup handshake token could surface in error dialogs — is now
scrubbed from the log ring). All 8 bundled cards mechanically validated (ST
shape, assets, tags, hooks; exactly one "default"); the six website-centric
cards now declare `extensions.lunamoth.website: "on"` so their core premise
works out of the box. zh-UI visual pass: all views × both widths, zero
horizontal overflow, natural copy — clean. Landlock LOW ergonomics closed
(/proc-unavailable note + PTY network-notice parity; a note-clobbering `=` vs
`+=` bug found and fixed in the wiring). Part 4 retest items closed where code
proves them (send_file→MEDIA UX, interrupt-feel mechanics); Part 1's stale
delegate wording refreshed.

### Remaining OPEN residue (small, non-blocking)
- Keyring cross-process last-write-wins (TUI `save_global_key` vs hub RPCs on
  `desktop.json`) — needs an flock to fully close; atomicity already prevents
  torn files. LOW.
- Latent (inherent to the join-timeout takeover model): a superseded zombie
  past the 10 s join can still overlap a freshly-claimed slot for its last
  in-flight tool; its interrupt flag is set so the window is minimal.


## Landlock ergonomics (LOW — clarity/ergonomics, NOT jail escape)
From the 2026-06-17 security review (generate_image is now non-blocking — runs in a
background thread, so a flapping endpoint can't freeze a turn). Remaining LOW:
- Network gating under Landlock needs ABI v4 (kernel 6.7); until then `/net off` is
  fs-only under that tier (surfaced honestly: the per-run terminal note, the
  once-per-process operator log warning, and the PTY open banner).
- closed 2026-07-03: the bare-EACCES mystery — every Landlock-tier terminal run now
  carries a one-line `[lunamoth: …]` note that `/proc` is unavailable by policy
  (deliberate: `/proc/<pid>/environ` leaks the supervisor token), so `ps`/`top`/
  interpreter failures read as jail policy, not mystery (`build_jail_command`;
  browser jail excluded — it re-adds /proc).
- closed 2026-07-03: the operator PTY now shows the same Landlock caveats (network
  not gated ABI v1 + /proc unavailable) as a banner at shell open
  (`isolation.interactive_shell_notice` → supervisor `_handle_pty`), parity with
  `runner.run_terminal`'s "fail visibly" note.

## R5-followup (LOW) — card-view art editing + richer world/expressions
R5 shipped the multi-page card view (display + 设定/世界 editing). DONE (2026-06-22 visuals
pass): per-asset generate + save for 立绘/主视觉/头像/表情/背景 — the 视觉 tab now generates
all five kinds (async), `card.asset_save` + the new `card.stickers_save`, so 表情 is a
generatable + saveable set. NAMED stickers effectively shipped since (filename slugs via
`stickers_save(names=…)` + `card.sticker_rename`/`sticker_reslice` — the `[{label,file}]`
data model was not adopted and isn't needed). Also since: 参考图 reference images persist
in the asset library (a82ad67). STILL DEFERRED: the per-entry world editor for EDITABLE
cards (read-only cards already show per-entry cards; editable still uses the text editor).

## R6 (P3) — Blank card → auto-generate a visual set via the image key (opt-in)
Largely BUILT (2026-06-22): the 视觉 tab generates the full set — keyvisual ANCHOR first,
then avatar/sprite/stickers/background referencing it for identity-lock, async polling +
一键生成全部 (anchor-first). What REMAINS is the "blank card → auto-fill at CREATION"
entry point (a one-click "give this new card a face" from the create/wake flow) + the
opt-in/cost UX; the generation machinery (`visuals/pipeline.py` + the brief + anchor
reuse) is done.

---

# Part 4 — 2026-06-17 test-feedback triage (open items only)

Owner tested the 2026-06-16 build. Open items from that triage (the small settled
fixes are on main and not retained here):

## Live verification (visuals + messaging) — needs real credentials/a host
The visuals pipeline + global keys + matte shipped but were never exercised
end-to-end: needs a real image key (full card visual-set generation — now incl. the
keyvisual anchor → identity-locked avatar/sprite/stickers/background, async job polling,
and the white-bg sticker slice) and a downloaded matte model (the cutout path; without it
the keyless `cut_white_bg` fallback runs). Same shape as the WeChat/QQ messaging
live-test (roadmap C.1). Budget one verification round with real keys.

## (2)(5)(6)(7) — deferred to the UI/feel refactor
- **(2) send_file UX**: closed 2026-07-03 — the standalone tool is gone; the MEDIA:
  convention renders inline image / download in the SPA (`lib/media.ts`
  `splitOutbound` → `StreamItems.tsx`), and restored history replays through the
  SAME streamModel restore path, so the cards re-render on reopen.
- **(5) interrupt / insert-message feel**: closed 2026-07-03 — the interrupt paths
  were rebuilt in the July sweeps: per-turn interrupt Event server-side (a zombie
  can't un-interrupt; honest `interrupted:false`), double-stop force-reset
  (`useCharaStream.forceStop`), persisted send-queue + stream ordering
  (`hooks/chatSession.ts`). The subjective "feel" was not re-tested live here; if
  the owner still dislikes it, reopen as a NARROWED symptom.
- **(6) tool-call compression**: fold consecutive tool calls with no assistant text
  into one group + one reasoning. NOTE: the React `streamModel.ts` already folds
  tool-groups — owner tested the OLD client; RE-TEST on the new SPA before doing work.
- **(7) silent after tool calls**: chara sometimes ends a turn without speaking.
  (Curriculum / prompt steering toward a closing `speak`.)

## Retired-clarify follow-up (from item 9)
The clarify TOOL is gone, but its generic interactive-question plumbing remains
dormant (codec `clarify_ask`/`clarify_reply`, dispatch round-trip, terminal stdin
hook, ~7 React files; mirrors `permission_hook`). Fully excising it is a
protocol-first change (constitution codec + React client) that wants owner sign-off.

## Frontend refactor pass (2026-06-17) — deferred items

DEFERRED (considered, judged net-negative TODAY — revisit with test scaffolding):
- **useCharaStream controller extraction** — split the 100-line connect/attach
  effect (client construct + 9 callback wirings + async attach + timers) into a
  controller the hook thinly wraps. Pure readability in the MOST delicate code
  (streaming lifecycle), and the web side has near-zero coverage of this hook — a
  rewrite needs `app.run_test()`-style pilot coverage FIRST. Also fixes the
  timers-created-inside-the-async-IIFE cleanup race the audit flagged.
- **CardContentForm spine** — one field-spec driving CardEditor / WakeSheet /
  CreateFlow. Declined: `CardBlock` is ALREADY the shared row primitive, and the
  three flows differ enough that one spec-driven form needs per-flow flags =
  config-explosion. The error-prone part (serialization) is already unified.

Other audit findings still open (lower value): useAsync/useBusySet hooks to fold
the repeated alive-flag load + Set busy-tracker (~10 files); per-pane file split of
ChatPanel's 6 bundled panes; React.memo on the markdown items (measure first).

## UX/design pass (2026-06-17, second wave) — token pass long tail

STILL OPEN — the token pass LONG TAIL (do per-surface with screenshot checks, NOT
blind-bulk — the rendered look is good and must be preserved):
- collapse the 26 font-sizes onto a ~7-step --fs-* scale (six are .5px); 8 weights → 4.
- migrate the 20 hardcoded radii onto --r-sm/--radius/--r-lg (esp. the 5 "card" radii
  13/14/15 → --r-lg).
- one `.selectable` base for the near-identical tile pickers (iso-seg / provider /
  gw-plats) — the wake sheet stacks them slightly off.
- give .btn + the field base a shared --control-h so rows stop wobbling.
- replace the ~24 inline style={{marginTop:N}} literals with rhythm utilities.

---

# Appendix A — client + deploy architecture reference

The full build plan (CLIENT-AND-DEPLOY-PLAN) shipped 2026-06-16 and was deleted;
the operational runbook proper lives in **README (EN/zh)** + **`deploy/`**
(Dockerfile, compose.yml, entrypoint.sh) + `install.sh`. The durable
architecture/rationale worth keeping (change only with owner sign-off):

- **Stack**: React 19 + Vite + TypeScript SPA; **Context + hooks**, no Redux/MobX;
  **hash routing** (so the supervisor's static handler needs no SPA-fallback list,
  and `file://` would work). Source at repo-root `apps/web/`.
- **Distribution = a wheel that bundles the built frontend** (hermes model). `apps/web`
  builds to `src/lunamoth/front/webui/` (GITIGNORED, not committed); setuptools
  package-data (`lunamoth=["front/webui/**/*"]`) packs it into the wheel at CI
  packaging time → users `uv tool install` and get the prebuilt UI, no node at install.
  `vite.config.ts`: `base:'./'`, `outDir:'../../src/lunamoth/front/webui'`, `emptyOutDir`.
- **Electron stays a thin local shell**, unchanged, always pointing at the local
  supervisor's HTTP URL. Remote = browser, never Electron.
- **Remote, two ways**: (a) SSH tunnel to the loopback-bound supervisor (`lunamoth
  connect ssh://user@host` opens `ssh -L` after reading the remote daemon token/ports);
  (b) supervisor bound to a real host behind a TLS reverse proxy (Caddy / cloudflared).
- **Auth**: ONE `lm_auth` SameSite cookie minted by a `?token=` handshake gates GET /
  `/asset` / `/rpc` / `/upload` / WS uniformly (401); Origin/Host allowlist; optional
  PBKDF2 password login layered on top; "no server token ⇒ open (dev/loopback)".
  Lives in `supervisor/` + `netsec.py`.
- **One-click deploy = Docker**: `python:3.12-slim` + `pip install` the release wheel
  (carries `webui/`, so no node); `compose.yml` with `restart: always`,
  `no-new-privileges`, a `~/.lunamoth` volume for sessions/cards/config.
- **Two known non-ideal-but-shipped choices** (future upgrade path): the supervisor
  runs TWO server stacks on TWO ports (stdlib http.server + websockets, WS = http+1
  for non-loopback) — single-port ASGI (Starlette/uvicorn) is the eventual cleanup.
