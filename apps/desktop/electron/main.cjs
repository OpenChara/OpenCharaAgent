// OpenCharaAgent desktop shell — main process.
//
// Hermes Desktop's shape, minimal: this app has NO renderer of its own. It
// spawns the `chara desktop` backend (HTTP renderer + WS hub), reads the
// one line the backend prints —
//   OpenCharaAgent desktop: http://127.0.0.1:PORT/#token=...&ws=PORT2
// — and loads that URL into a BrowserWindow. Backend missing, dying, or not
// printing the line is a visible error dialog and exit, never a silent
// fallback (project rule).
//
// Lifecycle: closing the window backgrounds the app to a menu-bar tray icon
// (backend keeps running); only Quit (tray menu / Cmd-Q) stops the backend.
// Chara daemons always live in their own sessions and outlive both.

const { app, BrowserWindow, Menu, Notification, Tray, dialog, ipcMain, nativeImage, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

// Name the app (menu bar, notifications, dock label) — without this an unpackaged
// `electron .` dev run shows the generic "Electron". The packaged build gets this
// from package.json productName + the bundle Info.plist.
app.setName('OpenCharaAgent')

const URL_LINE = /OpenCharaAgent desktop:\s+(http:\/\/127\.0\.0\.1:\d+\/\S*)/
// First dev launch may `uv sync` a fresh venv before the server comes up.
const STARTUP_TIMEOUT_MS = 180_000
const SIGTERM_GRACE_MS = 8_000

let win = null
let backend = null // { proc, url, origin, log: [] }
let tray = null
let quitting = false

// ---- backend discovery -------------------------------------------------------------

function installedLauncher() {
  const bin = path.join(os.homedir(), '.chara', 'bin', 'chara')
  try {
    fs.accessSync(bin, fs.constants.X_OK)
    return bin
  } catch {
    return null
  }
}

function devRepoRoot() {
  // apps/desktop/electron/main.cjs → repo root is three levels up.
  const root = path.resolve(__dirname, '..', '..', '..')
  return fs.existsSync(path.join(root, 'pyproject.toml')) ? root : null
}

function backendCommand() {
  const bin = installedLauncher()
  if (bin) return { cmd: bin, args: ['desktop', '--no-open'], cwd: os.homedir() }
  const repo = devRepoRoot()
  if (repo) {
    return {
      cmd: 'uv',
      // Both extras: a bare `--extra server` sync would strip the dev tools
      // (pytest) out of the repo venv every time the app launches.
      args: ['run', '--extra', 'dev', '--extra', 'server', 'chara', 'desktop', '--no-open'],
      cwd: repo,
    }
  }
  return null
}

// ---- backend lifecycle -------------------------------------------------------------

function fatal(title, detail) {
  if (quitting) return
  quitting = true
  dialog.showMessageBoxSync({
    type: 'error',
    title: 'OpenCharaAgent',
    message: title,
    detail: String(detail || '').slice(-2000),
  })
  stopBackend().finally(() => app.exit(1))
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const found = backendCommand()
    if (!found) {
      reject(new Error(
        'No OpenCharaAgent backend found.\n\n' +
        `Looked for ${path.join(os.homedir(), '.chara', 'bin', 'chara')} ` +
        '(install.sh) and for a development checkout next to apps/desktop.'))
      return
    }
    const log = []
    const note = (chunk) => {
      for (let line of chunk.toString('utf8').split('\n')) {
        if (!line.trim()) continue
        // The handshake line carries the auth token in the URL fragment; the
        // ring feeds error DIALOGS (and the debug log), so scrub it there —
        // the loadURL capture reads the raw stream before this buffer.
        line = line.replace(/#token=[^\s&]+/g, '#token=***')
        log.push(line)
        if (log.length > 200) log.shift()
        if (process.env.CHARA_SHELL_DEBUG) console.log('[backend]', line)
      }
    }
    let proc
    try {
      // detached → own process group, so we can SIGTERM the whole backend tree
      // (uv → python → per-chara serve children) without touching chara daemons,
      // which start in their own sessions.
      proc = spawn(found.cmd, found.args, {
        cwd: found.cwd,
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (err) {
      reject(new Error(`Could not start the backend (${found.cmd}): ${err.message}`))
      return
    }
    backend = { proc, url: null, origin: null, log }
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      reject(new Error(
        `The backend did not announce its URL within ${STARTUP_TIMEOUT_MS / 1000}s.\n\n` +
        log.slice(-15).join('\n')))
    }, STARTUP_TIMEOUT_MS)

    const scan = (chunk) => {
      note(chunk)
      if (settled) return
      const m = URL_LINE.exec(chunk.toString('utf8'))
      if (m) {
        settled = true
        clearTimeout(timer)
        backend.url = m[1]
        backend.origin = new URL(m[1]).origin
        resolve(backend)
      }
    }
    proc.stdout.on('data', scan)
    proc.stderr.on('data', scan)
    proc.on('error', (err) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`Could not start the backend (${found.cmd}): ${err.message}`))
    })
    proc.on('exit', (code, signal) => {
      const dead = backend && backend.proc === proc
      if (dead) backend = null
      if (!settled) {
        settled = true
        clearTimeout(timer)
        reject(new Error(
          `The backend exited before it was ready (${signal || `code ${code}`}).\n\n` +
          log.slice(-15).join('\n')))
      } else if (dead && !quitting) {
        fatal('The OpenCharaAgent backend stopped unexpectedly.', log.slice(-15).join('\n'))
      }
    })
  })
}

function stopBackend() {
  const current = backend
  backend = null
  if (!current || current.proc.exitCode !== null || current.proc.signalCode) {
    return Promise.resolve()
  }
  const { proc } = current
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(killTimer)
      resolve()
    }
    const killTimer = setTimeout(() => {
      try { process.kill(-proc.pid, 'SIGKILL') } catch { /* already gone */ }
    }, SIGTERM_GRACE_MS)
    proc.once('exit', finish)
    try {
      process.kill(-proc.pid, 'SIGTERM')
    } catch {
      finish()
    }
  })
}

// ---- window ------------------------------------------------------------------------

function createWindow(url, origin) {
  win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 980,
    minHeight: 620,
    show: false,
    backgroundColor: '#f5f7fa',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  win.once('ready-to-show', () => win.show())
  win.on('closed', () => { win = null })

  // Closing the window is leaving the room, not ending the visit: the app
  // retires to the menu-bar moth and the backend (with its charas' gateway)
  // keeps living. Quit lives in the tray menu / Cmd-Q.
  win.on('close', (event) => {
    if (quitting) return
    event.preventDefault()
    win.hide()
    if (app.dock) app.dock.hide() // pure menu-bar presence while backgrounded
  })

  // Only the local backend may be displayed; everything else is the system's.
  win.webContents.on('will-navigate', (event, target) => {
    if (new URL(target).origin === origin) return
    event.preventDefault()
    if (/^https?:/.test(target)) shell.openExternal(target)
  })
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target) && new URL(target).origin !== origin) shell.openExternal(target)
    return { action: 'deny' }
  })

  win.loadURL(url)
}

async function boot() {
  try {
    const { url, origin } = await startBackend()
    if (quitting) return
    createWindow(url, origin)
  } catch (err) {
    fatal('OpenCharaAgent could not start.', err.message)
  }
}

// ---- tray --------------------------------------------------------------------------

function showWindow() {
  if (app.dock) app.dock.show()
  if (win) {
    win.show()
    win.focus()
  } else if (backend && backend.url) {
    createWindow(backend.url, backend.origin)
  } else if (!quitting) {
    boot()
  }
}

function createTray() {
  // A monochrome moth silhouette (the `Template` suffix + setTemplateImage
  // let macOS auto-invert it: white on a dark menu bar, dark on a light one).
  // @2x is picked up automatically alongside trayTemplate.png.
  const icon = nativeImage.createFromPath(
    path.join(__dirname, '..', 'assets', 'trayTemplate.png'),
  )
  icon.setTemplateImage(true)
  tray = new Tray(icon)
  tray.setToolTip('OpenCharaAgent')
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 OpenCharaAgent / Open', click: showWindow },
    { type: 'separator' },
    { label: '退出 / Quit', click: () => app.quit() },
  ]))
  tray.on('click', showWindow)
}

// ---- app lifecycle -----------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  ipcMain.handle('chara:notify', (_event, payload) => {
    if (!Notification.isSupported()) return false
    const { title, body } = payload || {}
    const n = new Notification({
      title: String(title || 'OpenCharaAgent').slice(0, 80),
      body: String(body || '').slice(0, 240),
      silent: false,
    })
    n.on('click', () => {
      if (win) {
        if (win.isMinimized()) win.restore()
        win.show()
        win.focus()
      }
    })
    n.show()
    return true
  })

  app.whenReady().then(() => {
    // Dock icon: an unpackaged dev run shows the default Electron diamond; set our
    // own. (Harmless when packaged — the .app bundle icon already wins there.)
    if (process.platform === 'darwin' && app.dock) {
      const dockIcon = nativeImage.createFromPath(path.join(__dirname, '..', 'assets', 'icon.png'))
      if (!dockIcon.isEmpty()) app.dock.setIcon(dockIcon)
    }
    createTray()
    boot()
  })

  // The window hides on close (see createWindow), so this only fires when a
  // window is destroyed outright (e.g. renderer crash). The app lives on in
  // the tray either way; Quit is the only thing that stops the backend.
  app.on('window-all-closed', () => { /* tray keeps the app alive */ })

  // macOS dock click with no window: bring the visit back.
  app.on('activate', () => {
    if (!quitting) showWindow()
  })

  app.on('before-quit', (event) => {
    if (quitting) return
    quitting = true
    if (backend) {
      event.preventDefault()
      stopBackend().finally(() => app.exit(0))
    }
  })
}
