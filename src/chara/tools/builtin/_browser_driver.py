"""agent-browser driver — the subprocess + daemon manager behind the browser
tool suite (faithful port of hermes-agent/tools/browser_tool.py's driver layer:
``_find_agent_browser``, ``_run_browser_command``, ``_create_local_session``,
``_chromium_installed``, ``_socket_safe_tmpdir``, ``_write_owner_pid``).

Underscore-prefixed so the registry's discovery scan (top-level
``registry.register`` only) never imports it as a tool module.

The automation engine is NOT in-process Playwright/CDP — it is the external
Node CLI ``agent-browser`` (npm ``agent-browser@^0.26.0``), shelled out once per
tool call. State across calls is held by a long-lived **agent-browser daemon**
keyed by ``--session <name>`` + a per-task socket dir; the Python side is
stateless per call. The page persists because subsequent CLI calls with the
same session name reconnect to the same daemon/page.

THE #1 FOOT-GUN (replicated here): capture stdout/stderr to TEMP FILES, never
pipes. agent-browser spawns a background daemon that inherits fds; with pipes
``communicate()`` never sees EOF and hangs to timeout every call.

BROWSER UNDER THE SANDBOX (the browser is a first-class tool even when jailed —
owner 2026-06-19): a real Chromium DOES run under OpenCharaAgent's default ``sandbox``
isolation, via a browser-specific jail (``session.isolation`` ``browser=True``).
The trick: Chromium can't nest its own sandbox inside our OS jail, so we pass
``--no-sandbox`` (auto-injected below whenever isolation != admin) and let the
OUTER jail be the only boundary — an inverted profile that allows by default but
confines writes to the workspace + the temp dirs Chrome scratches in (its
user-data-dir + ProcessSingleton socket land in the per-user Darwin temp; the
agent-browser socket in /tmp) and keeps the secret home unreadable. The macOS
path is validated end-to-end (2026-06-19); the Linux bwrap/Landlock variants are
implemented on the same principle but still want validation on a real Linux host
(see docs/OPEN-WORK.md). ``admin`` isolation also works (no jail; --no-sandbox
only when root/AppArmor).

OpenCharaAgent adaptation: hermes reaches a global subprocess layer keyed by task_id;
OpenCharaAgent is one-process-one-chara, so the *session manager* is an ephemeral
store stashed on ``ctx.browser`` (created lazily). ``task_id`` is the internal
session key — defaults to ``"default"`` — and is NOT part of any tool schema.
Commands run through ``ctx.run_terminal`` so the chara's isolation
(sandbox/admin) and network/writable facts are honoured; agent-browser is
invoked by absolute path with its socket-dir / idle-timeout / no-sandbox env
baked into the command line via env-prefix, because ``run_terminal`` takes a
single shell string.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("chara.tools.browser")

# Snapshot truncation threshold (hermes browser_tool.py:191).
SNAPSHOT_SUMMARIZE_THRESHOLD = 8000

# Commands that legitimately return no output (hermes :194).
_EMPTY_OK_COMMANDS = frozenset({"close", "record"})

# Daemon self-idle timeout, ms (hermes BROWSER_SESSION_INACTIVITY_TIMEOUT*1000).
_BROWSER_IDLE_TIMEOUT_SECONDS = int(os.environ.get("BROWSER_INACTIVITY_TIMEOUT", "300"))

# Default per-call command timeout (seconds). hermes reads browser.command_timeout;
# OpenCharaAgent has no such config — use a sane default, overridable by env.
_DEFAULT_COMMAND_TIMEOUT = int(os.environ.get("BROWSER_COMMAND_TIMEOUT", "30"))

_cached_agent_browser: Optional[str] = None
_agent_browser_resolved = False
_cached_chromium_installed: Optional[bool] = None
_resolve_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CLI + Chromium discovery (hermes _find_agent_browser :1753, _chromium_installed :3580)
# ---------------------------------------------------------------------------

def find_agent_browser() -> Optional[str]:
    """Resolve the ``agent-browser`` CLI. Returns an absolute path, the literal
    ``"npx agent-browser"`` fallback, or ``None`` when nothing is found. Result
    cached (the binary doesn't move mid-process)."""
    global _cached_agent_browser, _agent_browser_resolved
    with _resolve_lock:
        if _agent_browser_resolved:
            return _cached_agent_browser

        found = shutil.which("agent-browser")
        if found:
            _cached_agent_browser = found
            _agent_browser_resolved = True
            return found

        # Local node_modules/.bin (npm install in repo root or ~/.chara).
        for base in _node_modules_bin_dirs():
            local = shutil.which("agent-browser", path=str(base))
            if local:
                _cached_agent_browser = local
                _agent_browser_resolved = True
                return local

        # npx fallback.
        if shutil.which("npx"):
            _cached_agent_browser = "npx agent-browser"
            _agent_browser_resolved = True
            return _cached_agent_browser

        _cached_agent_browser = None
        _agent_browser_resolved = True
        return None


def _node_modules_bin_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = Path.home()
    dirs.append(home / ".chara" / "node" / "node_modules" / ".bin")
    # repo-root node_modules (when running from a checkout).
    dirs.append(Path(__file__).resolve().parents[4] / "node_modules" / ".bin")
    return [d for d in dirs if d.is_dir()]


def _chromium_search_roots() -> list[str]:
    roots: list[str] = []
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if pw:
        roots.append(pw)
    home = Path.home()
    roots.append(str(home / ".chara" / ".playwright"))
    roots.append(str(home / ".cache" / "ms-playwright"))            # Linux default
    roots.append(str(home / "Library" / "Caches" / "ms-playwright"))  # macOS default
    return roots


def chromium_installed() -> bool:
    """True when a usable Chromium/headless-shell build is on disk (hermes
    :3580): (1) ``AGENT_BROWSER_EXECUTABLE_PATH`` env, (2) system chrome in
    PATH, (3) a ``chromium-*`` / ``chromium_headless_shell-*`` dir in a
    Playwright cache root. Cached."""
    global _cached_chromium_installed
    if _cached_chromium_installed is not None:
        return _cached_chromium_installed

    ab_path = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if ab_path and (os.path.isfile(ab_path) or shutil.which(ab_path)):
        _cached_chromium_installed = True
        return True

    if (shutil.which("google-chrome") or shutil.which("chromium")
            or shutil.which("chromium-browser") or shutil.which("chrome")):
        _cached_chromium_installed = True
        return True

    # agent-browser's OWN managed browser (`agent-browser install` puts a
    # chrome-<version>/chrome under ~/.agent-browser/browsers — NOT a Playwright
    # cache). This is the install path the deploy requirements set up.
    ab_browsers = Path.home() / ".agent-browser" / "browsers"
    try:
        for entry in ab_browsers.iterdir():
            if entry.name.startswith(("chrome-", "chromium-", "chrome_", "chromium_")):
                _cached_chromium_installed = True
                return True
    except OSError:
        pass

    for root in _chromium_search_roots():
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.startswith("chromium-") or entry.startswith("chromium_headless_shell-"):
                _cached_chromium_installed = True
                return True

    _cached_chromium_installed = False
    return False


def ensure_crashpad_db_fix() -> bool:
    """Shim agent-browser's bundled ``chrome_crashpad_handler`` so it can't kill
    Chrome under our OS jail.

    Chrome-for-Testing headless sometimes spawns the crashpad handler WITHOUT a
    ``--database`` argument; the handler then prints "--database is required" and
    EXITS, Chrome treats the handler death as fatal and exits early ("Chrome
    exited early without DevToolsActivePort"). Seen under the Linux **Landlock**
    tier (Docker). We replace the handler with a tiny shim that injects a writable
    ``--database`` when Chrome omits one (and passes through otherwise — so on
    bwrap/macOS, where Chrome supplies its own, nothing changes).

    Idempotent (keeps the real handler as ``*.real``); safe to call on every
    ``setup browser``. Returns True if a shim is in place after the call."""
    done = False
    try:
        # rglob: the handler is top-level on Linux but nested inside the .app
        # bundle on macOS (…/Google Chrome for Testing.app/Contents/Frameworks/
        # …/Helpers/chrome_crashpad_handler).
        handlers = [h for h in (Path.home() / ".agent-browser" / "browsers").rglob("chrome_crashpad_handler")
                    if not h.name.endswith(".real")]
    except OSError:
        return False
    for h in handlers:
        real = h.with_suffix(h.suffix + ".real") if h.suffix else Path(str(h) + ".real")
        try:
            if real.exists():
                done = True  # already shimmed
                continue
            h.rename(real)
            shim = (
                "#!/bin/bash\n"
                "# OpenCharaAgent shim: inject a writable --database when Chrome omits it\n"
                "# (Chrome-for-Testing headless does, which kills Chrome under the jail).\n"
                'case "$*" in\n'
                f'  *--database=*) exec "{real}" "$@" ;;\n'
                f'  *) mkdir -p /tmp/chara-crashpad 2>/dev/null; exec "{real}" --database=/tmp/chara-crashpad "$@" ;;\n'
                "esac\n"
            )
            h.write_text(shim, encoding="utf-8")
            h.chmod(0o755)
            done = True
        except OSError:
            pass
    return done


def is_browser_available() -> bool:
    """check_fn gating the whole toolset: True only when BOTH the agent-browser
    CLI and a Chromium build are present. Absent driver → tools hidden, not
    broken (clean degrade; no-fallback principle)."""
    return bool(find_agent_browser()) and chromium_installed()


def _reset_caches_for_test() -> None:
    """Test hook: clear the resolution caches so a monkeypatched discovery
    re-runs."""
    global _cached_agent_browser, _agent_browser_resolved, _cached_chromium_installed
    with _resolve_lock:
        _cached_agent_browser = None
        _agent_browser_resolved = False
        _cached_chromium_installed = None


# ---------------------------------------------------------------------------
# Socket-safe tmpdir + owner pid (hermes _socket_safe_tmpdir :1136, _write_owner_pid :1275)
# ---------------------------------------------------------------------------

def _socket_safe_tmpdir() -> str:
    """A short temp dir suitable for AF_UNIX sockets. macOS TMPDIR
    (/var/folders/...) overflows the 104-byte AF_UNIX path limit once the
    session suffix is appended, so use /tmp directly there."""
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


def _write_owner_pid(socket_dir: str, session_name: str) -> None:
    """Record this process as the session owner for cross-process orphan
    reaping (hermes :1275). Best-effort."""
    try:
        Path(socket_dir, f"{session_name}.owner_pid").write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-chara session manager (ephemeral, stashed on ctx.browser)
# ---------------------------------------------------------------------------

class BrowserSessions:
    """Holds the agent-browser session name(s) for this chara. hermes keys by
    task_id across a multi-env process; OpenCharaAgent is one-process-one-chara, so
    this is a tiny per-session store created lazily on ``ctx.browser``."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_or_create(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            info = self._sessions.get(task_id)
            if info is None:
                info = {
                    "session_name": f"lm_{uuid.uuid4().hex[:10]}",
                    "first_nav": True,
                }
                self._sessions[task_id] = info
            return info

    def drop(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._sessions.pop(task_id, None)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._sessions.values())


def _ctx_sessions(ctx) -> BrowserSessions:
    mgr = getattr(ctx, "browser", None)
    if not isinstance(mgr, BrowserSessions):
        mgr = BrowserSessions()
        ctx.browser = mgr
    return mgr


# ---------------------------------------------------------------------------
# Command runner (hermes _run_browser_command :1877) — temp files, not pipes
# ---------------------------------------------------------------------------

def _needs_sandbox_bypass() -> bool:
    """Chromium refuses to start as root, and AppArmor (Ubuntu 23.10+) restricts
    unprivileged user namespaces → inject --no-sandbox (hermes :2012)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def _build_command_string(cli: str, session_name: str, command: str,
                          args: list[str], socket_dir: str, isolation: str = "sandbox") -> str:
    """Build the shell command string for ctx.run_terminal. Env facts the
    daemon needs are exported inline (run_terminal takes one shell string, so
    we can't pass an env dict). Everything is shell-quoted."""
    if cli == "npx agent-browser":
        prefix_parts = ["npx", "agent-browser"]
    else:
        prefix_parts = [cli]

    argv = prefix_parts + ["--session", session_name, "--json", command] + list(args)
    cmd = " ".join(shlex.quote(p) for p in argv)

    env_exports = [
        f"AGENT_BROWSER_SOCKET_DIR={shlex.quote(socket_dir)}",
        f"AGENT_BROWSER_IDLE_TIMEOUT_MS={_BROWSER_IDLE_TIMEOUT_SECONDS * 1000}",
        # Override the jail's TMPDIR (=workspace) with a SHORT temp base for the
        # browser only. agent-browser derives Chrome's --user-data-dir from
        # os.tmpdir()=$TMPDIR; under a deep workspace (~/.chara/sessions/<name>/
        # sandbox/workspace) that overflows macOS's 104-char AF_UNIX socket limit
        # → Chrome FATALs (path_service "Failed to get the path", crashpad empty
        # mkdir). A short base (/tmp → /private/tmp on macOS) keeps Chrome's
        # profile + ProcessSingleton socket short. The dir is writable in every
        # browser jail (macOS allows /private/tmp; bwrap binds /tmp; Landlock rw /tmp).
        f"TMPDIR={shlex.quote(_socket_safe_tmpdir())}",
    ]
    # Chromium can't nest its own sandbox inside our OS jail (bwrap/seatbelt), so
    # under `sandbox` isolation we MUST pass --no-sandbox — the outer jail is the
    # real boundary (writes confined to workspace+temp, secret home unreadable;
    # verified macOS 2026-06-19). Under `admin` only root/AppArmor needs it.
    needs_bypass = (isolation or "sandbox").lower() != "admin" or _needs_sandbox_bypass()
    if (not os.environ.get("AGENT_BROWSER_ARGS")
            and not os.environ.get("AGENT_BROWSER_CHROME_FLAGS")
            and needs_bypass):
        # Pin crashpad's database to a SHORT, jail-writable path. On macOS the
        # crashpad handler is IN-PROCESS (crash_report_database_mac.mm): it derives
        # its db from the user-data-dir and FATALs on an empty mkdir when that path
        # is too long — the Linux external-handler --database shim
        # (ensure_crashpad_db_fix) can't cover the in-process path. An explicit
        # --crash-dumps-dir closes that out on every platform (no comma in the path,
        # so it survives agent-browser's comma-split of AGENT_BROWSER_ARGS).
        crash_dir = os.path.join(_socket_safe_tmpdir(), "chara-crashpad")
        flags = [
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            f"--crash-dumps-dir={crash_dir}",
        ]
        env_exports.append("AGENT_BROWSER_ARGS=" + ",".join(flags))

    return " ".join(env_exports) + " " + cmd


def run_browser_command(ctx, task_id: str, command: str,
                        args: Optional[list[str]] = None,
                        timeout: Optional[int] = None) -> dict[str, Any]:
    """Run one agent-browser CLI command for ``task_id``. Returns the parsed
    JSON envelope ``{"success": bool, "data"|"error": ...}``. Mirrors hermes
    ``_run_browser_command``: discovery fail-fast, per-task socket dir, owner
    pid, idle-timeout + no-sandbox env, stdout/stderr to TEMP FILES, JSON parse
    with empty-output-as-failure and a non-JSON guard."""
    args = list(args or [])
    if timeout is None:
        timeout = _DEFAULT_COMMAND_TIMEOUT

    cli = find_agent_browser()
    if not cli:
        return {"success": False, "error": (
            "agent-browser CLI not found. Install it with: "
            "npm install -g agent-browser  (then: agent-browser install)"
        )}

    if not chromium_installed():
        return {"success": False, "error": (
            "Chromium browser is missing. Install it with: "
            "agent-browser install --with-deps "
            "(or: npx playwright install --with-deps chromium)"
        )}

    info = _ctx_sessions(ctx).get_or_create(task_id)
    session_name = info["session_name"]

    socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
    try:
        os.makedirs(socket_dir, mode=0o700, exist_ok=True)
    except OSError as e:
        return {"success": False, "error": f"Failed to create socket directory: {e}"}
    _write_owner_pid(socket_dir, session_name)

    # stdout/stderr to TEMP FILES (the #1 foot-gun): the daemon inherits the
    # shell's fds; piped output never hits EOF and hangs every call. We tee the
    # CLI's stdout into a file inside the socket dir and read it back. Because
    # run_terminal returns the captured terminal output too, we ALSO parse that
    # as a fallback — but the file is authoritative.
    stdout_path = os.path.join(socket_dir, f"_stdout_{command}")
    stderr_path = os.path.join(socket_dir, f"_stderr_{command}")

    try:
        isolation = ctx.isolation()
    except Exception:  # noqa: BLE001 — defensive: fall back to the jailed default
        isolation = "sandbox"
    base_cmd = _build_command_string(cli, session_name, command, args, socket_dir, isolation)
    # Redirect to temp files; do not pipe. </dev/null detaches stdin so an
    # inherited daemon fd can't keep the parent shell open.
    full_cmd = (
        f"{base_cmd} > {shlex.quote(stdout_path)} 2> {shlex.quote(stderr_path)} "
        f"< /dev/null"
    )

    try:
        # browser=True selects the Chromium-capable jail (see session.isolation).
        ctx.run_terminal(full_cmd, timeout=timeout, browser=True)
    except Exception as e:  # noqa: BLE001 — surface as a tool failure, never raise
        logger.warning("browser '%s' run_terminal error: %s", command, e)
        return {"success": False, "error": f"Command failed: {e}"}

    stdout_text = _read_text(stdout_path).strip()
    stderr_text = _read_text(stderr_path).strip()
    # Best-effort cleanup of the temp files.
    for p in (stdout_path, stderr_path):
        try:
            os.unlink(p)
        except OSError:
            pass

    if stderr_text:
        logger.debug("browser '%s' stderr: %s", command, stderr_text[:500])

    if not stdout_text:
        if command in _EMPTY_OK_COMMANDS:
            return {"success": True, "data": {}}
        if stderr_text:
            return {"success": False, "error": stderr_text}
        return {"success": False, "error": f"Browser command '{command}' returned no output"}

    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        raw = stdout_text[:2000]
        # screenshot can emit a bare path on some agent-browser builds.
        if command == "screenshot":
            recovered = _extract_screenshot_path(stdout_text + "\n" + stderr_text)
            if recovered and Path(recovered).exists():
                return {"success": True, "data": {"path": recovered, "raw": raw}}
        return {"success": False,
                "error": f"Non-JSON output from agent-browser for '{command}': {raw}"}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _extract_screenshot_path(text: str) -> Optional[str]:
    for token in text.replace("\n", " ").split():
        if token.endswith(".png"):
            return token
    return None


# ---------------------------------------------------------------------------
# Snapshot size discipline (hermes _truncate_snapshot)
# ---------------------------------------------------------------------------

def truncate_snapshot(snapshot_text: str, limit: int = SNAPSHOT_SUMMARIZE_THRESHOLD) -> str:
    if len(snapshot_text) <= limit:
        return snapshot_text
    head = snapshot_text[:limit]
    return head + f"\n\n... [snapshot truncated at {limit} chars; call browser_snapshot with full=false to refine or scroll]"
