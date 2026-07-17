"""The launcher / roster — what `chara` opens to.

Hermes-style: stays in the normal terminal (scrollback preserved), never takes
over the screen. A blue OpenCharaAgent splash + a roster of your charas you navigate
with the arrow keys (↑/↓ to move, Enter to open). Only when you attach does the
full-screen TUI take over.

`run_launcher()` returns one of:
    ("attach", name) | ("new", None) | ("start_all", None) | ("stop", name) | None
Start/stop are handled in-place in interactive mode; the CLI handles the rest.
"""
from __future__ import annotations

import datetime as _dt
import sys
import time

from rich.console import Console, Group
from rich.text import Text

from . import art
from ..session import sessions as S

_STATUS = {
    "attached": ("◆", "#eafaff"),   # a live TUI is open
    "running": ("●", "#7fe0c0"),    # background daemon, thinking/creating
    "idle": ("○", "#6f8a99"),       # configured, not running
    "new": ("·", "#c8a86a"),        # never set up
}


def _ago(ts: float) -> str:
    if not ts:
        return "—"
    s = int((_dt.datetime.now() - _dt.datetime.fromtimestamp(ts)).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _row_text(meta: S.SessionMeta, selected: bool) -> Text:
    status = meta.status()
    glyph, color = _STATUS.get(status, ("·", "#888888"))
    t = Text()
    t.append("  ▸ " if selected else "    ", style="bold #9fd9ff")
    t.append(f"{glyph} ", style=color)
    name_style = "bold #eafaff" if selected else "bold #dfeefa"
    t.append(f"{meta.name:<16}", style=name_style)
    t.append(f"{meta.character_label():<22}", style="#9fd9ff")
    t.append(f"{status:<9}", style=color)
    t.append(f"{meta.isolation:<8}", style="#5f7d8c")
    t.append(_ago(meta.last_active or meta.created_at), style="#5f7d8c")
    if selected:
        t.stylize("on #0e2536")
    return t


def _hint(interactive: bool) -> Text:
    h = Text("  ")
    pairs = (
        [("↑↓", "move"), ("⏎", "open"), ("n", "new"), ("s", "start all"), ("x", "stop"), ("q", "quit")]
        if interactive
        else [("1-9", "open"), ("n", "new"), ("s", "start all"), ("x N", "stop"), ("q", "quit")]
    )
    for key, label in pairs:
        h.append(key + " ", style="#9fd9ff")
        h.append(label + "   ", style="#5f7d8c")
    return h


def _splash(console: Console, animate: bool) -> None:
    # The compact block wordmark is the one OpenCharaAgent look (the wide serif art was
    # retired). art.* no longer takes a `compact` flag.
    if animate and console.is_terminal:
        try:
            from rich.live import Live  # inline (screen=False) → stays in scrollback

            with Live(console=console, refresh_per_second=24, transient=False) as live:
                for frame in art.sweep_frames():
                    live.update(frame)
                    time.sleep(0.04)
        except Exception:
            console.print(art.wordmark())
    else:
        console.print(art.wordmark())
    console.print(art.tagline())
    console.print()


# ---- raw-mode single-key reader (unix) --------------------------------------

def _raw_supported() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return True
    except ImportError:
        return False


def _read_key() -> str:
    """Return 'up'/'down'/'enter'/'esc'/'ctrl-c' or a single character.

    Reads the raw fd with os.read (NOT sys.stdin.read): Python's text buffer
    would swallow the "[A" after an ESC, leaving select() to see an empty fd and
    mis-report every arrow key as a bare Esc — which made ↑/↓ quit the launcher.
    """
    import os
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":  # escape sequence (arrow keys) or a bare Esc
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                return "esc"
            seq = os.read(fd, 2)
            return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left"}.get(seq, "esc")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x03":
            return "ctrl-c"
        return ch.decode("utf-8", "ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _menu(rows: list[S.SessionMeta], sel: int, interactive: bool) -> Group:
    lines: list[Text] = []
    if not rows:
        lines.append(Text("    no chara yet — press  n  to summon one", style="#6f8a99"))
    else:
        for i, meta in enumerate(rows):
            lines.append(_row_text(meta, interactive and i == sel))
    lines.append(Text(""))
    lines.append(_hint(interactive))
    return Group(*lines)


def _interactive(console: Console):
    from . import cli  # lazy: cli imports roster, so import here not at module load
    from rich.live import Live

    sel = 0
    with Live(console=console, auto_refresh=False, transient=False) as live:
        while True:
            rows = S.list_sessions()
            sel = min(sel, len(rows) - 1) if rows else 0
            live.update(_menu(rows, sel, interactive=True))
            live.refresh()
            key = _read_key()
            if key in ("q", "esc", "ctrl-c"):
                return None
            if key == "n":
                return ("new", None)
            if not rows:
                continue
            if key == "up":
                sel = (sel - 1) % len(rows)
            elif key == "down":
                sel = (sel + 1) % len(rows)
            elif key == "enter":
                return ("attach", rows[sel].name)
            elif key.isdigit() and key != "0":
                i = int(key) - 1
                if i < len(rows):
                    return ("attach", rows[i].name)
            elif key == "s":  # start all idle, in place
                for m in rows:
                    if m.is_configured() and not m.daemon_pid() and not m.running_pid():
                        cli._start_daemon(m)
            elif key == "x":  # stop the selected, in place
                cli._stop_daemon(rows[sel])


def _line_mode(console: Console):
    """Fallback for non-tty / no-termios: numbered line input."""
    while True:
        rows = S.list_sessions()
        console.print(_menu(rows, -1, interactive=False))
        try:
            raw = console.input("  [#9fd9ff]choose ▸[/] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return None
        if raw in ("", "q", "quit", "exit"):
            return None
        if raw in ("n", "new"):
            return ("new", None)
        if raw in ("s", "start", "start-all", "start all"):
            return ("start_all", None)
        parts = raw.split()
        if parts and parts[0] in ("x", "stop") and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1]) - 1
            if 0 <= i < len(rows):
                return ("stop", rows[i].name)
        if raw.isdigit():
            i = int(raw) - 1
            if 0 <= i < len(rows):
                return ("attach", rows[i].name)
        console.print("  [#c8704a]?[/] [#6f8a99]type a number to open, or n / s / x N / q[/]\n")


def run_launcher(animate: bool = True):
    console = Console()
    console.print()
    _splash(console, animate)
    if _raw_supported():
        return _interactive(console)
    return _line_mode(console)
