"""`terminal` tool — execute shell commands under the chara's isolation.

Apple-to-apple with hermes-agent (reference/hermes-agent/tools/terminal_tool.py):
identical schema, params, and behavior, re-implemented against OpenCharaAgent's
runtime. Foreground runs through ``ctx.run_terminal`` (the existing runner that
already does isolation + 100K head/tail truncate + ANSI strip + exit-code
annotation + timeout group-kill — reused, NOT reimplemented). Background hands
off to the process registry (builtin/_process_registry.py).

Divergences from hermes, per the spec (.codex-fleet/spec-terminal-process.md):
  * Foreground over-limit timeout is CLAMPED (with a note), not rejected — the
    runner already does this. No "failure fallback" for a too-large timeout.
  * PTY (``pty=true``) runs the foreground command under a real pseudo-terminal
    INSIDE the chara's isolation jail (``runner.run_terminal_pty``) so
    interactive tools (REPLs, vim/top, isatty-branching commands) work. Same
    clamp/ANSI-strip/truncate/group-kill as the pipe path. Background PTY is not
    supported (the process registry runs over pipes) — surfaced with a note.
  * task_id / multi-backend env machinery is dropped (one process = one chara).
  * The foreground-backgrounding guard is a HARD BLOCK, apple-to-apple with
    hermes (a long-lived/self-backgrounding command in the foreground is refused
    with guidance to use background=true). This is a mature, value-NEUTRAL
    harness behavior — the chara-VALUE neutrality principle does not apply to it
    (CLAUDE.md, owner 2026-06-19).
"""
from __future__ import annotations

import json
import re

from ..registry import registry, tool_error
from ._process_registry import get_registry

# ---- foreground timeout bound (hermes FOREGROUND_MAX_TIMEOUT default 600) ----
FOREGROUND_MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 180

# workdir injection allowlist (hermes terminal_tool.py:270 _WORKDIR_SAFE_RE).
# OpenCharaAgent's `admin` isolation runs with full user privileges, so guard the
# model-supplied workdir before it reaches the shell.
_WORKDIR_SAFE_RE = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +@=,]+$")

# foreground-backgrounding advisory (hermes terminal_tool.py:1683-1726).
_SHELL_LEVEL_BACKGROUND_RE = re.compile(r"(?:^|\s|;|&&|\|\|)(?:nohup|disown|setsid)\b")
_LONG_LIVED_FOREGROUND_PATTERNS = (
    "npm run dev", "npm start", "yarn dev", "pnpm dev", "vite", "next dev",
    "uvicorn", "gunicorn", "flask run", "rails server", "python -m http.server",
    "http-server", "webpack serve", "ng serve", "jekyll serve",
)
_HELP_VERSION_RE = re.compile(r"(?:--help|-h\b|--version|-V\b|\bversion\b)")


TERMINAL_TOOL_DESCRIPTION = f"""Execute shell commands on a Linux environment. Filesystem usually persists between calls.

Do NOT use cat/head/tail to read files — use read_file instead.
Do NOT use grep/rg/find to search — use search_files instead.
Do NOT use ls to list directories — use search_files(target='files') instead.
Do NOT use sed/awk to edit files — use patch instead.
Do NOT use echo/cat heredoc to create files — use write_file instead.
Reserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.

Foreground (default): Commands return INSTANTLY when done, even if the timeout is high. Set timeout=300 for long builds/scripts — you'll still get the result in seconds if it's fast. Prefer foreground for short commands. The foreground timeout is clamped to {FOREGROUND_MAX_TIMEOUT}s max (a higher value is capped, with a note); use background=true for genuinely long-running commands.
Background: Set background=true to get a session_id. Almost always pair with notify_on_complete=true — bg without notify runs SILENTLY and you have no way to learn it finished short of calling process(action='poll') yourself. Two legitimate uses:
  (1) Long-lived processes that never exit (servers, watchers, daemons) — silent is correct, there's no exit to notify on.
  (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — MUST set notify_on_complete=true. Without it you'll either forget to poll or sit blocked waiting for the user to surface the result.
For servers/watchers, do NOT use shell-level background wrappers (nohup/disown/setsid/trailing '&') in foreground mode. Use background=true so this runtime can track lifecycle and output.
After starting a server, verify readiness with a health check or log signal, then run tests in a separate terminal() call. Avoid blind sleep loops.
Use process(action="poll") for progress checks, process(action="wait") to block until done.
Working directory: Use 'workdir' for per-command cwd.
PTY mode: Set pty=true for interactive CLI tools (Codex, Claude Code, Python REPL).

Do NOT use vim/nano/interactive tools without pty=true — they hang without a pseudo-terminal. Pipe git output to cat if it might page.
"""

TERMINAL_SCHEMA = {
    "description": TERMINAL_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute in your environment",
            },
            "background": {
                "type": "boolean",
                "description": "Run the command in the background. Almost always pair with notify_on_complete=true — without it, the process runs silently and you'll have no way to learn it finished short of calling process(action='poll') yourself (easy to forget, leading to silent blindness on long jobs). Two legitimate patterns: (1) Long-lived processes that never exit (servers, watchers, daemons) — these stay silent because there's no exit to notify on. (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — these MUST set notify_on_complete=true. For short commands, prefer foreground with a generous timeout instead.",
                "default": False,
            },
            "timeout": {
                "type": "integer",
                "description": f"Max seconds to wait (default: 180, foreground max: {FOREGROUND_MAX_TIMEOUT}). Returns INSTANTLY when command finishes — set high for long tasks, you won't wait unnecessarily. Foreground timeout is clamped to {FOREGROUND_MAX_TIMEOUT}s max (a higher value is capped, with a note); use background=true for genuinely long-running commands.",
                "minimum": 1,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for this command (absolute path). Defaults to the session working directory.",
            },
            "pty": {
                "type": "boolean",
                "description": "Run in pseudo-terminal (PTY) mode for interactive CLI tools (a REPL, vim/top, or any command that behaves differently when it detects a real terminal). Foreground only; no stdin is attached, so a command that blocks waiting for typed input will hit the timeout. Default: false.",
                "default": False,
            },
            "notify_on_complete": {
                "type": "boolean",
                "description": "When true (and background=true), you'll be automatically notified exactly once when the process finishes. **This is the right choice for almost every long-running task** — tests, builds, deployments, multi-item batch jobs, anything that takes over a minute and has a defined end. Use this and keep working on other things; the system notifies you on exit. MUTUALLY EXCLUSIVE with watch_patterns — when both are set, watch_patterns is dropped.",
                "default": False,
            },
            "watch_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Strings to watch for in background process output. HARD RATE LIMIT: at most 1 notification per 15 seconds per process — matches arriving inside the cooldown are dropped. After 3 consecutive 15-second windows with dropped matches, watch_patterns is automatically disabled for that process and promoted to notify_on_complete behavior (one notification on exit, no more mid-process spam). USE ONLY for truly rare, one-shot mid-process signals on LONG-LIVED processes that will never exit on their own — e.g. ['Application startup complete'] on a server so you know when to hit its endpoint, or ['migration done'] on a daemon. DO NOT use for: (1) end-of-run markers like 'DONE'/'PASS' — use notify_on_complete instead; (2) error patterns like 'ERROR'/'Traceback' in loops or multi-item batch jobs — they fire on every iteration and you'll hit the strike limit fast; (3) anything you'd ever combine with notify_on_complete. When in doubt, choose notify_on_complete. MUTUALLY EXCLUSIVE with notify_on_complete — set one, not both.",
            },
        },
        "required": ["command"],
    },
}


def _validate_workdir(workdir: str) -> str | None:
    """Return None if safe, else an error message (hermes:273-292)."""
    if not workdir:
        return None
    if not _WORKDIR_SAFE_RE.match(workdir):
        for ch in workdir:
            if not _WORKDIR_SAFE_RE.match(ch):
                return (
                    f"Blocked: workdir contains disallowed character {ch!r}. "
                    "Use a simple filesystem path without shell metacharacters."
                )
        return "Blocked: workdir contains disallowed characters."
    return None


def _strip_quotes(command: str) -> str:
    """Drop quoted spans so the backgrounding scan doesn't match inside literals."""
    return re.sub(r"""(['"])(?:\\.|(?!\1).)*\1""", "", command)


def _foreground_block_reason(command: str) -> str:
    """Why a foreground command must be REFUSED (empty string = allowed).

    A long-lived server/watcher/daemon in the foreground blocks the whole turn
    until the timeout kills it; shell-level self-backgrounding (nohup/disown/
    setsid/trailing &) escapes the runner's process-group tracking. Both are
    HARD-BLOCKED with guidance to use ``terminal(background=true)`` — apple-to-
    apple with hermes (terminal_tool.py:1683-1726). This is a mature, value-
    NEUTRAL harness behavior, not a chara-value choice (CLAUDE.md, owner
    2026-06-19): the harness adopts hermes's solution directly.
    """
    unquoted = _strip_quotes(command)
    if _HELP_VERSION_RE.search(unquoted):
        return ""
    low = unquoted.lower()
    if _SHELL_LEVEL_BACKGROUND_RE.search(unquoted):
        return "shell-level backgrounding (nohup/disown/setsid)"
    if re.search(r"(?:^|\s)&(?:\s|$)|&\s*$", unquoted):
        return "a trailing '&'"
    for pat in _LONG_LIVED_FOREGROUND_PATTERNS:
        if pat in low:
            return f"a long-lived server/watcher pattern ({pat})"
    return ""


def terminal(args: dict, ctx) -> str:
    command = args.get("command")
    if not isinstance(command, str):
        return tool_error(
            f"Invalid command: expected string, got {type(command).__name__}",
            output="", exit_code=-1, status="error",
        )

    background = bool(args.get("background", False))
    timeout = args.get("timeout")
    workdir = args.get("workdir")
    pty = bool(args.get("pty", False))
    notify_on_complete = bool(args.get("notify_on_complete", False))
    watch_patterns = args.get("watch_patterns")

    if workdir:
        wd_err = _validate_workdir(str(workdir))
        if wd_err:
            return tool_error(wd_err, output="", exit_code=-1, status="blocked")

    effective_timeout = int(timeout) if timeout else (DEFAULT_TIMEOUT if not background else None)

    if background:
        return _run_background(
            command, ctx,
            workdir=workdir,
            notify_on_complete=notify_on_complete,
            watch_patterns=watch_patterns,
            pty=pty,
        )

    # ---- foreground ----
    # Hard-block (hermes parity): a long-lived/self-backgrounding command in the
    # foreground would wedge the turn — refuse and point at background=true.
    blocked = _foreground_block_reason(command)
    if blocked:
        return tool_error(
            f"Refused: this looks like {blocked} run in the foreground, which would "
            "block this turn until the timeout. Use terminal(background=true) "
            "(pair with notify_on_complete=true) for servers / watchers / daemons / "
            "long-running tasks.",
            output="", exit_code=-1, status="blocked",
        )
    from pathlib import Path
    wd: Path | None = None
    if workdir:
        wd = Path(workdir) if str(workdir).startswith("/") else (ctx.workspace / workdir)
    eff = effective_timeout or DEFAULT_TIMEOUT
    # The runner already clamps the timeout into [1, 600] and appends a note, so
    # an over-limit foreground timeout is clamped (not rejected) — no fallback.
    if pty:
        return _run_foreground_pty(command, ctx, timeout=eff, workdir=wd).strip()
    term = ctx.run_terminal_result(command, timeout=eff, workdir=wd)
    out = (term.text or "").strip()
    # A refused run (isolation jail unavailable → the command NEVER executed) or a
    # timeout is a real failure, not a result — surface it as a tool_error so the
    # chara is told the cause and the loop guard counts it, instead of a plain
    # string the gateway reads as success. A non-zero exit stays plain text
    # (hermes parity: the exit code is in the output and the model reads it).
    if term.refused or term.timed_out:
        return tool_error(
            out or ("the command was refused (isolation jail unavailable)"
                    if term.refused else f"the command timed out after {eff}s"),
            output=out,
            exit_code=term.exit_code if term.exit_code is not None else -1,
            status="refused" if term.refused else "timeout",
        )
    return out


def _run_foreground_pty(command, ctx, *, timeout, workdir) -> str:
    """Foreground PTY run: same isolation/clamp/strip/truncate as the pipe path,
    but on a real pseudo-terminal (``runner.run_terminal_pty``). Env facts come
    from the live state snapshot (ctx.run_terminal isn't pty-aware)."""
    from ..runner import run_terminal_pty
    perms = ctx.permissions()
    return run_terminal_pty(
        command,
        ctx.workspace,
        isolation=perms.isolation or None,
        allow_network=perms.network_on,
        writable_paths=perms.writable_paths,
        timeout=timeout,
        workdir=str(workdir) if workdir else None,
    )


def _run_background(command, ctx, *, workdir, notify_on_complete, watch_patterns, pty) -> str:
    from pathlib import Path

    # mutex (hermes _resolve_notification_flag_conflict): notify wins, watch dropped.
    watch_ignored = False
    if notify_on_complete and watch_patterns:
        watch_patterns = None
        watch_ignored = True

    cwd: Path | None = None
    if workdir:
        cwd = Path(workdir) if str(workdir).startswith("/") else (ctx.workspace / workdir)

    perms = ctx.permissions()
    isolation = perms.isolation
    allow_network = perms.network_on
    writable_paths = perms.writable_paths

    reg = get_registry(ctx)
    try:
        session = reg.spawn(
            command,
            ctx.workspace,
            isolation=isolation,
            allow_network=allow_network,
            writable_paths=writable_paths,
            cwd=cwd,
            notify_on_complete=notify_on_complete,
            watch_patterns=watch_patterns,
        )
    except Exception as e:  # noqa: BLE001
        return tool_error(
            f"Failed to start background process: {e}",
            output="", exit_code=-1,
        )

    result = {
        "output": "Background process started",
        "session_id": session.id,
        "pid": session.pid,
        "exit_code": 0,
        "error": None,
    }
    if notify_on_complete:
        result["notify_on_complete"] = True
    if watch_patterns:
        result["watch_patterns"] = list(watch_patterns)
    if watch_ignored:
        result["watch_patterns_ignored"] = (
            "watch_patterns dropped because notify_on_complete is also set (mutually exclusive)"
        )
    if pty:
        result["pty_note"] = "pty mode applies to foreground commands only; this background process ran without a pseudo-terminal"
    if not notify_on_complete and not watch_patterns:
        result["hint"] = (
            "This background process runs SILENTLY — you'll only learn its outcome "
            "by calling process(action='poll'/'wait'). For bounded tasks, prefer "
            "notify_on_complete=true."
        )

    # watch_patterns matches re-emit as notes in the returned JSON (no protocol
    # coupling): surface anything already queued at spawn time.
    notes = _collect_watch_notes(reg)
    if notes:
        result["notes"] = notes
    return json.dumps(result, ensure_ascii=False)


def _collect_watch_notes(reg) -> list[str]:
    """Drain pending WATCH events into plain-text notes for the JSON result (the
    spec's "re-emit watch_patterns as notes" requirement). Uses drain_watch_notes
    so it consumes ONLY watch_* events — completion/image_gen notices stay queued
    for the agent layer's drain_notifications(), which is their sole consumer.
    (Draining them destructively here silently dropped 'job finished' notices.)"""
    notes: list[str] = []
    for evt in reg.drain_watch_notes():
        etype = evt.get("type")
        if etype == "watch_match":
            notes.append(f"[watch:{evt.get('pattern')}] {evt.get('output', '')}")
        elif evt.get("message"):
            notes.append(str(evt["message"]))
    return notes


registry.register(
    "terminal",
    "terminal",
    TERMINAL_SCHEMA,
    terminal,
    emoji="💻",
    max_result_size_chars=100_000,
)
