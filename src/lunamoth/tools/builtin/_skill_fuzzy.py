"""Compact fuzzy find-and-replace for skill_manage(patch).

A faithful subset of hermes-agent ``tools/fuzzy_match.py``'s
``fuzzy_find_and_replace`` covering the strategies that matter for SKILL.md /
supporting-file patches: exact, line-trimmed, and whitespace-normalized. Returns
the SAME 4-tuple shape ``(new_content, match_count, strategy, error)`` and the
same uniqueness / not-found error strings, so skill_manage behaves identically.

Leading underscore → not discovered as a tool module (only modules with a
top-level ``registry.register`` call are imported by the registry's AST scan).

(The full nine-strategy engine — escape-drift, block-anchor, unicode-normalized,
context-aware — is the patch-search group's ``tools/fuzzy_match.py``; this is a
self-contained local copy so skills never imports another group's module at
registration time.)
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple


def _strategy_exact(content: str, pattern: str) -> List[Tuple[int, int]]:
    """All non-overlapping exact occurrences as (start, end) spans."""
    matches: List[Tuple[int, int]] = []
    start = 0
    while True:
        idx = content.find(pattern, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(pattern)))
        start = idx + len(pattern)
    return matches


def _line_spans(content: str) -> List[Tuple[int, int]]:
    """(start, end) byte spans of each line, end exclusive of the newline."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    for line in content.splitlines(keepends=True):
        stripped_len = len(line.rstrip("\n"))
        spans.append((pos, pos + stripped_len))
        pos += len(line)
    return spans


def _strategy_line_trimmed(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Match a run of lines whose trimmed text equals the pattern's trimmed lines."""
    pat_lines = [ln.strip() for ln in pattern.splitlines()]
    pat_lines = [ln for ln in pat_lines if ln != ""] or [p.strip() for p in pattern.splitlines()]
    if not pat_lines:
        return []
    raw_lines = content.splitlines()
    trimmed = [ln.strip() for ln in raw_lines]
    spans = _line_spans(content)
    matches: List[Tuple[int, int]] = []
    n = len(pat_lines)
    i = 0
    while i + n <= len(trimmed):
        if trimmed[i:i + n] == pat_lines:
            start = spans[i][0]
            end = spans[i + n - 1][1]
            matches.append((start, end))
            i += n
        else:
            i += 1
    return matches


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strategy_whitespace_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Match where collapsing internal whitespace makes content == pattern."""
    norm_pat = _normalize_ws(pattern)
    if not norm_pat:
        return []
    matches: List[Tuple[int, int]] = []
    # Slide a window across content; compare whitespace-normalized substrings of
    # the same word-token length as the pattern.
    for m in re.finditer(re.escape(norm_pat.split(" ")[0]), content):
        start = m.start()
        # Greedily extend until the normalized window matches, capped to keep it cheap.
        for end in range(start + len(norm_pat), min(len(content), start + len(norm_pat) * 3) + 1):
            if _normalize_ws(content[start:end]) == norm_pat:
                matches.append((start, end))
                break
    # De-overlap.
    deduped: List[Tuple[int, int]] = []
    last_end = -1
    for s, e in sorted(matches):
        if s >= last_end:
            deduped.append((s, e))
            last_end = e
    return deduped


def _apply(content: str, matches: List[Tuple[int, int]], new_string: str) -> str:
    out: List[str] = []
    prev = 0
    for start, end in sorted(matches):
        out.append(content[prev:start])
        out.append(new_string)
        prev = end
    out.append(content[prev:])
    return "".join(out)


def fuzzy_find_and_replace(
    content: str, old_string: str, new_string: str, replace_all: bool = False
) -> Tuple[str, int, Optional[str], Optional[str]]:
    """Find/replace via a chain of increasingly fuzzy strategies.

    Returns ``(new_content, match_count, strategy_name, error_message)``:
      - success → ``(modified, n, strategy, None)``
      - failure → ``(original, 0, None, error)``
    """
    if not old_string:
        return content, 0, None, "old_string cannot be empty"
    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    strategies: List[Tuple[str, Callable[[str, str], List[Tuple[int, int]]]]] = [
        ("exact", _strategy_exact),
        ("line_trimmed", _strategy_line_trimmed),
        ("whitespace_normalized", _strategy_whitespace_normalized),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)
        if matches:
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"Found {len(matches)} matches for old_string. "
                    f"Provide more context to make it unique, or use replace_all=True."
                )
            new_content = _apply(content, matches, new_string)
            return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def format_no_match_hint(error: Optional[str], match_count: int, old_string: str, content: str) -> str:
    """A short hint appended to a no-match error (hermes parity, abbreviated)."""
    if not error or "Could not find" not in error:
        return ""
    first_line = old_string.splitlines()[0].strip() if old_string.strip() else ""
    if first_line and first_line in content:
        return (
            "\nHint: the first line of old_string appears in the file, but the full "
            "block did not match — check trailing whitespace and surrounding context."
        )
    return "\nHint: re-read the file with skill_view and copy the exact text to patch."


__all__ = ["fuzzy_find_and_replace", "format_no_match_hint"]
