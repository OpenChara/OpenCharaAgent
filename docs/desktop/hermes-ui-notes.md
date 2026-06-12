# Hermes Desktop UI study — 28 screenshots, 2026-06-12

Source: `hermes ui/` screenshots of the official NousResearch Hermes Desktop app
(v0.16.0, macOS, light mode, "Nous" theme). Captured in time order: installer →
first run → main app tour → onboarding → chat → settings deep-dive.

## Screen-by-screen

### 16.08.52 — Installer welcome
Full-window splash. Huge blue display-serif wordmark "HERMES AGENT" centered,
one gray subtitle line ("The agent that grows with you. We'll set things up in
the background — takes a few minutes."), single primary blue pill button
"Install Hermes →". Background is a very light blue-gray. Zero chrome besides
macOS traffic lights. The wordmark IS the empty state.

### 16.08.57 — Install progress (collapsed)
Sticky top bar: spinner + current step name left, "0 of 11 steps" right, thin
blue progress bar underneath. Body = vertical checklist of all 11 steps
(Download Hermes Agent, Create Python venv, Install deps, Install browser-tool
deps, Install hermes command, Prepare config and skills, Configure API keys and
settings, Configure gateway service, Build desktop app, Finish install).
Current step: white card + spinner; future steps: dim gray text + gray dot.
Footer: "Show details >" left, Cancel button right. The whole plan is visible
up front — no mystery spinner.

### 16.09.20 — Install progress (details expanded)
Same checklist on the left, right pane "Live output" with line counter
("14 lines") streaming the raw terminal log (uv install, Python detection,
ASCII banner). Progressive disclosure: friendly checklist by default, real
console one click away. "Hide details" toggles back.

### 16.38.56 — Install complete
"HERMES IS READY" in the same blue display serif. Subtitle teaches CLI parity:
"You can launch from here, or any time from your terminal with
`hermes desktop`" — the command rendered as an inline code chip. One primary
button "🚀 Launch Hermes".

### 16.40.20 — Main app, fresh empty state
Three persistent regions: (1) left sidebar ~210px, light gray, top nav =
New session / Skills & Tools / Messaging / Artifacts (icon + label rows);
(2) main canvas: centered wordmark "HERMES AGENT" + two-line capability
tagline ("Ask a question, paste an error, or point me at a repo…") as the
empty-chat state; (3) bottom composer: rounded full-width-ish input
"Give Hermes a task" with `+` attach at left, mic + circular send at right.
Persistent bottom STATUS BAR: left = Gateway (orange dot, "needs setup"),
Agents, Cron; right = model picker ("Opus 4.6 · Med"), context/version chip
("v0.16.0 (+3) 8b2a3c9" in blue). Top-right icon cluster: sound, keyboard
shortcuts, settings gear, right-panel toggle.

### 16.40.26 — Update modal
Centered dialog over dimmed app. Logo mark, "New update available", subtitle,
then changelog inside a tinted card grouped by WHAT'S NEW / FIXED / IMPROVED
(small-caps section labels) with PR numbers. Full-width blue "Update now",
plain-text "Maybe later" beneath. Compact, scannable, two-action.

### 16.40.35 — Composer attach menu
`+` opens a small popover anchored to the composer: section label "ATTACH",
rows Files… / Folder… / Images… / Paste image / URL… / Prompt snippets…, each
with an icon. Footer hint: "Tip: type @ to reference files inline." Menu
doubles as keyboard-affordance education.

### 16.41.00 — Skills & Tools page
Tabs: Skills | Toolsets. Below: category filter chips with live counts
(All 71 · Apple 6 · Autonomous-AI-Agents 5 · Creative 10 · Data-Science 7 ·
Email 5 · General 6 · Github 3 · Media 6 · Mlops 7 · Note-Taking 7 ·
Productivity · Research · Smart-Home · Social-Media · Software-Development).
Search field top-right ("Search skills…"). Body = flat list grouped by
SMALL-CAPS category headers; each row = bold skill name + one-line gray
description + a blue iOS-style toggle on the right (e.g. apple-notes "Manage
Apple Notes via memo CLI", claude-code "Delegate coding to Claude Code CLI
(features, PRs)", macos-computer-use with a 2-line description). Enable =
one toggle, no modal.

### 16.41.06 — Messaging (connectors) page — the gateway UX
Master-detail inside the main area. Inner-left list (~150px): every connector
with its real brand icon + name + tiny status dot: Telegram, Discord, Slack,
Mattermost, Matrix, WhatsApp, Signal, BlueBubbles (iMessage), Home Assistant,
Email, SMS (Twilio), DingTalk, Feishu/Lark, WeCom (group bot), WeCom (app),
WeChat (Official Account), QQ Bot. Search at top ("Search messaging…").
Right detail pane (Telegram): logo + name + one-line value prop ("Run Hermes
from Telegram DMs, groups, and topics"), then THREE STATUS CHIPS:
"Disabled" · "Needs setup" · "Messaging gateway stopped". Sections:
GET YOUR CREDENTIALS (plain-language steps: talk to @BotFather, /newbot,
grab numeric ID from @userinfobot) + "Open setup guide ↗"; REQUIRED (Bot
token, field placeholder "Paste Telegram bot token", external-link icon);
RECOMMENDED (Allowed Telegram user IDs, with the why: "Without this, anyone
can DM your bot"); ADVANCED (1) collapsed disclosure. Bottom bar: enable
toggle bottom-left, blue "Save changes" bottom-right.

### 16.41.11 — Artifacts page (empty)
Filter tabs All (0) / Images (0) / Files (0) / Links (0) with counts. Centered
empty state: bold "No artifacts found" + one explanatory line ("Generated
images and file outputs will appear here as sessions produce them") — empty
states always say what WILL fill them. Sidebar footer shows home + "+"
(workspace tabs) and overflow "…". Status bar: Gateway needs setup (orange),
Agents, Cron / "No model ∨" / v0.16.0 (+3) 8b2a3c9.

### 16.41.19 — Model picker popover (from status bar)
Clicking the model chip in the status bar opens a TWO-PANEL popover:
left panel "OPTIONS" (Thinking toggle on, Fast toggle off) + "EFFORT" radio
list (Minimal / Low / Medium ✓ / High / Max); right panel = searchable model
list ("Search models") grouped by SMALL-CAPS provider headers — ANTHROPIC:
Fable 5, Opus 4.8, Opus 4.7, Opus 4.6 (highlighted), Sonnet 4.6, dated snapshots
(Opus 4.5 20251101, Sonnet 4.5 20250929…), then GITHUB COPILOT group. Footer
link "Edit Models…" escapes to full settings. Model + reasoning effort are a
single surface, reachable without leaving chat.

### 16.41.44 — Insights overlay: Usage
Settings-style overlay (large modal covering the window, X top-right) with its
own left nav: Sessions / System / Usage. Usage page: title + subtitle, time
range segmented control (7d / 30d / 90d) top-right; FOUR STAT COLUMNS in
small-caps (SESSIONS, API CALLS, TOKENS IN/OUT, EST. COST $0.00); DAILY TOKENS
bar chart with input/output legend; below, two columns TOP MODELS / TOP SKILLS.
Cost is a first-class, always-computed number.

### 16.42.00 — Provider onboarding (first run, collapsed)
Centered card on a blank window: "Let's get you setup with Hermes Agent" +
"Connect a model provider to start chatting. Most options take one click."
One highlighted row: Nous Portal with a blue "RECOMMENDED" letterspaced badge
and the pitch "One subscription, 300+ frontier models — the recommended way to
run Hermes", chevron right. "Other providers ∨" expander. Footer escape
hatches: "I'll choose a provider later" (left) / "I have an API key" (right).

### 16.42.08 — Provider onboarding (expanded)
Provider rows each state their AUTH MECHANIC in one gray line: OpenAI OAuth
(ChatGPT) "Opens a verification page in your browser — Hermes connects
automatically"; MiniMax same; Qwen Code "Sign in once in your terminal, then
come back to chat" (terminal icon at right); xAI Grok; Anthropic API Key
"Opens your browser to sign in, then continues here"; Anthropic OAuth row
carries a warning in its title ("Required Extra Usage Credits to Use
Subscription") and a "✓ Connected" pill. "Collapse ∧" link. Already-connected
state is shown inline in the same list.

### 16.43.10 — Provider connected confirmation
Deliberate style break: monospace, terminal-flavored, centered.
"OPENROUTER CONNECTED" (blue, letterspaced) / small-caps "DEFAULT MODEL" /
`anthropic/claude-fable-5` / price line "$10.00 in / $50.00 out per Mtok" /
"Change" text link / bracketed "[ BEGIN ]" button. Surfaces the default model
AND its price before the first message.

### 16.43.30 — Chat error + hot-swap model picker
Sidebar now shows: Search sessions…, PINNED section (with inline hint
"Shift-click a chat to pin"), SESSIONS list (current "测试" highlighted).
User message rendered as a full-width bordered box (not a bubble). Agent
failure = honest red error text with remediation ("Run 'hermes model' to
choose a provider and model, or set an API key (OPENROUTER_API_KEY,
OPENAI_API_KEY, etc.) in ~/.hermes/.env") — no silent fallback. Model picker
open over the status bar, search "dee" filtering to OPENROUTER group: Fable 5
Med ✓, Deepseek V4 Pro, Deepseek V4 Flash; options/effort panel beside it.
Status bar now "Gateway ready" + "Session 1:37" timer.

### 16.44.22 — Working chat + right workspace panel
Chat column (~700px, centered, text left-aligned): user messages in outlined
boxes; collapsed "Thinking" labels between steps; tool calls as one-line chips
with icon + verb + duration ("✎ Edited file 147ms ∨", "▤ Read file 88ms");
assistant prose with emoji/✅; a labeled code block ("Code") showing file
content; file path rendered as a copyable mono chip. RIGHT PANEL = workspace
file browser, header "/Users/jyxc-dz-0101366 — click to change folder",
directory tree list. Status bar right side now dense: "16.6k/1.0M ▓ 2%"
context meter, "Session 0:32", "Deepseek V4 Flash - Med", version.

### 16.44.33 — Artifacts page (populated)
Table: TITLE/NAME (icon + filename) · LOCATION (monospace absolute path) ·
SESSION (session title + timestamp, links back to the chat). Pagination
"1-1 of 1 items", search top-right. Artifacts are session-traceable.

### 16.45.08 — Tool calls expanded
"Edited file 147ms" expanded inline into a real DIFF VIEW (red/green lines,
-1.0/+1.0 hunk markers, mono font, dark-on-light tinted card). "Read file
80ms" expands to numbered file contents. "Thinking" sections show a one-line
gray summary when collapsed ("The file was created and read back successfully.
Let me confirm everything to the user."). Composer stays pinned bottom with
circular send.

### 16.45.42 — Settings → Model
Full settings is a large overlay with persistent left nav in two groups:
[Model, Chat, Appearance, Workspace, Safety, Memory & Context, Voice,
Advanced] then [Providers, Gateway, Tools & Keys, MCP, Archived Chats] then
About. Bottom-left: import / export / reset icons. Model page header copy:
"Applies to new sessions. Use the model picker in the composer to hot-swap the
active chat." — explicitly distinguishes default vs live model. Provider
dropdown (OpenRouter) + model dropdown + blue Apply. Then "Auxiliary models"
(with "Reset all to main"): per-task model overrides, each row = task name +
purpose chip + current value "auto · use main model" + "Set to main" /
"Change": Vision (image analysis), Web extract (page summarization),
Compression (context compaction), Skills hub (skill search), Approval (smart
auto-approve), MCP (MCP tool routing), Title gen (session titles).

### 16.45.48 — Settings → Model (scrolled)
More aux models incl. Curator (skill-usage review). Then: Context Window
numeric field — "Leave at 0 to use the selected model's detected context
window" (auto-detect with explicit override). Fallback Models — comma-
separated backup provider/model entries "to try if the default model fails".

### 16.46.13 — Settings → Appearance (top)
Copy: "These are desktop-only display preferences. Mode controls brightness;
theme controls the accent palette and chat surface styling." Language dropdown
with search: English (EN), 简体中文 (ZH), 繁體中文 (ZH-HANT), 日本語 (JA).
Color Mode (fixed or follow system). Theme = grid of preview CARDS, each a
mini chat mockup + name + one-line vibe: Nous "Glass neutrals with Nous blue
accents" (✓ selected), Midnight "Deep blue-violet with cool accents", Ember
"Warm crimson and bronze — forge vibes", Mono "Clean grayscale — minimal and
focused".

### 16.46.22 — Settings → Appearance (scrolled)
Remaining themes: Cyberpunk "Neon green on black — matrix terminal", Slate
"Cool slate blue — focused developer theme". Note "The selected mode is
applied on top" (mode × theme are orthogonal). Below: `publisher.extension`
input + "Install" — themes are installable extensions. Then "Tool Call
Display" segmented control: Product | Technical — "Product hides raw tool
payloads; Technical shows full input/output." Audience switch as one toggle.

### 16.46.37 — Settings → Safety
Flat labeled rows, mixed control types: Approval Mode (dropdown: Manual),
Approval Timeout (60), Confirm MCP Reloads (toggle), Command Allowlist
(comma-separated), Redact Secrets (on, "Hide detected secrets from
model-visible content when possible"), Allow Private URLs (off), Browser
Private URLs (off), Local Browser For Private URLs (on), File Checkpoints
("Create rollback snapshots before file edits", off). Every row: bold label +
one-line gray rationale + control on the right.

### 16.46.43 — Settings → Memory & Context
Persistent Memory (toggle), User Profile (toggle, "Maintain a compact profile
of user preferences"), Memory Budget (2200), Profile Budget (1375) — budgets
in characters/tokens are user-visible numbers. Memory Provider (plugin
dropdown, "(none)"), Context Engine (dropdown: Compressor — "Strategy for
managing long conversations near the context limit"), Auto-Compression
(toggle, "Summarize older context when conversations get large"), Compression
Threshold (0.5), Compression Target (0.2), Protected Recent Messages (20).
The compaction machine is fully inspectable and tunable.

### 16.47.00 — Settings → Gateway
"Gateway Connection" intro: desktop starts its own LOCAL gateway by default;
use a remote one to control an already-running backend elsewhere. Two
radio CARDS side by side: "Local gateway — Start a private Hermes backend on
localhost. This is the default and works offline." (✓) vs "Remote gateway —
Connect this desktop shell to a remote Hermes backend. Hosted gateways use
OAuth or a username and password; self-hosted ones may use a session token."
Remote URL field (placeholder https://gateway.example; "Path prefixes are
supported, for example /hermes"). Actions: "Test remote", "Save for next
restart", blue "Save and reconnect". Diagnostics row: "Reveal desktop.log in
your file manager — useful when the gateway fails to start" + "Open logs".

### 16.47.25 — Chat + Preview panel (HTML artifact)
Right panel switched to "Preview": renders the generated page.html live
(styled heading, chips), with a "Preview Console" strip beneath (log lines)
and actions "Send to chat" / "Copy" / "Clear". Chat column shows a markdown
results table (file/format/size/status with ✅ chips). The right panel is a
multi-mode surface: file browser OR live preview.

### 16.47.51 — Chat, terminal tool calls
Sequence of collapsed "Run" chips, each showing the actual command in mono
("Run · mv /Users/... && mkdir ...", "Run · chmod +x ...", "Run · python3 ...",
"Run · ls -la ...") + per-call duration (ms/s) at the right. Thinking labels
between batches. Final assistant message = headline ("✅ 多种文件格式测试 —
全部成功") + markdown table + closing question. A long agentic run reads as a
clean ledger of verbs, not a wall of JSON.

## Patterns

**Frame & layout**
- Fixed three-layer frame: left sidebar (~210px) · main canvas · persistent
  bottom STATUS BAR (~24px). Optional right panel (~300–320px) toggled from
  the top-right icon cluster, multi-mode (file browser / preview).
- Chat content lives in a centered column ~680–760px max-width; everything
  inside it is left-aligned (including user messages — boxes, not right-side
  bubbles). Full window width is never used for prose; side whitespace is
  deliberate, and the right panel eats it when there's something to show.
- Empty screens are never blank: the wordmark + capability tagline IS the
  empty chat; list pages get "No X found" + one line explaining what will
  populate them. Big display-serif wordmark used ONLY on splash/empty states —
  brand as furniture, not chrome.
- Density: one-line rows everywhere (name bold 13px-ish, description gray
  12px, control right-aligned); SMALL-CAPS letterspaced gray section headers
  (APPLE, REQUIRED, OPTIONS, DAILY TOKENS) as the universal grouping device;
  generous row height (~44px) but zero decorative padding blocks.
- Light theme: near-white blue-gray canvas, white cards, 1px hairline borders,
  single saturated blue accent (buttons, toggles, links, selected states).
  Status colors: orange = needs setup, green = ready, red = error text. Mono
  font reserved for: paths, commands, model ids, code, the "connected"
  ceremony screen.

**Navigation model**
- Only FOUR top-level destinations in the sidebar (New session, Skills &
  Tools, Messaging, Artifacts) + sessions list below (Search / PINNED /
  SESSIONS). Everything else is an overlay: Settings = large modal with its
  own two-group left nav (8 behavior sections + 5 infrastructure sections +
  About); Insights/Usage = similar overlay (Sessions/System/Usage). Deep pages
  use in-page master-detail (Messaging) or tabs+chips (Skills, Artifacts).
- The status bar is the system tray: gateway state, agents, cron on the left;
  context meter, session timer, model+effort chip, version+commit on the
  right. Chips are buttons (model chip opens the picker in place).

**Gateway / connectors**
- One "Messaging" page, master-detail. ~17 connectors listed with real brand
  icons and per-item status dots. Detail pane formula: identity header →
  status chips row (enabled? configured? gateway running? — three independent facts) →
  GET YOUR CREDENTIALS walkthrough + external setup guide → REQUIRED fields →
  RECOMMENDED fields (with the security "why") → ADVANCED (n) collapsed →
  enable toggle + Save changes pinned at bottom.
- Local-vs-remote gateway is a separate settings page with two radio cards,
  explicit Test / Save-for-restart / Save-and-reconnect actions, and a
  one-click "Open logs" diagnostics escape.

**Models & providers**
- Three distinct surfaces, clearly scoped: (1) onboarding cards — one
  RECOMMENDED path + expandable list where every provider states its auth
  mechanic in one line, with Connected pills inline, plus escape hatches
  ("later" / "I have an API key"); (2) the composer/status-bar POPOVER for
  hot-swapping the active chat: search, provider-grouped list, Thinking/Fast
  toggles + 5-level Effort, "Edit Models…" link; (3) Settings → Model for the
  default: provider+model dropdowns, AUXILIARY per-task model overrides
  (vision/compression/title-gen/approval/MCP-routing… each "auto · use main
  model" with Change), context-window auto-detect-with-override, fallback
  model list. Current model + effort always visible in the status bar; price
  per Mtok shown at connect time; est. cost in Usage.
- Errors name the fix: missing provider → red text citing the exact env vars
  and the command to run.

**Settings philosophy**
- Quick/live things = status bar chips and popovers (model, effort, thinking).
- Configuration = the settings overlay, organized behavior-first
  (Model/Chat/Appearance/Workspace/Safety/Memory/Voice/Advanced) then
  infrastructure (Providers/Gateway/Tools & Keys/MCP/Archived Chats).
- Every setting row: bold label + one-line plain-language rationale + control.
  Numbers are exposed, not hidden (budgets, thresholds, protected messages).
- Tool Call Display "Product | Technical" = a single audience switch instead
  of per-feature verbosity flags. Themes are cards with live mini-previews and
  one-line vibes, and are installable by extension id.

**Chat surface**
- User turn: bordered box. Assistant turn: bare prose, no bubble.
- Tool call: one-line chip — icon + verb ("Edited file", "Read file",
  "Run · <command>") + duration — expandable to diff / file body / output.
- Thinking: collapsed label with a one-line gray summary; repeats between
  tool batches so the agent's cadence is visible.
- Composer: pinned bottom, "+ Give Hermes a task" / "Keep it going" /
  "Adjust or continue" placeholders that change with session state; attach
  popover (+ files/folder/images/URL/snippets, "@" hint), mic, circular send.
- Live telemetry during a session: context used (16.6k/1.0M + % bar), session
  wall-clock, model — all in the status bar, not in the transcript.

## What LunaMoth should copy

Mapped to our surfaces: board/roster, chat with five channels (incl. ⚡Super
Chat), card studio, per-chara gateways, settings.

1. **Adopt the three-layer frame + bottom status bar (highest leverage).**
   Per-chara persistent status bar: left = chara state (resting until HH:MM /
   working / listening — our presence + rest_until), gateway state dot,
   isolation badge (dir/sandbox/docker), net on/off; right = context meter
   (used/real window + % bar, we already know the real window from
   providers.py), session timer, model+effort chip (click = hot-swap popover),
   version. This single bar solves "where is the agent state" and "what model
   am I on" at once.

2. **Chat column ~720px max-width, left-aligned, user turns as bordered
   boxes, assistant as bare prose.** No bubbles. Keep our five channel styles
   as variations of the assistant register (muse = dimmer/italic strip, say =
   normal prose, ⚡Super Chat = accent-tinted card) rather than five layouts.
   Empty chat = the chara's wordmark/avatar + greeting line, exactly like the
   Hermes splash-as-empty-state — kills the "screen feels empty" problem and
   gives each card a brand moment.

3. **Tool calls as verb-chips with duration, expandable.** "Run · <cmd>
   1.2s", "Edited file 147ms" → expand to diff/output. Add the Hermes
   "Product | Technical" switch in Appearance: Product hides raw payloads (OC
   creators), Technical shows full I/O (developers). This is our
   three-audiences problem solved with one segmented control. Thinking =
   collapsed label + one-line gray summary, repeated between tool batches.

4. **Connectors page = master-detail with the Hermes detail formula.** For
   our messaging adapters (Telegram first) and MCP servers: inner-left list
   with brand icon + status dot; right pane with THREE separate status chips
   (Enabled/Disabled · Configured/Needs setup · Gateway running/stopped — they
   are independent facts), GET YOUR CREDENTIALS plain-language steps +
   "Open setup guide ↗", REQUIRED / RECOMMENDED (with the security why) /
   ADVANCED (n) collapsed, enable toggle bottom-left + "Save changes"
   bottom-right. Deliver say-channel-only note belongs in the blurb line.

5. **Model picker as a status-bar popover, two panels.** Left: Thinking
   toggle + Effort (Minimal/Low/Medium/High/Max); right: searchable list
   grouped by provider with current ✓, footer "Edit Models…" into settings.
   Settings → Model keeps the default ("applies to new sessions; use the
   picker to hot-swap the active chat" — copy this exact scoping sentence).
   Steal **Auxiliary models** for our helper tasks: compaction summarizer,
   title gen, (future) card-studio drafting — each "auto · use main model" +
   Change. Note: Hermes has Fallback Models; we deliberately DON'T (no-fallback
   principle) — render that row as an explicit "No fallbacks — failures are
   shown" statement instead of omitting it silently.

6. **Settings = overlay modal with two-group left nav**, behavior first
   (Chara, Chat, Appearance, Tempo & Presence, Safety, Memory & Context,
   Advanced) then infrastructure (Providers, Gateway, Toolpacks, MCP). Every
   row = bold label + one-line rationale + control; expose our real numbers
   (memory_chars, compaction threshold, THINK_WINDOW, patience, tempo) the way
   Hermes exposes Memory Budget 2200 / Compression Threshold 0.5 / Protected
   Recent Messages 20. Import/export/reset icons bottom-left of the nav.

7. **Gateway settings page: Local vs Remote as two radio cards** with
   "works offline" / auth-mechanism copy, Test + Save-for-restart +
   Save-and-reconnect, and a Diagnostics row with one-click "Open logs"
   (we have sandbox/logs/lunamoth.log — surface it exactly like this).

8. **Provider onboarding cards**: one recommended path + expandable list
   where each provider row states its auth mechanic in one gray line and
   shows "✓ Connected" inline; escape hatches "later" / "I have an API key".
   After connect, copy the monospace ceremony screen: PROVIDER CONNECTED /
   DEFAULT MODEL / model id / $ per Mtok / [ BEGIN ]. Errors must name the
   fix (env var + command) — matches our no-fallback principle.

9. **Installer pattern for `install.sh`/first-run wizard**: full step
   checklist visible up front ("0 of 11 steps"), current step highlighted,
   "Show details" → live log pane. Our desktop first-run and `lunamoth doctor`
   should render this way.

10. **Smaller steals**: empty states always say what will fill them;
    skills/toolpacks page = category chips with counts + toggle rows
    (our toolpacks page should look exactly like Skills & Tools); Artifacts
    table with session backlink (our charas' file outputs → traceable to the
    conversation); right panel as multi-mode (chara sandbox file browser /
    HTML preview with console + "Send to chat"); theme cards with mini-chat
    previews + one-line vibe (fits our themes.py skins, and per-card theme
    color slots in naturally); Usage overlay with sessions/API
    calls/tokens/est. cost per chara; PINNED sessions with the inline
    "Shift-click to pin" hint; update modal with grouped changelog; small-caps
    letterspaced section headers as the one grouping device everywhere.
