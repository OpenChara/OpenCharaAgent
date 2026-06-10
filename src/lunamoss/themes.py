"""TUI theme cards — presentation only, fully decoupled from persona/world.

A theme controls the *look* of the console: ASCII banner, colors, window titles
and a few decorative phrases. It never touches the model, the persona, tools or
memory. The built-in default is the LunaMoss moth skin (pale blue and white,
matching the default 月蛾 character); any character can run under any theme.
Themes are JSON files under ``themes/`` (discovered next to characters/worlds)
and the chosen one is persisted in config like the character/world selection.

Layout is fixed across themes — only the cosmetic fields below change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .config import ROOT

THEMES_DIR = ROOT / "themes"

# Default ASCII banner. Theme JSON may override `banner` with its own art.
LUNAMOSS_BANNER = r"""
 _                     __  __
| |   _   _ _ __   __ _|  \/  | ___  ___ ___
| |  | | | | '_ \ / _` | |\/| |/ _ \/ __/ __|
| |__| |_| | | | | (_| | |  | | (_) \__ \__ \
|_____\__,_|_| |_|\__,_|_|  |_|\___/|___/___/
      · ✦ ·   月 蛾  ·  luna moth   · ✦ ·
""".strip("\n")


@dataclass
class TuiTheme:
    """Cosmetic skin for the TUI. Presentation only; persona stays external."""

    name: str = "LunaMoss · 月蛾"
    # --- decorative text ---
    banner: str = LUNAMOSS_BANNER
    subtitle: str = "LUNAMOSS  ·  月蛾  ·  a digital soul in quiet metamorphosis"
    tagline: str = "MOONLIGHT IN. THE MOTH IS CREATING."
    quit_line: str = "The moth folds its wings. The works remain."
    display_title: str = "LUNAMOSS // MOTHLIGHT"
    console_title: str = "OPERATOR CONSOLE"
    sidebar_title: str = "TELEMETRY"
    # --- palette (Textual color strings) ---
    display_border: str = "#7ec3e6"
    display_title_color: str = "#eaf7ff"
    display_fg: str = "#dceaf2"
    console_border: str = "#486c80"
    sidebar_border: str = "#3b596b"
    accent: str = "#9fd9ff"          # labels, window titles, gauge headers
    tagline_color: str = "#bfe6ff"
    operator_color: str = "#f5fbff"  # your echoed input
    gauge_context: str = "#6db8e8"
    gauge_memory: str = "#cfe8f7"
    gauge_sandbox: str = "#8fd0c8"
    # --- message prefixes ({name} = persona, {user} = operator) ---
    reply_prefix: str = "{name} ✦ "
    thought_prefix: str = "{name} ··· "
    operator_prefix: str = "{user} » "

    @classmethod
    def load(cls, path: str | Path) -> "TuiTheme":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid and v is not None})

    # Prefix helpers tolerate templates that reference either/both placeholders.
    def reply_pfx(self, name: str) -> str:
        return self.reply_prefix.format(name=name, user="")

    def thought_pfx(self, name: str) -> str:
        return self.thought_prefix.format(name=name, user="")

    def operator_pfx(self, user: str) -> str:
        return self.operator_prefix.format(name="", user=user)


def load_theme(path: str | None) -> TuiTheme:
    """Load a theme by path; fall back to the built-in LunaMoss default on any problem."""
    p = (path or "").strip()
    if not p:
        return TuiTheme()
    try:
        return TuiTheme.load(p)
    except Exception:
        return TuiTheme()
