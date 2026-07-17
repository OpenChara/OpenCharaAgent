from __future__ import annotations


_PREFERRED_BREAKS = frozenset("。！？.!?\n;；")
_FENCE = "```"


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — what the platforms actually limit (hermes
    gateway/platforms/base.py utf16_len): astral chars (emoji, CJK Ext B) count 2."""

    return len(text) + sum(1 for c in text if ord(c) > 0xFFFF)


def _fenced_cut_map(text: str) -> list[int]:
    """For each CUT position i (a split between text[i-1] and text[i]), the
    opening index of the ``` fence that cut would tear apart, else -1.

    A cut at a fence's own opening index (send the fence whole in the next
    chunk) or right after its closing line (fence complete in this chunk) is
    fine; anything strictly between splits the block. An unclosed fence runs
    to the end of the text. Length is len(text)+1 — cut positions, not chars.
    """
    n = len(text)
    inside = [-1] * (n + 1)
    pos = 0
    open_at = -1
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(_FENCE):
            if open_at < 0:
                open_at = pos
            else:
                close_end = pos + len(line)
                for i in range(open_at + 1, close_end):
                    inside[i] = open_at
                open_at = -1
        pos += len(line)
    if open_at >= 0:
        for i in range(open_at + 1, n + 1):
            inside[i] = open_at
    return inside


def split_text(text: str, max_length: int) -> list[str]:
    """Split outbound text for platform caps, preferring sentence breaks.

    ``max_length`` is counted in UTF-16 code units (Telegram/Discord/Slack all
    limit in UTF-16, not Python chars — an astral-heavy chunk counted in chars
    used to still 400 visibly on send). Cutting inside a fenced ``` block is
    avoided when reasonably possible: prefer a break outside any fence, else
    cut just before the fence opens; a block bigger than the cap still has to
    be cut, at its best inner break.
    """

    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if not text:
        return []
    if utf16_len(text) <= max_length:
        return [text]

    fence_at = _fenced_cut_map(text)
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        # Widest end with utf16_len(text[start:end]) <= max_length.
        width = 0
        end = start
        while end < n:
            width += 2 if ord(text[end]) > 0xFFFF else 1
            if width > max_length:
                break
            end += 1
        if end == start:
            end = start + 1  # a single astral char over a cap of 1: must progress
        if end >= n:
            chunks.append(text[start:])
            break

        cut = 0
        for i in range(end, start, -1):
            if text[i - 1] in _PREFERRED_BREAKS and fence_at[i] < 0:
                cut = i
                break
        if not cut and fence_at[end] > start:
            cut = fence_at[end]  # cut before the fence rather than tearing it
        if not cut:
            for i in range(end, start, -1):
                if text[i - 1] in _PREFERRED_BREAKS:
                    cut = i
                    break
        if cut <= start:
            cut = end
        chunks.append(text[start:cut])
        start = cut

    return [c for c in chunks if c]
