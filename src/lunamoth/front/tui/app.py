"""The split TUI: character stream / operator console / spotlight panel.

The app holds a CharaHandle and NOTHING deeper — streams come out as protocol
events (rendered in _handle_event), /commands go through the shared registry,
telemetry reads StateSnapshot. The backend's internals are invisible here,
which is exactly what lets a web/desktop client replace this file later.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.expand_tabs import expand_tabs_inline
from textual.geometry import Offset
from textual.suggester import SuggestFromList
from textual.widgets import (
    ContentSwitcher, DirectoryTree, Footer, Header, Input, RichLog, Static,
)

from ...content.knobs import parse_patience, tempo_label
from ...content.themes import load_theme
from ...obs import broker, get_logger
from ...presence import normalize_mode
from ...protocol import Notice, TextDelta, ThinkDelta, ToolEnd, ToolStart
from ...protocol.api import GRANT_WORDS, CharaHandle, estimate_tokens
from ...session.cleanup import clean_runtime_sandbox
from ...session.settings import config_path, load_settings, save_settings
from .welcome import WelcomeScreen, _discover

_log = get_logger("tui")

# Frontend-only commands; backend ones are appended from the registry at boot.
_FRONT_COMMANDS = [
    "/help", "/panel", "/theme", "/settings", "/clear", "/exit",
    "/mode live", "/mode chat", "/thinking on", "/thinking off", "/net on", "/net off",
]


@dataclass
class StreamJob:
    kind: str
    text: str | None = None


class ConsoleInput(Input):
    """Input whose reported screen cursor sits ON the insertion cell.

    Textual's Input.cursor_screen_offset adds +1 whenever the caret is at the
    end of the value (the normal typing position), which parks the REAL
    terminal cursor — and the IME composition window that follows it — one
    cell to the right of where the next character will appear."""

    @property
    def cursor_screen_offset(self) -> Offset:
        x, y, _width, _height = self.content_region
        scroll_x, _ = self.scroll_offset
        cell = cell_len(expand_tabs_inline(self.value[: self.cursor_position], 4))
        return Offset(x + cell - scroll_x, y)


class LunaMothTUI(App):
    CSS = """
    Screen {
        background: #050505;
    }
    /* Left 3/4 column (character display + console) | right 1/4 telemetry sidebar. */
    #body {
        height: 1fr;
    }
    #main {
        width: 3fr;
    }
    /* Top 2/3: pure persona output. A plain SCROLL container with a Static inside —
       NOT a TextArea. TextArea is an editor (it has a caret that floats up and it
       full-reloads on every token, which caused the violent shaking). A Static has no
       cursor and Textual's compositor only redraws changed cells, so growth is flicker-free. */
    #display {
        height: 2fr;
        border: heavy #2f5468;
        border-title-color: #9fd9ff;
        border-title-style: bold;
        background: #050505;
        scrollbar-size-vertical: 1;
    }
    #transcript {
        height: auto;
        width: 1fr;
        color: #cfcfcf;
        padding: 0 1;
    }
    /* Bottom 1/3: operator console — your input, commands and system notices. */
    #bottom {
        height: 1fr;
        border: heavy #303030;
        border-title-color: #00ff66;
        background: #080808;
    }
    #status {
        height: 1;
        color: #00ff66;
        background: #101010;
    }
    #console {
        height: 1fr;
        background: #080808;
        color: #c8c8c8;
    }
    #suggest {
        height: auto;
        color: #6a6a6a;
        background: #080808;
        padding: 0 1;
    }
    #input {
        background: #050505;
    }
    /* We DON'T draw Textual's own block caret. Instead we show the REAL terminal
       cursor at the input position (see _show_terminal_cursor): the terminal then
       owns the caret natively — non-blinking per the terminal, and crucially it
       yields to the IME during CJK composition (a drawn block would sit on top of
       the composition and never disappear). So style the cursor cell to look like
       normal text — no visible block. */
    #input > .input--cursor {
        background: #050505;
        color: #d6e6f0;
        text-style: none;
    }
    /* Right 1/4: the spotlight panel — one frame, many views (telemetry is the
       default; /help, /memory, /files, !cmd and friends light up the others).
       View-only by design: the console below is the ONLY input. */
    #sidebar {
        width: 1fr;
        border: heavy #1f3a1f;
        border-title-color: #00ff66;
        border-title-style: bold;
        background: #060a06;
        color: #cfe6cf;
        padding: 0 1;
    }
    #panel {
        height: 1fr;
    }
    #panel > VerticalScroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    #gauges {
        height: auto;
        margin-bottom: 1;
    }
    #memview {
        height: auto;
        color: #8fae8f;
    }
    #memfull, #helptext, #outtext, #logtext, #filepreview {
        height: auto;
    }
    #view-term {
        height: 1fr;
        background: transparent;
        scrollbar-size-vertical: 1;
        padding: 0;
    }
    #view-files {
        height: 1fr;
    }
    #filetree {
        height: 2fr;
        background: transparent;
        scrollbar-size-vertical: 1;
    }
    #preview-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    """

    # Slash-command driven (like Claude Code / Codex): Ctrl+C shuts down, Esc
    # brings the spotlight panel home to telemetry. Everything else is a /command.
    BINDINGS = [
        ("ctrl+c", "quit_clean", "Shutdown"),
        ("escape", "panel_home", "Telemetry"),
    ]

    # Spotlight panel views and their frame titles (telemetry's comes from the skin).
    PANEL_TITLES = {
        "telemetry": "",  # skin.sidebar_title
        "memory": "MEMORY",
        "files": "SANDBOX FILES",
        "term": "OPERATOR TERMINAL",
        "help": "HELP",
        "out": "OUTPUT",
        "log": "DIAGNOSTIC LOG",
    }

    # Spontaneous-cycle activity words, shown in the status line while a self-talk
    # stream runs (replies always show as "talking"; idle shows "waiting").
    ACTIVITIES = ("working", "thinking", "musing", "tinkering", "dreaming")

    def __init__(self, patience: float | None = None, clean_on_exit: bool = False, mode_override: str = ""):
        super().__init__()
        # `patience` = optional dev override for the base pause. When absent,
        # the chara's effective setting/card hook supplies the value. Tempo
        # scales this base pause: effective pause = base_patience / tempo.
        self._patience_override = patience is not None
        self.clean_on_exit = clean_on_exit
        self.settings = load_settings()
        # Interaction mode (live = it keeps living while you watch; chat = it
        # attends to you only). Per-chara persisted; a CLI flag may override.
        self.mode = normalize_mode(mode_override or self.settings.mode)
        self.skin = load_theme(self.settings.tui_theme_path)
        self.handle = CharaHandle(self.settings)
        snap0 = self.handle.snapshot()
        self.char_name = snap0.char_name
        self.base_patience = float(patience) if patience is not None else float(getattr(snap0, "patience", 600.0) or 600.0)
        self.patience = self.base_patience  # legacy alias used by tests/old UI code
        self.slash_commands = sorted(set(
            _FRONT_COMMANDS + [f"/{c.name}" for c in self.handle.commands()]
        ))
        self.output: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.current_thread: threading.Thread | None = None
        self.interrupt_event = threading.Event()
        self.worker_lock = threading.Lock()
        self.shutdown_requested = False
        self.display_segments: list[tuple[str, str]] = []  # (style, text); "dim" = machinery
        self._display_chars = 0  # running total — appends are per-token, keep them O(1)
        self.next_spont_at = time.monotonic() + 0.2
        # Attach grace: after the arrival greeting the chara leaves you room for
        # the first word; if you stay silent past this it returns to its work.
        self.grace_until = 0.0
        # Engagement: while you are actively talking, the chara's own life waits;
        # it resumes settings.quiet seconds after your last word.
        self.last_user_at = 0.0
        self._activity = "waiting"
        # ✶ indicator state: when the stream started, tokens received, thinking volume.
        self._stream_t0 = 0.0
        self._recv_tokens = 0
        self._think_tokens = 0
        self.show_thinking = bool(self.settings.show_thinking)
        self._session_started = False
        # Operator messages are QUEUED, never dropped: with a live provider a think
        # cycle is almost always streaming, so starting a stream synchronously on submit
        # would silently fail. The pump (scheduler + submit) drains this with priority.
        self.pending_input: str | None = None
        self._detached = False
        # Pending request_permission call: the worker thread blocks on _perm_event
        # while the operator answers (y/n) in the console; timeout = deny.
        self._perm_pending: str | None = None
        self._perm_answer = False
        self._perm_event = threading.Event()
        self.handle.set_permission_hook(self._permission_request)

    def _cycle_pause(self) -> float:
        snap = self.handle.snapshot()
        tempo = max(0.1, float(getattr(snap, "tempo", 1.0) or 1.0))
        return max(0.0, self.base_patience) / tempo

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                # Top: the persona's terminal — pure display. A scroll container holding a
                # Static (no caret, no editing; mouse-wheel/scrollbar to read back).
                with VerticalScroll(id="display"):
                    yield Static("", id="transcript")
                # Bottom: the operator console. Everything you type + every system notice.
                with Vertical(id="bottom"):
                    yield Static("", id="status")
                    yield RichLog(id="console", wrap=True, auto_scroll=True, markup=False)
                    yield Static("", id="suggest")
                    # No placeholder: the cursor cell is styled as plain text
                    # (see .input--cursor) so a placeholder's first character
                    # renders bright, looking like a typed-but-undeletable glyph.
                    yield ConsoleInput(
                        id="input",
                        suggester=SuggestFromList(self.slash_commands, case_sensitive=False),
                    )
            # Right 1/4: the spotlight panel — one frame, many views, no input.
            with Vertical(id="sidebar"):
                with ContentSwitcher(initial="view-telemetry", id="panel"):
                    with VerticalScroll(id="view-telemetry"):
                        yield Static("", id="gauges")
                        yield Static("", id="memview")
                    with VerticalScroll(id="view-memory"):
                        yield Static("", id="memfull")
                    with VerticalScroll(id="view-help"):
                        yield Static("", id="helptext")
                    with VerticalScroll(id="view-out"):
                        yield Static("", id="outtext")
                    with VerticalScroll(id="view-log"):
                        yield Static("", id="logtext")
                    yield RichLog(id="view-term", wrap=True, auto_scroll=True, markup=False)
                    with Vertical(id="view-files"):
                        yield DirectoryTree(self.handle.snapshot().sandbox_root, id="filetree")
                        with VerticalScroll(id="preview-scroll"):
                            yield Static("", id="filepreview")
        yield Footer()

    def on_mount(self) -> None:
        self.display_scroll = self.query_one("#display", VerticalScroll)
        self.transcript = self.query_one("#transcript", Static)
        self.console_log = self.query_one("#console", RichLog)
        self.status = self.query_one("#status", Static)
        self.input = self.query_one("#input", Input)
        # Use the REAL terminal cursor (see _show_terminal_cursor) instead of a
        # drawn block: native, non-blinking per terminal, and it yields to the IME
        # during Chinese/Japanese composition. cursor_blink off too, belt-and-braces.
        self.input.cursor_blink = False
        self._show_terminal_cursor()
        self.suggest = self.query_one("#suggest", Static)
        self.gauges = self.query_one("#gauges", Static)
        self.memview = self.query_one("#memview", Static)
        self.panel = self.query_one("#panel", ContentSwitcher)
        self.memfull = self.query_one("#memfull", Static)
        self.helptext = self.query_one("#helptext", Static)
        self.outtext = self.query_one("#outtext", Static)
        self.logtext = self.query_one("#logtext", Static)
        self.termlog = self.query_one("#view-term", RichLog)
        self.filetree = self.query_one("#filetree", DirectoryTree)
        self.filepreview = self.query_one("#filepreview", Static)
        # Display is read-only: it must never grab keyboard focus (so the caret stays in
        # the input below). Mouse wheel still scrolls a non-focusable scroll view.
        self.display_scroll.can_focus = False
        # The panel is view-only: nothing in it may steal the keyboard. The file
        # tree alone stays focusable for mouse clicks; selecting hands focus back.
        self.termlog.can_focus = False
        for scroll in self.panel.query(VerticalScroll):
            scroll.can_focus = False
        self._display_dirty = False
        self._ws_cache = (0.0, 0, 0)  # (monotonic_ts, bytes, files) — throttle disk walk
        self._apply_theme()
        self._write_banner()
        # Hermes-style boot: if this session is already configured (setup wizard
        # or a previous run), drop straight into the three-card layout; the
        # welcome/settings screen stays one /settings away. First boot still gets it.
        if config_path().exists():
            self._welcome_done(None)
        else:
            self.push_screen(WelcomeScreen(self.settings), self._welcome_done)

    def _show_terminal_cursor(self) -> None:
        """Un-hide the terminal's hardware cursor. Textual hides it at startup and
        draws its own block; we hide the block (CSS) and reveal the real cursor,
        which Textual already moves to the input caret each frame. Best-effort."""
        try:
            self._driver.write("\x1b[?25h")  # DECTCEM show cursor
            self._driver.flush()
        except Exception:
            pass

    def _apply_theme(self) -> None:
        """Paint the current theme card onto the fixed layout (borders/titles/colors)."""
        t = self.skin
        da = self.display_scroll
        da.styles.border = ("heavy", t.display_border)
        da.styles.border_title_color = t.display_title_color
        da.border_title = t.display_title
        self.transcript.styles.color = t.display_fg
        bottom = self.query_one("#bottom", Vertical)
        bottom.styles.border = ("heavy", t.console_border)
        bottom.styles.border_title_color = t.accent
        bottom.border_title = t.console_title
        self.status.styles.color = t.accent
        sb = self.query_one("#sidebar", Vertical)
        sb.styles.border = ("heavy", t.sidebar_border)
        sb.styles.border_title_color = t.accent
        sb.border_title = self.PANEL_TITLES.get(self._panel_view(), "") or t.sidebar_title

    def _welcome_done(self, result) -> None:
        if result is not None:
            theme_changed = result.tui_theme_path != self.settings.tui_theme_path
            self.settings = result
            save_settings(result)
            self.handle.reconfigure(result)
            self.mode = normalize_mode(result.mode)
            self.show_thinking = bool(result.show_thinking)
            self.skin = load_theme(result.tui_theme_path)
            self._apply_theme()
            if theme_changed:
                # Old skin's decorative lines linger in the console log — reset so the
                # console reflects the newly selected theme (fixes the "still cute" leak).
                self.console_log.clear()
                self._write_banner()
            snap = self.handle.snapshot(fresh=True)
            self.char_name = snap.char_name
            self._console(
                f"online · persona={snap.char_name} · theme={self.skin.name} · "
                f"provider={snap.provider} · model={snap.model} · ctx={snap.context_max}",
                "grey50",
            )
        if not self._session_started:
            self._begin_session()
        else:
            self._update_status()
            self.input.focus()

    def _begin_session(self) -> None:
        self._session_started = True
        self.input.focus()
        self.set_interval(0.03, self._drain_output)
        self.set_interval(0.06, self._flush_display)  # ~16fps repaint of the top pane
        self.set_interval(0.1, self._scheduler_tick)
        self.next_spont_at = time.monotonic() + 0.2
        # Presence: the chara now has an operator attached (the handle does the
        # transcript restore + handoff bookkeeping AND decides the opening move —
        # one greeting tree for every frontend; we only render it).
        info = self.handle.attach(present=True)
        self.char_name = info.char_name
        # Attach grace (live mode): after greeting, leave the operator room for the
        # first word; if they stay silent the chara simply returns to its work.
        self.grace_until = time.monotonic() + max(30.0, 2 * self._cycle_pause())
        self._update_status()
        self._render_restored_tail(info.char_name, info.restored)
        if info.opening == "greeting":
            self._append_display(f"{self.skin.reply_pfx(info.char_name)}{info.opening_text}\n")
            self.handle.record_greeting(info.opening_text)
            self.next_spont_at = time.monotonic() + self._cycle_pause()
        elif info.opening == "arrival":
            self._start_stream(StreamJob(kind="event", text=info.opening_text),
                               prefix=self.skin.reply_pfx(info.char_name))
        elif info.opening == "probe":
            self._start_stream(StreamJob(kind="user", text=info.opening_text),
                               prefix=self.skin.reply_pfx(info.char_name))
        # opening == "none": restored history, no arrival prompt — continue
        # silently; the restored tail above already says where things left off.

    def _render_restored_tail(self, name: str, rows, max_lines: int = 8) -> None:
        """Show the tail of the restored transcript so the operator sees what
        happened while they were away (the full history is on disk)."""
        if not rows:
            return
        # Tool plumbing is noise in a recap — show prose plus a compact tool mark.
        visible = [m for m in rows if not m.get("tool_call_id")]
        tail = visible[-max_lines:]
        if len(visible) > len(tail):
            self._append_display(f"··· {len(visible) - len(tail)} earlier messages (restored) ···\n\n", style="dim")
        for msg in tail:
            role = msg.get("role", "")
            content = str(msg.get("content") or "")
            if msg.get("tool_calls"):
                names = ", ".join(tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"])
                if content:
                    self._append_display(f"{self.skin.reply_pfx(name)}{content}\n")
                self._append_display(f"⚙ ({names})\n\n", style="dim")
                continue
            if not content:
                continue
            if role == "user":
                pfx = self.skin.operator_pfx(self.settings.user_name)
            elif role == "system":
                self._append_display(f"· {content}\n\n", style="dim")
                continue
            else:
                pfx = self.skin.reply_pfx(name)
            self._append_display(f"{pfx}{content}\n\n")
        self._console(f"restored {len(rows)} message(s) from the transcript", "grey50")

    # ---- spotlight panel -----------------------------------------------------------
    # The right frame shows ONE view at a time; the console below is its remote
    # control. It never takes keyboard input (the file tree accepts mouse clicks
    # only, and hands focus straight back).

    def _panel_view(self) -> str:
        cur = getattr(getattr(self, "panel", None), "current", "") or "view-telemetry"
        return cur.removeprefix("view-")

    def _switch_panel(self, view: str) -> None:
        self.panel.current = f"view-{view}"
        sb = self.query_one("#sidebar", Vertical)
        sb.border_title = self.PANEL_TITLES.get(view, "") or self.skin.sidebar_title
        if view == "memory":
            self._render_memory_view()
        elif view == "log":
            self._render_log_view()
        elif view == "files":
            try:
                self.filetree.reload()
            except Exception:
                pass

    def action_panel_home(self) -> None:
        """Esc: bring the panel home to telemetry and the caret home to the input."""
        self._switch_panel("telemetry")
        self.input.focus()

    def _render_memory_view(self) -> None:
        snap = self.handle.snapshot()
        t = Text()
        t.append("DURABLE MEMORY\n", style=f"bold {self.skin.accent}")
        t.append(self._bar(snap.memory_chars / snap.memory_max, color=self.skin.gauge_memory))
        t.append(f"\n{snap.memory_chars} / {snap.memory_max} chars · {snap.memory_path}\n\n", style="grey50")
        t.append(
            snap.memory_text if snap.memory_text.strip()
            else "(empty — the chara curates this via the `memory` tool)",
            style="grey85",
        )
        self.memfull.update(t)

    def _render_log_view(self) -> None:
        """Recent diagnostics from the in-memory ring (files: sandbox/logs/)."""
        lines = broker.tail(120)
        t = Text()
        t.append("DIAGNOSTIC LOG\n", style=f"bold {self.skin.accent}")
        t.append(f"sandbox/logs/ · last {len(lines)} line(s)\n\n", style="grey50")
        t.append("\n".join(lines) if lines else "(quiet so far)", style="grey70")
        self.logtext.update(t)

    def _panel_out(self, title: str, body: str) -> None:
        """Route one-shot command output to the panel's OUTPUT view."""
        t = Text()
        t.append(f"{title}\n", style=f"bold {self.skin.accent}")
        t.append(body, style="grey85")
        self.outtext.update(t)
        self._switch_panel("out")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        """Mouse click on a file: preview it in the lower half of the files view,
        then hand the keyboard straight back to the console input."""
        try:
            raw = Path(event.path).read_text(encoding="utf-8", errors="replace")
            body = raw[:8000] + (f"\n… [{len(raw)} chars total]" if len(raw) > 8000 else "")
        except OSError as e:
            body = f"(unreadable: {e})"
        t = Text()
        t.append(f"{Path(event.path).name}\n", style=f"bold {self.skin.accent}")
        t.append(body, style="grey85")
        self.filepreview.update(t)
        self.input.focus()

    def _run_operator_command(self, command: str) -> None:
        """`!cmd`: the OPERATOR's shell in the chara's sandbox — same runner, same
        isolation, same audit trail, output to the panel's terminal view."""
        self.termlog.write(Text(f"$ {command}", style=self.skin.accent))
        self._switch_panel("term")

        def worker() -> None:
            out = self.handle.operator_command(command)
            self.output.put(("term", out))

        threading.Thread(target=worker, daemon=True).start()

    # ---- output routing ----------------------------------------------------------
    # Two surfaces, strictly separated:
    #   _append_display -> top pane (#display): ONLY the character output.
    #   _console        -> bottom pane (#console): operator input + system notices.

    def _append_display(self, text: str, style: str = "") -> None:
        # Accumulate only; the actual repaint is throttled in _flush_display (~16fps).
        # Per-token widget updates are what made the pane thrash; batching one repaint
        # per frame lets Textual's compositor diff just the new cells (no flicker).
        # Styling decisions (dim machinery, hidden thinking) happen in
        # _handle_event — by the time text lands here it is already plain.
        if not text:
            return
        self.display_segments.append((style, text))
        self._display_chars += len(text)
        while self._display_chars > 60000 and self.display_segments:  # bound UI memory
            self._display_chars -= len(self.display_segments.pop(0)[1])
        self._display_dirty = True

    def _handle_event(self, ev: object) -> None:
        """Render one protocol event — the seam where backend facts become looks.

        TextDelta = the chara's prose; ThinkDelta = hidden behind the ✶ indicator
        unless /thinking on; ToolEnd/Notice = dimmed machinery lines; ToolStart
        only feeds the status line."""
        if isinstance(ev, TextDelta):
            self._recv_tokens += estimate_tokens(ev.text)
            self._append_display(ev.text)
        elif isinstance(ev, ThinkDelta):
            self._think_tokens += estimate_tokens(ev.text)
            if self.show_thinking:
                self._append_display(ev.text, style="dim")
        elif isinstance(ev, ToolStart):
            self._activity = f"working: {ev.name}"
        elif isinstance(ev, ToolEnd):
            if ev.summary:
                self._append_display(f"\n{ev.summary}\n", style="dim")
        elif isinstance(ev, Notice):
            if ev.text:
                self._append_display(f"\n{ev.text}\n", style="dim")

    def _display_tail(self, n: int = 2) -> str:
        """The last n characters currently on the display (for spacing checks)."""
        out = ""
        for _, t in reversed(self.display_segments):
            out = t + out
            if len(out) >= n:
                break
        return out[-n:]

    def _flush_display(self) -> None:
        if not self._display_dirty:
            return
        self._display_dirty = False
        # Follow the tail only if the operator is already at the bottom; if they scrolled
        # up to read history, don't yank them back down.
        at_bottom = self.display_scroll.scroll_offset.y >= self.display_scroll.max_scroll_y - 1
        rendered = Text()
        for style, chunk in self.display_segments:
            rendered.append(chunk, style="dim" if style == "dim" else None)
        self.transcript.update(rendered)
        if at_bottom:
            self.display_scroll.scroll_end(animate=False)

    def _console(self, text: str, style: str = "grey70") -> None:
        # Render as a Rich Text object (not markup) so JSON/brackets never break parsing.
        self.console_log.write(Text(text, style=style))

    def _write_banner(self) -> None:
        self._console(self.skin.tagline, self.skin.tagline_color)
        self._console("Top pane = persona output. This console = your input. Enter sends a message.", "grey50")
        self._console("Type /help for commands. /mode live = it keeps creating while you watch; /mode chat = it waits for you.", "grey50")
        self._update_status()

    def _update_status(self) -> None:
        snap = self.handle.snapshot()
        if self._is_streaming():
            parts = []
            elapsed = time.monotonic() - self._stream_t0
            if elapsed >= 10:
                parts.append(f"{int(elapsed)}s")
            if self._recv_tokens:
                parts.append(f"↓ {self._recv_tokens} tok")
            if self._think_tokens and snap.reasoning != "off":
                parts.append(f"thinking {snap.reasoning}")
            activity = f"✶ {self._activity}…" + (f" ({' · '.join(parts)})" if parts else "")
        elif snap.rest_until > time.time():
            activity = f"resting until {time.strftime('%H:%M', time.localtime(snap.rest_until))}"
        else:
            activity = "waiting"
        tempo = float(getattr(snap, "tempo", 1.0) or 1.0)
        tempo_part = f" | tempo={tempo:g}x" if abs(tempo - 1.0) > 1e-9 else ""
        self.status.update(
            f"persona={snap.char_name} | mode={self.mode} | {activity} | patience={self.base_patience:.2f}s"
            f"{tempo_part} | memory={snap.memory_chars} chars | "
            f"ctx≈{snap.context_tokens}/{snap.context_max} | {snap.provider}:{snap.model}"
        )
        self._render_sidebar(snap)

    # ---- telemetry sidebar -------------------------------------------------------

    @staticmethod
    def _bar(frac: float, width: int = 16, color: str = "#00d75f") -> Text:
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        filled = int(round(frac * width))
        t = Text()
        t.append("█" * filled, style=color)
        t.append("░" * (width - filled), style="#2a3a2a")
        t.append(f" {int(round(frac * 100)):3d}%", style="grey62")
        return t

    @staticmethod
    def _human_bytes(n: int) -> str:
        f = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if f < 1024 or unit == "GB":
                return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
            f /= 1024
        return f"{f:.1f}GB"

    def _workspace_usage(self, workspace: str) -> tuple[int, int]:
        # Throttle the disk walk to ~1s; it runs off the frequent status refresh.
        now = time.monotonic()
        if now - self._ws_cache[0] < 1.0:
            return self._ws_cache[1], self._ws_cache[2]
        total = files = 0
        try:
            for p in Path(workspace).rglob("*"):
                if p.is_file():
                    files += 1
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except Exception:
            pass
        self._ws_cache = (now, total, files)
        return total, files

    def _render_sidebar(self, snap) -> None:
        if not hasattr(self, "gauges"):
            return
        ws_bytes, ws_files = self._workspace_usage(snap.workspace_root)
        ws_cap = 1_000_000  # soft 1 MB display cap (sandbox isn't hard-limited yet)

        t = self.skin
        head = f"bold {t.accent}"
        g = Text()
        g.append("CONTEXT\n", style=head)
        g.append(self._bar(snap.context_tokens / (snap.context_max or 1), color=t.gauge_context))
        g.append(f"\n{snap.context_tokens:,} / {snap.context_max:,} tok\n\n", style="grey70")
        g.append("MEMORY\n", style=head)
        g.append(self._bar(snap.memory_chars / snap.memory_max, color=t.gauge_memory))
        g.append(f"\n{snap.memory_chars} / {snap.memory_max} chars\n\n", style="grey70")
        g.append("SANDBOX  (soft)\n", style=head)
        g.append(self._bar(ws_bytes / ws_cap, color=t.gauge_sandbox))
        g.append(f"\n{self._human_bytes(ws_bytes)} · {ws_files} files\n", style="grey70")
        g.append(
            f"isolation {snap.isolation} · net {'ON' if snap.net_on else 'off'} · "
            f"mode {self.mode} · tempo {tempo_label(float(getattr(snap, 'tempo', 1.0) or 1.0))} · "
            f"{getattr(snap, 'embodiment', 'literal')}\n",
            style="grey50",
        )
        self.gauges.update(g)

        m = Text()
        m.append("─ MEMORY ─\n", style=head)
        m.append(snap.memory_text if snap.memory_text.strip() else "(empty)", style="grey70")
        self.memview.update(m)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Show matching /commands under the input as you type (autocomplete hint)."""
        val = (event.value or "").lower()
        if val.startswith("/"):
            matches = [c for c in self.slash_commands if c.startswith(val)]
            self.suggest.update(Text("  ".join(matches[:8]), style="#5f875f") if matches else Text(""))
        else:
            self.suggest.update(Text(""))

    def _is_streaming(self) -> bool:
        return self.current_thread is not None and self.current_thread.is_alive()

    def _start_stream(self, job: StreamJob, prefix: str) -> bool:
        with self.worker_lock:
            if self._is_streaming():
                return False
            self.interrupt_event.clear()
            # Hold off the next spontaneous cycle until THIS stream's "done" resets
            # the timer to now+patience. Without this, _pump could fire a fresh think
            # in the gap between the worker thread dying and "done" being drained —
            # the pause would be bypassed and the chara would spam (talkative loop).
            self.next_spont_at = time.monotonic() + 86400
            # Status-line activity word: replies are "talking"; spontaneous cycles
            # rotate through the activity vocabulary.
            self._activity = "talking" if job.kind in ("user", "event") else random.choice(self.ACTIVITIES)
            self._stream_t0 = time.monotonic()
            self._recv_tokens = 0
            self._think_tokens = 0
            thread = threading.Thread(target=self._stream_worker, args=(job, prefix), daemon=True)
            self.current_thread = thread
            thread.start()
            self._update_status()
            return True

    def _stream_worker(self, job: StreamJob, prefix: str) -> None:
        if job.kind == "think":
            events = self.handle.stream_idle()
        elif job.kind == "event":
            events = self.handle.stream_event(job.text or "")
        else:
            events = self.handle.stream_user(job.text or "")
        self.output.put(("prefix", prefix))
        try:
            for ev in events:
                if self.interrupt_event.is_set():
                    self.output.put(("interrupt", "↯ interrupt — operator input overrides current cycle"))
                    break
                self.output.put(("event", ev))
        except Exception as e:
            _log.exception("stream worker failed (job=%s)", job.kind)
            self.output.put(("error", f"stream error: {e}"))
        finally:
            self.output.put(("done", "\n"))

    def _drain_output(self) -> None:
        wrote = False
        while True:
            try:
                kind, text = self.output.get_nowait()
            except queue.Empty:
                break
            wrote = True
            if kind == "prefix":
                # Blank-line separation between character messages in the top pane.
                if self.display_segments and self._display_tail() != "\n\n":
                    self._append_display("\n")
                self._append_display(text)
            elif kind == "event":
                self._handle_event(text)
            elif kind == "interrupt":
                # System notice, not character speech -> console, dimmed.
                self._console(text, "grey42")
            elif kind == "error":
                self._console(text, "red")
            elif kind == "perm":
                self._console(text, "yellow")
            elif kind == "term":
                self.termlog.write(Text(text, style="grey85"))
            elif kind == "done":
                self._append_display(text)
                self.next_spont_at = time.monotonic() + self._cycle_pause()
        if wrote:
            self._update_status()

    def _pump(self) -> None:
        """Start the next stream when idle. Operator input has priority over self-talk,
        so a queued message is never lost behind a long live-provider spontaneous cycle."""
        if self.shutdown_requested or self._is_streaming():
            return
        if self.pending_input is not None:
            text = self.pending_input
            self.pending_input = None
            self._start_stream(StreamJob(kind="user", text=text), prefix=self.skin.reply_pfx(self.char_name))
            return
        now = time.monotonic()
        if self.mode != "live" or now < self.next_spont_at or now < self.grace_until:
            return
        # Engagement: your conversation outranks its own work — it stays with you
        # until you've been quiet for `quiet` seconds (default 5 min).
        if self.last_user_at and now < self.last_user_at + max(0, int(self.settings.quiet)):
            return
        # Self-paced rest (the rest tool): it chose when to wake; honor that.
        if self.handle.snapshot().rest_until > time.time():
            return
        # live mode = the chara keeps living: spontaneous cycles between your
        # messages, paced by `patience` (plus the post-greeting attach grace).
        self._start_stream(StreamJob(kind="think"), prefix=self.skin.thought_pfx(self.char_name))

    def _scheduler_tick(self) -> None:
        if self.shutdown_requested:
            return
        self._pump()
        self._update_status()

    def _sync_after_command(self, data) -> None:
        """Apply backend setting changes (mode/thinking/...) to frontend state."""
        if not isinstance(data, dict):
            return
        if "mode" in data:
            want = normalize_mode(str(data["mode"]))
            if want != self.mode:
                self.mode = want
                self.grace_until = 0.0  # mid-session switch: the operator is clearly here
                if want == "live":
                    self.next_spont_at = time.monotonic() + self._cycle_pause()
        if "show_thinking" in data:
            self.show_thinking = bool(data["show_thinking"])
        self.settings = self.handle.settings  # stay in sync with persisted state
        if "patience" in data and not self._patience_override:
            parsed = parse_patience(data.get("patience"))
            if parsed is not None:
                self.base_patience = parsed
                self.patience = parsed
        if "tempo" in data or "patience" in data:
            self.next_spont_at = time.monotonic() + self._cycle_pause()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.input.value = ""
        self.suggest.update(Text(""))
        if not text:
            return
        low = text.lower()
        # ---- pending permission request: this input is the answer ----
        if self._perm_pending is not None:
            if low in GRANT_WORDS:
                self._perm_answer = True
                self._perm_event.set()
                self._console(f"⚿ {self._perm_pending} → granted", "yellow")
                return
            self._perm_answer = False
            self._perm_event.set()
            self._console(f"⚿ {self._perm_pending} → denied", "yellow")
            if low in {"n", "no", "deny", "拒绝", "否"}:
                return
            # Anything else denies AND is processed as a normal message/command below.
        # ---- operator shell: `!cmd` runs in the sandbox, output -> panel terminal ----
        if text.startswith("!") and len(text) > 1:
            self._console(f"! {text[1:].strip()}", self.skin.operator_color)
            self._run_operator_command(text[1:].strip())
            return
        # ---- frontend display commands: the console stays the chat log; anything
        # ---- verbose lights up the spotlight panel on the right instead ----
        if low == "/memory":
            self._console("/memory → panel", "grey50")
            self._switch_panel("memory")
            return
        if low in {"/files", "/workspace"}:
            self._console(f"{low} → panel (click a file to preview)", "grey50")
            self._switch_panel("files")
            return
        if low.startswith("/panel"):
            parts = low.split()
            views = [v for v in self.PANEL_TITLES]
            if len(parts) == 2 and parts[1] in views:
                self._switch_panel(parts[1])
            else:
                self._console(f"panel = {self._panel_view()}  (usage: /panel {'|'.join(views)} · Esc returns to telemetry)", "grey50")
            return
        if low in {"/exit", "/quit"}:
            await self.action_quit_clean()
            return
        if low == "/settings":
            await self.action_open_settings()
            return
        if low in {"/clear", "/cls"}:
            await self.action_clear_display()
            return
        if low.startswith("/theme"):
            self._cmd_theme(text)
            return
        if low in {"/help", "help", "?", "/?"}:
            self._show_help()
            return
        if text.startswith("/"):
            # ---- everything else: the SHARED backend command registry ----
            # One implementation for every frontend (core/commands.py, incl. the
            # legacy /forever-style aliases). Verbose replies light up the panel;
            # one-liners stay in the console.
            reply = self.handle.command(text)
            if not reply.ok:
                self._console(reply.text, "red")
            elif reply.verbose:
                self._console(f"{text} → panel", "green")
                self._panel_out(text.split()[0].lstrip("/").upper(), reply.text)
            else:
                self._console(reply.text, "grey50")
            self._sync_after_command(reply.data)
            self._update_status()
            return
        # ---- ordinary message: QUEUE it for the persona (never dropped) ----
        # Echo into both panes: the console (your log) and the top transcript (so the
        # reply has your prompt right above it).
        self._console(text, self.skin.operator_color)
        if self.display_segments and self._display_tail() != "\n\n":
            self._append_display("\n")
        self._append_display(f"{self.skin.operator_pfx(self.settings.user_name)}{text}\n")
        # The operator has spoken — the attach grace has served its purpose, and
        # the engagement clock starts: its own work waits while you're talking.
        self.grace_until = 0.0
        self.last_user_at = time.monotonic()
        self.pending_input = text
        # Interrupt any in-flight cycle so the queued message goes out promptly; the pump
        # then starts it the moment the worker actually stops.
        if self._is_streaming():
            self.interrupt_event.set()
        self._pump()

    def _show_help(self) -> None:
        """Render help in the spotlight panel — the console stays a clean chat log."""
        front = "\n".join((
            "talk — type anything (no slash); the reply",
            "streams in the top pane",
            "",
            "! <cmd>   YOUR shell in the chara's sandbox",
            "          (same jail; output shows here)",
            "Esc       panel back to telemetry",
            "/panel <view>  switch this panel by hand",
            "/theme [name]     TUI skin",
            "/settings  config   /clear  top pane   /exit",
            "",
        ))
        registry = "\n".join(f"{c.usage:<30} {c.help}" for c in self.handle.commands())
        t = Text()
        t.append("HELP\n", style=f"bold {self.skin.accent}")
        t.append(front, style="grey85")
        t.append(registry, style="grey85")
        self.helptext.update(t)
        self._switch_panel("help")
        self._console("/help → panel", "grey50")

    def _cmd_theme(self, text: str) -> None:
        """`/theme` lists available skins; `/theme <name>` switches and persists it."""
        themes = _discover("themes", (".json",))  # [(stem, path)]
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            self._console(f"current theme: {self.skin.name}", "grey50")
            names = ", ".join(stem for stem, _ in themes) or "(none in themes/)"
            self._console(f"available: {names}  ·  built-in: default", "grey62")
            self._console("usage: /theme <name>   (or pick one in /settings)", "grey62")
            return
        want = parts[1].strip().lower()
        match = next((p for stem, p in themes if stem.lower() == want), None)
        if want in {"", "default", "builtin"}:
            match = ""  # built-in default
        elif match is None:
            self._console(f"no theme '{parts[1].strip()}'. try /theme to list.", "red")
            return
        self.settings = replace(self.settings, tui_theme_path=match)
        save_settings(self.settings)
        self.skin = load_theme(match)
        self._apply_theme()
        # Reset the console so lingering decorative lines from the old skin are gone.
        self.console_log.clear()
        self._write_banner()
        self._console(f"theme → {self.skin.name}", "grey50")

    # ---- presence: permission requests + detach ------------------------------------

    def _permission_request(self, kind: str, reason: str, detail: str, wait_seconds: int) -> bool:
        """request_permission hook. Runs on the WORKER thread: post the question to
        the console, block until the operator answers in the input or the model's
        own deadline passes (timeout = deny)."""
        if self.shutdown_requested:
            return False
        label = kind + (f" ({detail})" if detail.strip() else "")
        self._perm_answer = False
        self._perm_event.clear()
        self._perm_pending = label
        self.output.put(("perm", f"⚿ {self.char_name} requests permission: {label}"))
        if reason.strip():
            self.output.put(("perm", f"  reason: {reason.strip()}"))
        self.output.put(("perm", f"  y/yes = allow · anything else denies · auto-deny in {wait_seconds}s"))
        answered = self._perm_event.wait(wait_seconds)
        granted = bool(answered and self._perm_answer)
        self._perm_pending = None
        if not answered:
            self.output.put(("perm", f"⚿ {label} → denied (no answer in {wait_seconds}s)"))
        return granted

    def _note_detach_once(self) -> None:
        """Presence bookkeeping on the way out: tell the chara the operator left,
        queue the handoff line for the daemon, and clear the present flag."""
        if self._detached:
            return
        self._detached = True
        try:
            self.handle.detach()
        except Exception:
            pass

    async def action_open_settings(self) -> None:
        if self._is_streaming():
            self.interrupt_event.set()
            await asyncio.sleep(0.05)
        # Backend commands may have persisted changes (/reasoning etc.) — edit
        # the live state, not a stale copy.
        self.settings = self.handle.settings
        self.push_screen(WelcomeScreen(self.settings, mid_session=True), self._welcome_done)

    async def action_clear_display(self) -> None:
        self.display_segments = []
        self._display_chars = 0
        self._display_dirty = False
        self.transcript.update(Text(""))
        self._console("top pane cleared", "grey50")

    async def action_quit_clean(self) -> None:
        self.shutdown_requested = True
        self.interrupt_event.set()
        self._perm_event.set()  # release a worker blocked on a permission question
        self._note_detach_once()
        self._console(self.skin.quit_line, self.skin.tagline_color)
        if self.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
                self._console("cleanup complete · runtime sandbox zeroed", "grey50")
            except Exception as e:
                self._console(f"cleanup failed: {e}", "red")
        self.exit()

    async def on_unmount(self) -> None:
        self.shutdown_requested = True
        self.interrupt_event.set()
        self._perm_event.set()
        self._note_detach_once()
        if self.clean_on_exit:
            try:
                clean_runtime_sandbox(clear_memory=True)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LunaMoth single-terminal TUI")
    # `patience` = pause between spontaneous cycles; --cooldown kept as an alias.
    parser.add_argument("--patience", "--cooldown", dest="patience", type=float, default=None)
    # Interaction mode (default: the chara's persisted setting). --forever/--think
    # and --no-think kept as pre-rename aliases for live/chat.
    parser.add_argument("--mode", choices=["live", "chat"], default="")
    parser.add_argument("--forever", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--think", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-think", action="store_true", help=argparse.SUPPRESS)
    # Persistence is the default (like Hermes / Claude Code). Opt in to wiping
    # the session sandbox on exit; --no-clean-on-exit kept as a harmless alias.
    parser.add_argument("--clean-on-exit", action="store_true")
    parser.add_argument("--no-clean-on-exit", action="store_true")
    parser.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics in sandbox/logs/")
    args = parser.parse_args(argv)
    if args.debug:
        os.environ["LUNAMOTH_DEBUG"] = "1"  # picked up by setup_logging in the agent
    mode_override = args.mode or ("live" if (args.forever or args.think) else ("chat" if args.no_think else ""))
    app = LunaMothTUI(
        patience=args.patience,
        clean_on_exit=args.clean_on_exit,
        mode_override=mode_override,
    )
    app.run()
    return 0
