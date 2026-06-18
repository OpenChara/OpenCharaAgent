"""Outbound media-marker extraction — the ``MEDIA:<path>`` convention, ported
apple-to-apple from hermes ``gateway/platforms/base.py`` and adapted for
LunaMoth's sandbox-relative paths.

Like hermes, a chara surfaces a file NOT through a tool but by writing a marker
in its reply; each rendering/delivery surface extracts what it can show and
leaves the rest as visible text. This module is the ONE place the parsing rules
live (the analog of hermes's ``BasePlatformAdapter`` extractors); the web side
ports the same rules in TypeScript.

Ported verbatim from hermes (the maturity is in the edge cases):
  * ``MEDIA_DELIVERY_EXTS`` — the deliverable-extension source of truth.
  * the three marker forms: ``MEDIA:<path>`` tags, markdown ``![alt](url)``
    image URLs, and bare local file paths.
  * the code-block / inline-code / blockquote / JSON-string masking, so an
    example marker in prose or a stored marker in a serialized tool result is
    never delivered.
  * strip-the-marker-from-visible-text (the tag is a directive, not prose).

PURE: stdlib only, no filesystem and no path resolution. A chara emits a
WORKSPACE-RELATIVE path (``works/sketch.png``, ``assets/art.png``) because it
lives in a jail, where hermes uses absolute paths — so the path branch also
accepts relative paths and the caller resolves them against the sandbox
(boundary + existence enforced there). ``extract_local_files`` takes an
``exists`` predicate the caller backs with a sandbox-aware check, mirroring
hermes's inline ``os.path.isfile`` gate without reaching the filesystem here.
"""
from __future__ import annotations

import re
from collections.abc import Callable

# --- the deliverable-extension set (verbatim from hermes base.py:1185) -------------
MEDIA_DELIVERY_EXTS: tuple[str, ...] = (
    # Images (embed inline)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    # Video (embed inline where supported)
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    # Audio (delivered as voice/audio where supported)
    ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac",
    # Documents (uploaded as file attachments)
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
    # Spreadsheets / data
    ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    # Presentations
    ".pptx", ".ppt", ".odp", ".key",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
    # Web / rendered output
    ".html", ".htm",
)

# Image extensions a rich surface embeds inline (vs offered as a download).
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
)

# Don't push more than ~8MB of a single file to the foreground (mirrors the
# retired send_file cap). Enforced where a path is resolved against the sandbox.
MAX_MEDIA_BYTES = 8 * 1024 * 1024

# Bare-extension alternation, sorted longest-first so a short ext never matches
# as a prefix of a longer one (hermes base.py:1204).
_MEDIA_EXT_ALTERNATION = "|".join(
    sorted((e.lstrip(".") for e in MEDIA_DELIVERY_EXTS), key=len, reverse=True)
)

# The MEDIA:<path> matcher. Path forms (in order): backtick/double/single-quoted,
# then an ABSOLUTE/home/Windows-drive path (hermes's original branch, spaces
# allowed), then — LunaMoth's addition — a WORKSPACE-RELATIVE path (no leading
# slash; e.g. ``works/sketch.png``). All must end in a deliverable extension; a
# trailing-boundary lookahead keeps surrounding punctuation out of the path.
MEDIA_TAG_RE = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/|[A-Za-z]:[/\\])\S+(?:[^\S\n]+\S+)*?\.(?:''' + _MEDIA_EXT_ALTERNATION + r''')|'''
    r'''[\w.\-]+(?:[/\\][\w.\-]+)*\.(?:''' + _MEDIA_EXT_ALTERNATION + r'''))'''
    r'''(?=[\s`"',;:)\]}]|$)[`"']?''',
    re.IGNORECASE,
)


def _mask_protected_spans(content: str) -> str:
    """Replace content inside fenced code blocks, inline code spans, and
    blockquotes with spaces so MEDIA: examples in prose are never delivered.
    Offset-preserving (chars -> spaces, newlines kept). Ported verbatim from
    hermes base.py:2814; skips backtick-quoted MEDIA: path quotes."""
    chars = list(content)
    spans: list[tuple[int, int]] = []
    # Fenced code blocks: ```...```
    for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
        spans.append((m.start(), m.end()))
    # Inline code: `...` but NOT a backtick-quoted path right after MEDIA:
    for m in re.finditer(r'`[^`\n]+`', content):
        start = m.start()
        prefix = content[max(0, start - 20):start]
        if re.search(r'MEDIA:\s*$', prefix):
            continue
        spans.append((start, m.end()))
    # Blockquote lines: > at line start
    for m in re.finditer(r'^>.*$', content, re.MULTILINE):
        spans.append((m.start(), m.end()))
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != '\n':
                chars[i] = ' '
    return ''.join(chars)


def _mask_json_string_media(content: str) -> str:
    """Blank out ``MEDIA:<bare-path>`` sitting inside a JSON string *value* (a
    stored marker in a serialized tool result), so it is never re-delivered.
    Offset-preserving. Ported verbatim from hermes base.py:2855."""
    if '"' not in content or "MEDIA:" not in content:
        return content
    chars = list(content)
    for m in re.finditer(r'(?<=[:,{\[])\s*"((?:[^"\\\n]|\\.)*)"', content):
        seg = m.group(1)
        if re.search(r'MEDIA:\s*(?:~/|/|[A-Za-z]:[/\\])', seg):
            for i in range(m.start(1), m.end(1)):
                if chars[i] != '\n':
                    chars[i] = ' '
    return ''.join(chars)


def _strip_quotes(path: str) -> str:
    """Unwrap a matched path the way hermes does: drop a symmetric surrounding
    quote pair, then trailing/leading quote/punctuation residue."""
    path = path.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    return path.lstrip("`\"'").rstrip("`\"',.;:)}]")


def extract_media(content: str) -> tuple[list[str], str]:
    """Find ``MEDIA:<path>`` markers and return ``(raw_paths, cleaned_text)``.

    Mirrors hermes ``extract_media``: mask protected spans only as a *locator*,
    collect the raw (un-expanded, un-resolved) paths, then delete exactly those
    marker spans from the UNMASKED text so protected spans survive verbatim.
    Resolution against the sandbox is the caller's job."""
    paths: list[str] = []
    scan = _mask_json_string_media(_mask_protected_spans(content))
    for m in MEDIA_TAG_RE.finditer(scan):
        p = _strip_quotes(m.group("path"))
        if p:
            paths.append(p)
    cleaned = content
    if paths:
        masked = _mask_json_string_media(_mask_protected_spans(cleaned))
        spans = [m.span() for m in MEDIA_TAG_RE.finditer(masked)]
        if spans:
            chars = list(cleaned)
            for start, end in sorted(spans, reverse=True):
                del chars[start:end]
            cleaned = re.sub(r'\n{3,}', '\n\n', "".join(chars)).strip()
    return paths, cleaned


# Markdown / HTML image-URL extraction (verbatim from hermes base.py:2630).
_MD_IMG_RE = r'!\[([^\]]*)\]\((https?://[^\s\)]+)\)'
_HTML_IMG_RE = r'<img\s+src=["\']?(https?://[^\s"\'<>]+)["\']?\s*/?>\s*(?:</img>)?'
_IMG_URL_MARKERS = ('.png', '.jpg', '.jpeg', '.gif', '.webp',
                    'fal.media', 'fal-cdn', 'replicate.delivery')


def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
    """Find remote image URLs in markdown ``![alt](url)`` / ``<img src>`` form,
    return ``([(url, alt)], cleaned_text)``. Only http(s) URLs that look like
    images qualify. Ported from hermes ``extract_images``."""
    images: list[tuple[str, str]] = []
    for m in re.finditer(_MD_IMG_RE, content):
        url = m.group(2)
        if any(url.lower().endswith(ext) or ext in url.lower() for ext in _IMG_URL_MARKERS):
            images.append((url, m.group(1)))
    for m in re.finditer(_HTML_IMG_RE, content):
        images.append((m.group(1), ""))
    cleaned = content
    if images:
        extracted = {url for url, _ in images}

        def _drop(match: re.Match) -> str:
            url = match.group(2) if (match.lastindex or 0) >= 2 else match.group(1)
            return '' if url in extracted else match.group(0)

        cleaned = re.sub(_MD_IMG_RE, _drop, cleaned)
        cleaned = re.sub(_HTML_IMG_RE, _drop, cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return images, cleaned


# Bare local file path (no MEDIA: prefix). Absolute/home/drive OR workspace-
# relative, ending in a deliverable extension. Adapted from hermes
# extract_local_files (base.py:3012) with a relative branch added.
_BARE_PATH_RE = re.compile(
    r'(?<![/:\w.])(?:~/|/|[A-Za-z]:[/\\]|(?=[\w.\-]+[/\\]))'
    r'(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:' + _MEDIA_EXT_ALTERNATION + r')\b',
    re.IGNORECASE,
)


def extract_local_files(content: str, exists: Callable[[str], bool]) -> tuple[list[str], str]:
    """Find bare file paths (no ``MEDIA:`` prefix) that actually exist, return
    ``(paths, cleaned_text)``. ``exists`` is a sandbox-aware predicate the caller
    supplies (the analog of hermes's inline ``os.path.isfile``); a path that does
    not resolve to a real readable file is left as prose. Code spans are skipped.
    Ported from hermes ``extract_local_files``; the relative branch is ours."""
    code_spans: list[tuple[int, int]] = []
    for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
        code_spans.append((m.start(), m.end()))
    for m in re.finditer(r'`[^`\n]+`', content):
        code_spans.append((m.start(), m.end()))

    def _in_code(pos: int) -> bool:
        return any(s <= pos < e for s, e in code_spans)

    found: list[str] = []
    seen: set[str] = set()
    for m in _BARE_PATH_RE.finditer(content):
        if _in_code(m.start()):
            continue
        raw = m.group(0)
        if raw in seen:
            continue
        if exists(raw):
            seen.add(raw)
            found.append(raw)
    cleaned = content
    if found:
        for raw in found:
            cleaned = cleaned.replace(raw, '')
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return found, cleaned


def is_image_path(path: str) -> bool:
    """True when *path*'s extension marks it as an inline-able image."""
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in IMAGE_EXTS
