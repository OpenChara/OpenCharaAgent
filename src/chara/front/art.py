"""OpenCharaAgent brand art: the compact block wordmark, a pale-blue→white gradient,
and a small "moonlight" sweep animation. Shown on every launch (roster/splash).

Cosmetic only. Kept restrained — this is a general runtime — but with a wordmark
+ a moth tagline so it reads as a roleplay tavern, not a dev tool. (The wide
serif wordmark was retired; the compact block art is the one true look.)

We render with rich `Text` (per-row styles) rather than markup strings, so the
ASCII art's stray brackets/backslashes can never be mis-parsed as markup tags.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rich.text import Text

_ASSETS = Path(__file__).resolve().parent.parent / "assets"

# Pale moonlit blue at the top fading to white at the bottom.
_TOP = (0x6D, 0xB3, 0xE0)
_BOTTOM = (0xFF, 0xFF, 0xFF)
_SWEEP = "#eafaff"   # the bright moonlight bar that crosses the wordmark
_DIM = "#5f7d8c"


@lru_cache(maxsize=4)
def _load(name: str) -> tuple[str, ...]:
    try:
        raw = (_ASSETS / name).read_text(encoding="utf-8").rstrip("\n")
        return tuple(raw.split("\n"))
    except OSError:
        return ("OpenCharaAgent",)


def _blend(t: float) -> str:
    r = round(_TOP[0] + (_BOTTOM[0] - _TOP[0]) * t)
    g = round(_TOP[1] + (_BOTTOM[1] - _TOP[1]) * t)
    b = round(_TOP[2] + (_BOTTOM[2] - _TOP[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _row_color(i: int, n: int) -> str:
    return _blend(i / max(1, n - 1))


def wordmark(sweep_x: int | None = None, sweep_w: int = 6) -> Text:
    """The wordmark as a rich Text, vertical blue→white gradient.

    sweep_x: center column of a brighter "moonlight" bar (None = static)."""
    rows = _load("wordmark_compact.txt")
    n = len(rows)
    t = Text(no_wrap=True)
    for i, row in enumerate(rows):
        if i:
            t.append("\n")
        base = _row_color(i, n)
        if sweep_x is None:
            t.append(row, style=base)
            continue
        lo, hi = max(0, sweep_x - sweep_w), max(0, sweep_x + sweep_w)
        before, bar, after = row[:lo], row[lo:hi], row[hi:]
        if before:
            t.append(before, style=base)
        if bar:
            t.append(bar, style=f"bold {_SWEEP}")
        if after:
            t.append(after, style=base)
    return t


def wordmark_width() -> int:
    return max((len(r) for r in _load("wordmark_compact.txt")), default=0)


def sweep_frames(step: int = 4, sweep_w: int = 6) -> list[Text]:
    """Frames for a one-shot moonlight sweep across the wordmark, then settle."""
    width = wordmark_width()
    frames = [wordmark(sweep_x=x, sweep_w=sweep_w) for x in range(-sweep_w, width + sweep_w, step)]
    frames.append(wordmark(sweep_x=None))  # settle to static gradient
    return frames


def tagline(text: str = "an agentic character tavern") -> Text:
    return Text(text, style=f"italic {_DIM}")
