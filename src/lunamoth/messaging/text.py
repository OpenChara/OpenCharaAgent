from __future__ import annotations


_PREFERRED_BREAKS = frozenset("。！？.!?\n;；")


def split_text(text: str, max_length: int) -> list[str]:
    """Split outbound text for platform caps, preferring sentence breaks."""

    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if len(text) <= max_length:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_length, n)
        if end >= n:
            chunks.append(text[start:])
            break

        cut = end
        for i in range(end, start, -1):
            if text[i - 1] in _PREFERRED_BREAKS:
                cut = i
                break
        if cut <= start:
            cut = end
        chunks.append(text[start:cut])
        start = cut

    return [c for c in chunks if c]
