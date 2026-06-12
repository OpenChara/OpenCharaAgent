from __future__ import annotations

import argparse
import select
import sys
import time
from dataclasses import dataclass

from ..obs import get_logger
from ..content.themes import LUNAMOTH_BANNER
from ..presence import normalize_mode
from ..protocol import Notice, TextDelta, ToolEnd
from ..protocol.api import GRANT_WORDS, CharaHandle
from ..session.cleanup import clean_runtime_sandbox

BANNER = (
    LUNAMOTH_BANNER
    + "\nLUNAMOTH // LOCAL AGENTIC CHARACTER RUNTIME"
    + "\nHuman input interrupts the display stream. /help for commands. Ctrl-C to quit.\n"
)

STDIN_ACTIVE = True
_log = get_logger("terminal")


def permanent_model_error(message: str) -> bool:
    return message.startswith(("HTTP 401", "HTTP 403", "HTTP 404"))


@dataclass
class TerminalState:
    running: bool = True
    eternal: bool = True
    idle_backoff: float = 0.0
    idle_blocked_until: float = 0.0

    def reset_idle_backoff(self) -> None:
        self.idle_backoff = 0.0
        self.idle_blocked_until = 0.0

    def note_permanent_idle_error(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self.idle_backoff = 60.0 if self.idle_backoff <= 0 else min(self.idle_backoff * 2.0, 1800.0)
        self.idle_blocked_until = now + self.idle_backoff
        return self.idle_backoff

    def idle_delay_remaining(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.idle_blocked_until - now)


def _stdin_line_ready() -> bool:
    if not STDIN_ACTIVE:
        return False
    r, _, _ = select.select([sys.stdin], [], [], 0)
    return bool(r)


def _read_line() -> str | None:
    global STDIN_ACTIVE
    if not _stdin_line_ready():
        return None
    line = sys.stdin.readline()
    if line == "":
        STDIN_ACTIVE = False
        return None
    return line.rstrip("\n")


def _prompt() -> None:
    print('\noperator> ', end='', flush=True)


def _stream_with_interrupt(prefix: str, events, allow_interrupt: bool = True) -> tuple[str, str | None]:
    print(prefix, end='', flush=True)
    full: list[str] = []
    for ev in events:
        if allow_interrupt and _stdin_line_ready():
            line = _read_line()
            print("\n\x1b[31m[INTERRUPT: operator input overrides current cycle]\x1b[0m", flush=True)
            return "".join(full), line
        # Plain-mode rendering policy: prose prints as-is, tool results and
        # notices print ANSI-dim, thinking stays silent (the TUI has the ✶
        # indicator; legacy mode just doesn't mention it).
        if isinstance(ev, TextDelta):
            print(ev.text, end='', flush=True)
            full.append(ev.text)
        elif isinstance(ev, ToolEnd) and ev.summary:
            print(f"\n\x1b[2m{ev.summary}\x1b[0m\n", end='', flush=True)
        elif isinstance(ev, Notice) and ev.text:
            print(f"\n\x1b[2m{ev.text}\x1b[0m\n", end='', flush=True)
    print('', flush=True)
    return "".join(full), None


def _cooldown(seconds: float) -> str | None:
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if _stdin_line_ready():
            return _read_line()
        time.sleep(min(0.05, end - time.monotonic()))
    return None


def _cycle_pause(handle: CharaHandle, base_patience: float) -> float:
    tempo = max(0.1, float(getattr(handle.snapshot(), "tempo", 1.0) or 1.0))
    return max(0.0, base_patience) / tempo


def _stdin_permission_hook(kind: str, reason: str, detail: str, wait_seconds: int) -> bool:
    """request_permission hook for the plain terminal: ask on stdout, wait on stdin.

    Single-threaded by design — the tool call blocks the stream until the operator
    answers or the model's own deadline passes (timeout = deny)."""
    label = kind + (f" ({detail})" if detail.strip() else "")
    print(f"\n⚿ permission request: {label}", flush=True)
    if reason.strip():
        print(f"  reason: {reason.strip()}", flush=True)
    print(f"  y/yes = allow · anything else denies · auto-deny in {wait_seconds}s: ", end="", flush=True)
    end = time.monotonic() + max(1, wait_seconds)
    while time.monotonic() < end:
        if _stdin_line_ready():
            line = (_read_line() or "").strip().lower()
            granted = line in GRANT_WORDS
            print(f"  → {'granted' if granted else 'denied'}", flush=True)
            return granted
        time.sleep(0.05)
    print("\n  → denied (no answer)", flush=True)
    return False


def _idle_ready(state: TerminalState, handle: CharaHandle, last_user_at: float) -> bool:
    """Gate spontaneous cycles: live/chat state, quiet engagement, rest, backoff."""
    if not state.eternal or state.idle_delay_remaining() > 0:
        return False
    quiet = max(0, int(getattr(handle.settings, "quiet", 300)))  # live: /quiet re-reads
    engaged = last_user_at and time.monotonic() < last_user_at + quiet
    resting = handle.snapshot().rest_until > time.time()
    return bool(not engaged and not resting)


def _run_idle_cycle(
    state: TerminalState,
    handle: CharaHandle,
    think_pfx: str,
    base_patience: float,
    *,
    interactive: bool,
) -> str | None:
    print("\n\x1b[2m· idle ·\x1b[0m", flush=True)
    try:
        _, interrupt = _stream_with_interrupt(think_pfx, handle.stream_idle(), allow_interrupt=True)
        state.reset_idle_backoff()
    except RuntimeError as e:
        message = str(e)
        if not permanent_model_error(message):
            raise
        delay = state.note_permanent_idle_error()
        _log.error(
            "permanent model error during idle; backing off spontaneous cycles for %.0fs: %s",
            delay,
            message[:200],
        )
        print(
            f"\n\x1b[31m[model error: {message} — idle cycles back off for {int(delay)}s; "
            "operator input still tries immediately]\x1b[0m",
            flush=True,
        )
        return None
    if interrupt is not None:
        state.reset_idle_backoff()
        return interrupt
    pending_line = _cooldown(_cycle_pause(handle, base_patience))
    if interactive:
        _prompt()
    return pending_line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='LunaMoth plain terminal mode (legacy; the TUI is the default)')
    parser.add_argument('--mode', choices=['live', 'chat'], default='',
                        help="interaction mode override (default: the chara's persisted setting)")
    parser.add_argument('--no-think', action='store_true', help=argparse.SUPPRESS)  # pre-rename alias for --mode chat
    parser.add_argument('--patience', '--cooldown', dest='patience', type=float, default=None,
                        help='override pause between spontaneous cycles, in seconds (default: chara setting)')
    parser.add_argument('--no-stream', action='store_true', help='use non-streaming fallback output')
    parser.add_argument('--clean-on-exit', action='store_true', help='wipe the session sandbox on shutdown (default: persist)')
    parser.add_argument('--no-clean-on-exit', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--debug', action='store_true', help='DEBUG-level diagnostics in sandbox/logs/')
    args = parser.parse_args(argv)
    if args.debug:
        import os

        os.environ['LUNAMOTH_DEBUG'] = '1'  # picked up by setup_logging in the agent

    base_patience = float(args.patience) if args.patience is not None else None
    # Presence: interactive terminal = operator attached; detached daemon
    # (stdin is /dev/null, started by `lunamoth start`) = operator away. The
    # handle does the presence/handoff bookkeeping on attach.
    interactive = sys.stdin.isatty()
    handle = CharaHandle()
    if interactive:
        handle.set_permission_hook(_stdin_permission_hook)
    info = handle.attach(present=interactive)
    if base_patience is None:
        base_patience = float(getattr(handle.snapshot(fresh=True), "patience", 600.0) or 600.0)
    name = info.char_name
    reply_pfx, think_pfx = f"{name}> ", f"{name}~ "

    mode = normalize_mode(args.mode or ("chat" if args.no_think else "") or info.mode)
    # The daemon always lives on its own; while attached, only live mode self-runs.
    state = TerminalState(eternal=(not interactive) or mode == "live")
    restored = bool(info.restored)  # transcript reloaded on attach
    print(BANNER)
    if interactive:
        if restored:
            print(f"[restored {len(info.restored)} message(s) from the transcript]", flush=True)
        # The opening move is DECIDED by the handle (one tree for every frontend);
        # this frontend only renders it.
        if info.opening == "greeting":
            print(f"{reply_pfx}{info.opening_text}", flush=True)
            handle.record_greeting(info.opening_text)
        elif info.opening == "arrival":
            _stream_with_interrupt(reply_pfx, handle.stream_event(info.opening_text), allow_interrupt=False)
        elif info.opening == "probe":
            _stream_with_interrupt(reply_pfx, handle.stream_user(info.opening_text), allow_interrupt=False)
    pending_line: str | None = None
    # Engagement: while the operator is actively talking, the chara's own life
    # waits; it resumes settings.quiet seconds after their last word.
    last_user_at = 0.0
    if interactive:
        _prompt()
        # Attach grace (live mode): leave the operator room for the first word; if
        # they type during the grace this captures it as the first pending line.
        if state.eternal:
            pending_line = _cooldown(max(30.0, 2 * _cycle_pause(handle, base_patience)))

    try:
        while state.running:
            if pending_line is None and _stdin_line_ready():
                pending_line = _read_line()
            if pending_line is not None:
                line = pending_line
                pending_line = None
                if line is None:
                    continue
                stripped = line.strip()
                if stripped in {'/quit', '/exit'}:
                    print('shutting down.')
                    break
                if stripped == '/toggle_think':
                    state.eternal = not state.eternal
                    print(f'eternal thinking = {state.eternal}')
                    _prompt()
                    continue
                if stripped == '/pause_think':
                    state.eternal = False
                    print('eternal thinking = False')
                    _prompt()
                    continue
                if stripped == '/resume_think':
                    state.eternal = True
                    print('eternal thinking = True')
                    _prompt()
                    continue
                if stripped.startswith(('/set_patience ', '/set_cooldown ')):
                    try:
                        base_patience = max(0.0, float(stripped.split(maxsplit=1)[1]))
                        print(f'patience = {base_patience}s')
                    except Exception as e:
                        print(f'bad patience: {e}')
                    _prompt()
                    continue
                if stripped == '/help':
                    print("\n".join(f"{c.usage:<34} {c.help}" for c in handle.commands()))
                    print('plain-terminal extras: /toggle_think /pause_think /resume_think /set_patience <sec> /exit')
                    _prompt()
                    continue
                if stripped.startswith('/'):
                    # Backend commands run through the shared registry — same
                    # behavior and help text as every other frontend.
                    print(handle.command(stripped).text, flush=True)
                    state.reset_idle_backoff()
                    _prompt()
                    continue
                last_user_at = time.monotonic()
                state.reset_idle_backoff()
                _, interrupt = _stream_with_interrupt(reply_pfx, handle.stream_user(line), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(_cycle_pause(handle, base_patience))
                _prompt()
                continue

            # Its own life waits while the operator is engaged or it chose to rest.
            backoff_remaining = state.idle_delay_remaining()
            if _idle_ready(state, handle, last_user_at):
                pending_line = _run_idle_cycle(state, handle, think_pfx, base_patience, interactive=interactive)
            else:
                pending_line = _cooldown(min(0.1, backoff_remaining) if backoff_remaining > 0 else 0.1)
    except KeyboardInterrupt:
        print('\n[interrupted]')
    finally:
        if interactive:
            handle.detach()
        if args.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
                print('\n[runtime sandbox cleaned]')
            except Exception as e:
                print(f'\n[sandbox cleanup failed: {e}]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
