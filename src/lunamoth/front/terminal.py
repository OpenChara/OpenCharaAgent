from __future__ import annotations

import argparse
import select
import sys
import time
from dataclasses import dataclass

from ..content.themes import LUNAMOTH_BANNER
from ..presence import normalize_mode
from ..protocol import Notice, TextDelta, ToolEnd
from ..protocol.api import CharaHandle
from ..session.cleanup import clean_runtime_sandbox

BANNER = (
    LUNAMOTH_BANNER
    + "\nLUNAMOTH // LOCAL AGENTIC CHARACTER RUNTIME"
    + "\nHuman input interrupts the display stream. /help for commands. Ctrl-C to quit.\n"
)

STDIN_ACTIVE = True


@dataclass
class TerminalState:
    running: bool = True
    eternal: bool = True


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


_GRANT_WORDS = {"y", "yes", "allow", "ok", "同意", "允许", "是"}


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
            granted = line in _GRANT_WORDS
            print(f"  → {'granted' if granted else 'denied'}", flush=True)
            return granted
        time.sleep(0.05)
    print("\n  → denied (no answer)", flush=True)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='LunaMoth plain terminal mode (legacy; the TUI is the default)')
    parser.add_argument('--mode', choices=['live', 'chat'], default='',
                        help="interaction mode override (default: the chara's persisted setting)")
    parser.add_argument('--no-think', action='store_true', help=argparse.SUPPRESS)  # pre-rename alias for --mode chat
    parser.add_argument('--patience', '--cooldown', dest='patience', type=float, default=0.5,
                        help='pause between spontaneous cycles, in seconds')
    parser.add_argument('--no-stream', action='store_true', help='use non-streaming fallback output')
    parser.add_argument('--clean-on-exit', action='store_true', help='wipe the session sandbox on shutdown (default: persist)')
    parser.add_argument('--no-clean-on-exit', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--debug', action='store_true', help='DEBUG-level diagnostics in sandbox/logs/')
    args = parser.parse_args(argv)
    if args.debug:
        import os

        os.environ['LUNAMOTH_DEBUG'] = '1'  # picked up by setup_logging in the agent

    patience = float(args.patience)
    # Presence: interactive terminal = operator attached; detached daemon
    # (stdin is /dev/null, started by `lunamoth start`) = operator away. The
    # handle does the presence/handoff bookkeeping on attach.
    interactive = sys.stdin.isatty()
    handle = CharaHandle()
    if interactive:
        handle.set_permission_hook(_stdin_permission_hook)
    info = handle.attach(present=interactive)
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
        if info.greeting and info.first_meeting:
            # SillyTavern first_mes: the card's designed opener for a first meeting.
            print(f"{reply_pfx}{info.greeting}", flush=True)
            handle.record_greeting(info.greeting)
        elif info.attach_text:
            # The card's on_attach prompt: a live arrival turn.
            _stream_with_interrupt(reply_pfx, handle.stream_event(info.attach_text), allow_interrupt=False)
        elif info.greeting and not restored:
            print(f"{reply_pfx}{info.greeting}", flush=True)
            handle.record_greeting(info.greeting)
        elif not restored:
            probe = "你是谁？只用一句话回答。" if info.lang == "zh" else "Who are you? Answer in one sentence."
            _stream_with_interrupt(reply_pfx, handle.stream_user(probe), allow_interrupt=False)
    pending_line: str | None = None
    if interactive:
        _prompt()
        # Attach grace (live mode): leave the operator room for the first word; if
        # they type during the grace this captures it as the first pending line.
        if state.eternal:
            pending_line = _cooldown(max(30.0, 2 * patience))

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
                        patience = max(0.0, float(stripped.split(maxsplit=1)[1]))
                        print(f'patience = {patience}s')
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
                    _prompt()
                    continue
                _, interrupt = _stream_with_interrupt(reply_pfx, handle.stream_user(line), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(patience)
                _prompt()
                continue

            if state.eternal:
                print("\n\x1b[2m· idle ·\x1b[0m", flush=True)
                _, interrupt = _stream_with_interrupt(think_pfx, handle.stream_idle(), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(patience)
                if interactive:
                    _prompt()
            else:
                pending_line = _cooldown(0.1)
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
