# OpenCharaAgent Desktop (Electron shell)

A deliberately thin shell, copied from the official Hermes Desktop's shape:
**no renderer of its own**. The main process spawns the `chara desktop`
backend (HTTP static renderer + WebSocket hub), parses the one URL line the
backend prints, and loads that URL into a BrowserWindow. The UI is the React SPA
in `apps/web/` (built to `src/chara/front/webui/` and served by the backend);
all logic lives in the Python backend. The shell stays renderer-agnostic — it just
loads whatever URL the backend serves, so it's unchanged by the SPA rewrite.

```
apps/desktop/
├─ electron/main.cjs      spawn backend → parse URL → window → lifecycle
├─ electron/preload.cjs   window.charaNative.notify(title, body) — the only bridge
└─ assets/icon.{svg,png}  placeholder icon (moth mark, moonlight blue)
```

## Backend resolution (in order, no silent fallback)

1. `~/.chara/bin/chara desktop --no-open` — the installed copy.
2. Development checkout: if `pyproject.toml` exists three levels up
   (i.e. this repo), `uv run --extra server chara desktop --no-open`
   with the repo as cwd.
3. Neither → error dialog, exit. A backend that dies or never prints its
   URL is likewise a visible error, never a retry-with-something-else.

## Develop

```bash
cd apps/desktop
npm install
npm run dev     # spawns the backend from this repo via uv, opens the window
```

First `npm run dev` in a fresh checkout may take a while: `uv run` builds the
virtualenv before the backend prints its URL (the shell waits up to 3 min).
`CHARA_SHELL_DEBUG=1 npm run dev` echoes backend output to the terminal.

Closing the window ends the visit: the backend process group (hub + per-chara
`serve --stdio` children) gets SIGTERM, 8 s grace, then SIGKILL. Chara daemons
are not part of that group — background life keeps running, as designed. On
macOS the app stays in the dock; clicking the dock icon starts a fresh visit
(new backend, new window).

## Package

```bash
npm run dist        # macOS arm64 dmg (primary platform)
npm run dist:linux  # Linux AppImage
```

No Windows (project platform policy). The packaged app expects the installed
backend (`install.sh` → `~/.chara/bin/chara`); it does not bundle Python.

## Security baseline

`contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`;
navigation is restricted to the spawned backend's `127.0.0.1` origin and
everything else opens in the system browser. The preload exposes exactly one
function: `charaNative.notify` (used by the renderer for `speak` system
notifications when the window is unfocused).
