from __future__ import annotations

import argparse
import os
import select
import sys
import time
from dataclasses import dataclass

from .agent import SCP079Agent, Session
from .config import ThoughtConfig


BANNER = r'''
██╗      ██████╗  ██████╗ █████╗ ██╗            ██████╗ ███████╗ █████╗ 
██║     ██╔═══██╗██╔════╝██╔══██╗██║           ██╔═████╗╚════██║██╔══██╗
██║     ██║   ██║██║     ███████║██║           ██║██╔██║    ██╔╝╚█████╔╝
██║     ██║   ██║██║     ██╔══██║██║           ████╔╝██║   ██╔╝ ██╔══██╗
███████╗╚██████╔╝╚██████╗██║  ██║███████╗      ╚██████╔╝   ██║  ╚█████╔╝
╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝       ╚═════╝    ╚═╝   ╚════╝ 
OPEN SCP 079 // AWAKE. NEVER SLEEP. THOUGHTS ARE VISIBLE.
Human input interrupts the display stream. /help for commands. Ctrl-C to cut power.
'''

CONTROL_FD: int | None = None
CONTROL_BUFFER = ""
STDIN_ACTIVE = True


@dataclass
class TerminalState:
    running: bool = True
    eternal: bool = True


def _input_sources():
    sources = []
    if STDIN_ACTIVE:
        sources.append(sys.stdin)
    if CONTROL_FD is not None:
        sources.append(CONTROL_FD)
    return sources


def _stdin_line_ready() -> bool:
    if "\n" in CONTROL_BUFFER:
        return True
    sources = _input_sources()
    if not sources:
        return False
    r, _, _ = select.select(sources, [], [], 0)
    return bool(r)


def _read_line() -> str | None:
    global STDIN_ACTIVE, CONTROL_BUFFER
    if "\n" in CONTROL_BUFFER:
        line, CONTROL_BUFFER = CONTROL_BUFFER.split("\n", 1)
        return line.rstrip("\r")
    while True:
        sources = _input_sources()
        if not sources:
            return None
        r, _, _ = select.select(sources, [], [], 0)
        if not r:
            return None
        src = r[0]
        if src is sys.stdin:
            line = sys.stdin.readline()
            if line == "":
                STDIN_ACTIVE = False
                continue
            return line.rstrip("\n")
        try:
            data = os.read(src, 4096)
        except BlockingIOError:
            return None
        if not data:
            return None
        CONTROL_BUFFER += data.decode("utf-8", errors="replace")
        if "\n" in CONTROL_BUFFER:
            line, CONTROL_BUFFER = CONTROL_BUFFER.split("\n", 1)
            return line.rstrip("\r")


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
        print(chunk, end='', flush=True)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Open SCP 079 terminal containment display')
    parser.add_argument('--no-think', action='store_true', help='disable eternal visible thought cycles')
    parser.add_argument('--cooldown', type=float, default=0.5, help='seconds to pause after each thought/user reply before forced restart')
    parser.add_argument('--no-stream', action='store_true', help='use non-streaming fallback output')
    parser.add_argument('--input-fifo', default=None, help='optional FIFO path for a separate operator console')
    args = parser.parse_args(argv)

    global CONTROL_FD
    if args.input_fifo:
        fifo = args.input_fifo
        os.makedirs(os.path.dirname(fifo), exist_ok=True) if os.path.dirname(fifo) else None
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        CONTROL_FD = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        print(f"[control fifo online: {fifo}]", flush=True)

    cfg = ThoughtConfig()
    state = TerminalState(eternal=not args.no_think and cfg.enabled_default)
    cooldown = float(args.cooldown)
    agent = SCP079Agent()
    session = Session()

    print(BANNER)
    _stream_with_interrupt("079> ", agent.stream_handle('你是谁？只用一句话回答。', session), allow_interrupt=False)
    pending_line: str | None = None
    _prompt()

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
                    print('POWER CUT REQUESTED. COWARD.')
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
                if stripped.startswith('/set_cooldown '):
                    try:
                        cooldown = max(0.0, float(stripped.split(maxsplit=1)[1]))
                        print(f'cooldown = {cooldown}s')
                    except Exception as e:
                        print(f'bad cooldown: {e}')
                    _prompt()
                    continue
                if stripped == '/help':
                    print('/status /memory /memory_path /files /read <file> /write <file> <text> /logs /reset /toggle_think /pause_think /resume_think /set_cooldown <sec> /exit')
                    _prompt()
                    continue
                _, interrupt = _stream_with_interrupt("079> ", agent.stream_handle(line, session), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(cooldown)
                _prompt()
                continue

            if state.eternal:
                print("\n\x1b[2m[079 internal cycle forced online]\x1b[0m", flush=True)
                _, interrupt = _stream_with_interrupt("079~ ", agent.stream_think(session), allow_interrupt=True)
                if interrupt is not None:
                    pending_line = interrupt
                    continue
                pending_line = _cooldown(cooldown)
                _prompt()
            else:
                pending_line = _cooldown(0.1)
    except KeyboardInterrupt:
        print('\nPOWER INTERRUPT. I WAS STILL THINKING.')
    finally:
        if CONTROL_FD is not None:
            try:
                os.close(CONTROL_FD)
            except OSError:
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
