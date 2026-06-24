# LunaMoth — open work

This is the ONE doc under `docs/` (owner rule, re-affirmed 2026-06-17: everything
condenses here; settled plans/specs/build-logs get deleted once their conclusions
live in `CLAUDE.md`, the code, and git history). What's kept is only what's still
*open* or worth remembering:

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
(read by both `agent` and `settings.load` — no more duplicated `abs(x-600)` literal).

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
- **Card / pack marketplace** — one-click package (card + embedded world book) +
  a shareable index. (Same as the roadmap's card-market item.)
- **Multi-chara visiting** — charas on one machine visiting each other; the
  `say|muse` protocol already supports multiple audiences. Far-future.
- **Voice (STT/TTS)** — hermes has the full chain to port; a big "aliveness"
  boost, but only after the core stabilizes.
- **Multimodal output** — image UNDERSTANDING (read_file → vision route) shipped;
  richer multimodal output is still deferred (skeleton left in place).
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
  - **Bug to fix regardless** (independent of the DMG work): `main.cjs`
    `installedLauncher()` looks for `~/.lunamoth/bin/lunamoth`, but `install.sh`
    puts the shim at `~/.local/bin/lunamoth`. Fix the path (check
    `~/.local/bin/lunamoth` too).
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

## Landlock ergonomics (LOW — clarity/ergonomics, NOT jail escape)
From the 2026-06-17 security review (generate_image is now non-blocking — runs in a
background thread, so a flapping endpoint can't freeze a turn). Remaining LOW:
- The Landlock tier grants no `/proc` (deliberate — `/proc/1/environ` leaks the
  supervisor token), so `/proc`-dependent tools (`ps`, some interpreters) fail with a
  bare EACCES under Docker. Consider a clearer message, or a procfs-hidepid mount if
  ever feasible.
- `interactive_shell_argv`'s Landlock fallback doesn't surface "network not gated
  (ABI v1)" to the operator PTY the way `runner.run_terminal` does — add the one-line
  notice for parity with the "fail visibly" contract.
- Network gating under Landlock needs ABI v4 (kernel 6.7); until then `/net off` is
  fs-only under that tier.

## R5-followup (LOW) — card-view art editing + richer world/expressions
R5 shipped the multi-page card view (display + 设定/世界 editing). DONE (2026-06-22 visuals
pass): per-asset generate + save for 立绘/主视觉/头像/表情/背景 — the 视觉 tab now generates
all five kinds (async), `card.asset_save` + the new `card.stickers_save`, so 表情 is a
generatable + saveable set. STILL DEFERRED: the labeled-expression data model
(`assets.stickers` → `[{label,file}]`, back-compat with the current bare-string list) so
表情 becomes a NAMED set; and the per-entry world editor for EDITABLE cards (read-only
cards already show per-entry cards; editable still uses the text editor).

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
- **(2) send_file UX**: file cards don't re-render on chat reopen; unclear where a
  download lands; html should open in the browser; other files should open in the
  sandbox Finder. (React file-card render + open-with.)
- **(5) interrupt / insert-message feel**: jank when interrupting or injecting a
  message mid-stream. (Streaming preemption semantics.)
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
