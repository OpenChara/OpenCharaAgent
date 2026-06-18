# Agent Field Notes — UI verification, cloud ops, hermes cooperation

Hard-won, reusable **methodology** for agents working on this repo. NOT a work
log. Server-specific values (IP / domain / API keys / paths) live in a **private
local runbook**, never here — keep this file secret-free so it can stay in git.

> Convention note: `CLAUDE.md` says `docs/` holds only `OPEN-WORK.md`; this
> capabilities playbook was added by explicit owner request (2026-06-18).

---

## 1. Verify the web UI *for real* — headless Chrome (Playwright)

The single most valuable technique this round: **don't guess whether the SPA
renders correctly — drive it with a real browser and capture evidence.** It
bisects "backend sends wrong data" vs "frontend renders it wrong" in one shot.

**When:** a UI bug you can't reproduce by reading code (avatar missing, message
swallowed); after a frontend change, to confirm end-to-end render (vitest units
aren't enough); to see exactly what the backend sends vs what renders.

**Recipe:**
- Playwright is already a devDep in `apps/web`. **The script MUST live inside
  `apps/web/`** — node ESM resolves `playwright` from the script's directory
  upward, so a script in `/tmp` fails with `ERR_MODULE_NOT_FOUND`.
- Launch a **clean app instance** (its own config/sandbox dir) so stale state
  can't mask the bug.
- **Hook the WebSocket** — this is the gold: you see the actual JSON-RPC traffic
  (attach/snapshot/`session.wake` params, `opening_text`, `avatar_uri`). It
  proves what the backend sent independently of what the DOM shows.
- **Screenshot each step** (`fullPage`), then read the PNGs back as images to
  *see* the result. Read the DOM too: `<img>` srcs (real data-URI vs fallback
  glyph), `body.innerText` (is the message actually there?).

```js
// apps/web/_check.mjs   ·   run from apps/web:  node _check.mjs "<url-with-#token>"
import { chromium } from "playwright";
const page = await (await chromium.launch({ headless: true })).newPage();
const ws = [];
page.on("websocket", (w) => {
  w.on("framesent", (d) => ws.push("SENT " + String(d.payload).slice(0, 1400)));
  w.on("framereceived", (d) => ws.push("RECV " + String(d.payload).slice(0, 1400)));
});
await page.goto(process.argv[2], { waitUntil: "networkidle" });
// drive the UI: page.getByText(/.../).click(), page.locator("button.btn.primary.big").click()
await page.screenshot({ path: "/tmp/shot.png", fullPage: true });
// then: inspect ws[], the screenshot, page.locator("img").evaluateAll(...)
```

**Scars:**
- The desktop URL carries token+ws in the **hash**: `http://host:port/#token=<t>&ws=<wsport>`.
  Grab it from the server's startup log; the ws port is **dynamic** per launch.
- A clean instance opens the **first-run onboarding** modal ("create" / "start
  from one of our characters"). Drive *through* it (the second button → bundled
  card picker → pick a card → wake), not around it.
- A `mock` provider (no base_url/key) makes the UI show "connect a model" and
  hides the deck. For a render-only check, set a **present (even dummy)**
  provider + key so the deck/wake unlocks — the greeting + avatar come from the
  **card**, not the model, so a dead key still renders them.
- **Clean up after**: delete the `_*.mjs` scripts (never leave them in the repo),
  the temp instance dir, and the screenshots.

---

## 2. Cloud deploy — the mechanism (decoupled from app code)

Deploy = **build a wheel locally → copy up → reinstall the installed binary →
restart the service.** Internal code changes *never* change this flow.

- Build: `scripts/build-wheel.sh` — it self-asserts the wheel bundles
  webui + cards + toolpacks (a wheel missing cards/toolpacks = charas with no
  tools/persona, a real P0; the script also cleans `build/` + `*.egg-info` so
  deleted files can't resurrect).
- Install on the server: `uv tool install --reinstall "<pkg> @ file:///path/to.whl"`,
  then restart the systemd unit.
- **Verify the new bytes are live**: service `is-active`, a local `curl
  127.0.0.1:<port>`, the public URL, AND `grep` a marker string from a NEW
  commit inside the installed `site-packages` (proves the reinstall took).
- Host / paths / unit names → the **private ops runbook**.

---

## 3. Check & clean up cloud work — inventory before you assume

- `systemctl list-units --all | grep <app>` + `list-unit-files` — defined vs running.
- `ps aux | grep <app>` — actual processes. **Watch for processes a chara spawned
  in its own sandbox**: a `live` chara can start its own background servers
  (found one running six `jarvis_*` servers under its sandbox). `pkill -f
  "/.lunamoth/sessions/"` catches those.
- `du -sh /root/* /root/.[a-z]*` — disk hogs; `.cache/uv` + `.cache/pip` are the
  usual reclaimable junk (regenerable).
- `ss -tlnp | grep :<port>` — confirm down/up.
- **Cleanup**: stop + `disable` the service; `pkill -f` the app + `serve --stdio`
  children + sandbox-spawned servers; wipe session instances — but **KEEP the
  credential file** (the global key) unless doing a full reset (re-adding a key
  is manual).
- **Scar**: a non-login `ssh host '...'` does **not** source the profile, so
  `~/.local/bin` is NOT on PATH — `which <tool>` lies even when the binary
  exists. Use full paths or `bash -lc '...'`.

---

## 4. Cooperate with the server-side hermes agent

The server runs **hermes** (a separate agent) that manages app instances via a
hermes *skill* + a thin CLI orchestrator. Lessons:

- Run hermes non-interactively: **`hermes chat -Q -q "<query>"`** (`-q` = single
  query / non-interactive, `-Q` = quiet). **Scar: `-p` is the PROFILE flag, NOT
  the prompt** — using it errors with "Invalid profile name".
- **Smoke-test the whole chain (agent + key + model) in one call**:
  `hermes chat -Q -q "Reply with exactly: OK"` — if it echoes `OK`, the
  provider/key/model all work end-to-end.
- `hermes doctor` for health (optional-tool warnings — web/rl/skills-hub/github —
  are non-blocking; the "reinstall entry point" line is a nag, not a failure).
- The orchestrator is a **thin layer over the INSTALLED app + systemd templates +
  the reverse proxy**, so it keeps working across app code changes — you only
  refresh the installed binary, the orchestrator/skill need no edits. It is
  **server-only, never committed** (it would expose infra).
- hermes (and the orchestrator's per-instance) carry their **own** keys,
  **separate** from the app's global key. Don't cross them.

---

## 5. Meta — three agents on one repo at once

- Stage **only your own files** (`git add -- <paths>`), never `git add -A`, while
  siblings are mid-edit — their uncommitted work isn't yours to commit.
- A file can change between your Read and your Edit (a sibling saved it) →
  "modified since read". Re-read and edit immediately; if it's churning every
  few seconds, pause and let it settle.
- **New methods / new files are the safest cross-agent contribution** — zero
  merge-conflict surface.
- Watch for **stragglers landing after your commit** (a sibling finishing a
  feature you depend on, e.g. wiring a config field). Verify it, then commit the
  coherent addition as a follow-up.
- When the owner says "integrate all agents' work" — *then* a broad commit is
  authorized; otherwise keep scopes disjoint.
