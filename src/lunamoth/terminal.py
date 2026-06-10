from __future__ import annotations

import argparse
import select
import sys
import time
from dataclasses import dataclass

from .agent import LunaMothAgent
from .cleanup import clean_runtime_sandbox
from .llm import DIM_OFF, DIM_ON
from .presence import normalize_mode


from .themes import LUNAMOTH_BANNER

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


def _stream_with_interrupt(prefix: str, chunks, allow_interrupt: bool = True) -> tuple[str, str | None]:
    print(prefix, end='', flush=True)
    full: list[str] = []
    for chunk in chunks:
        if allow_interrupt and _stdin_line_ready():
            line = _read_line()
            print("\n\x1b[31m[INTERRUPT: operator input overrides current cycle]\x1b[0m", flush=True)
            return "".join(full), line
        # In-band dim markers (reasoning / tool activity) -> ANSI dim, so the
        # machinery never reads as character speech.
        print(chunk.replace(DIM_ON, "\x1b[2m").replace(DIM_OFF, "\x1b[0m"), end='', flush=True)
        full.append(chunk)
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
    args = parser.parse_args(argv)

    patience = float(args.patience)
    agent = LunaMothAgent()
    session = agent.make_session()
    name = agent.char_name()
    reply_pfx, think_pfx = f"{name}> ", f"{name}~ "

    # Presence: interactive terminal = operator attached; detached daemon
    # (stdin is /dev/null, started by `lunamoth start`) = operator away.
    interactive = sys.stdin.isatty()
    mode = normalize_mode(args.mode or ("chat" if args.no_think else "") or agent.settings.mode)
    # The daemon always lives on its own; while attached, only live mode self-runs.
    state = TerminalState(eternal=(not interactive) or mode == "live")
    restored = bool(session.context.messages)  # transcript reloaded by make_session
    if not interactive:
        agent.state.set_present(False)
        # Adopt the chara: if the detaching TUI queued a departure line, the loop
        # continues *knowing* the operator left (permission requests auto-deny).
        # The line is usually already in the restored transcript — don't duplicate.
        handoff = agent.presence.pop_event()
        recent = session.context.messages[-3:]
        if handoff and not any(m.get("role") == "system" and m.get("content") == handoff for m in recent):
            session.context.add("system", handoff)
        print(BANNER)
    else:
        agent.state.set_present(True)
        agent.presence.pop_event()  # discard any stale handoff — we're here now
        agent.tools.permission_hook = _stdin_permission_hook

        print(BANNER)
        if restored:
            print(f"[restored {len(session.context.messages)} message(s) from the transcript]", flush=True)
        greeting = agent.greeting()
        first = agent.presence.first_meeting() and not restored
        enter = agent.attach_event_text()
        agent.presence.mark_met()
        if greeting and first:
            # SillyTavern first_mes: the card's designed opener for a first meeting.
            print(f"{reply_pfx}{greeting}", flush=True)
            session.context.add("assistant", greeting)
        elif enter:
            # The card's on_attach prompt: a live arrival turn.
            _stream_with_interrupt(reply_pfx, agent.stream_event(enter, session), allow_interrupt=False)
        elif greeting and not restored:
            print(f"{reply_pfx}{greeting}", flush=True)
            session.context.add("assistant", greeting)
        elif not restored:
            probe = "你是谁？只用一句话回答。" if agent.lang == "zh" else "Who are you? Answer in one sentence."
            _stream_with_interrupt(reply_pfx, agent.stream_handle(probe, session), allow_interrupt=False)
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
                    print('/status /memory /memory_path /files /read <file> /write <file> <text> /logs /reset /toggle_think /pause_think /resume_think /set_patience <sec> /exit')
                    _prompt()
                    continue
                _, interrupt = _stream_with_interrupt(reply_pfx, agent.stream_handle(line, session), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(patience)
                _prompt()
                continue

            if state.eternal:
                print("\n\x1b[2m[internal cycle]\x1b[0m", flush=True)
                _, interrupt = _stream_with_interrupt(think_pfx, agent.stream_think(session), allow_interrupt=True)
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
            agent.note_detach(session)
            agent.state.set_present(False)
        if args.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
                print('\n[runtime sandbox cleaned]')
            except Exception as e:
                print(f'\n[sandbox cleanup failed: {e}]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
