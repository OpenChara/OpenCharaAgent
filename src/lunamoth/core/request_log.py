"""The faithful per-turn request log — sandbox/logs/requests.jsonl.

Debug instrumentation extracted from agent.py: the exact system + messages + tools
+ model sent on each turn, capped at the last _REQUEST_LOG_MAX_LINES lines (and
_REQUEST_LOG_MAX_BYTES bytes) and credential-redacted before write (the file is
bundled into the session export ZIP, so a secret in context must never land here
in cleartext). The ONE shape edit before write: inline base64 image data URIs are
replaced by a short placeholder — pixels ride the in-memory context only, never
this log (the same rule transcript._strip_inline_images applies to the durable
transcript). Best-effort — a logging failure NEVER raises into the turn
(no-fallback applies to model output, not to this side channel).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..config import SANDBOX_ROOT
from .redact import redact_sensitive_text

_REQUEST_LOG_MAX_LINES = 200
# Byte ceiling for the kept tail. A single record carries the WHOLE context sent
# that turn, so even with image bytes elided 200 lines can reach tens of MB on a
# long session — the byte cap bounds the file AND the trim's read (the trim never
# loads more than this into memory, so a huge log can't stall the turn thread).
_REQUEST_LOG_MAX_BYTES = 4 * 1024 * 1024
# How often (in appends) to run the trim sweep. The plain append is cheap; the
# re-read+rewrite is not, so we amortize it instead of paying it every turn. The
# file can briefly grow to _REQUEST_LOG_MAX_LINES + this before being trimmed
# back to the cap — still bounded, never the multi-MB-per-turn churn it was.
_REQUEST_LOG_TRIM_EVERY = 50
# In-memory append counter (one process = one chara, so a module global is fine).
_request_log_appends = 0


def _strip_inline_image_data(messages: list[dict]) -> list[dict]:
    """Copy of the messages with inline base64 image data URIs replaced by a short
    placeholder (transcript._strip_inline_images is the sibling rule for the
    durable transcript). The record stays faithful in SHAPE — the image_url part
    remains where it sat in the request — only the megabytes of pixels are elided,
    since re-logging them on EVERY turn while the image stays in context is pure
    disk churn. Never mutates the input."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: list = []
        changed = False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                iv = part.get("image_url")
                url = iv.get("url", "") if isinstance(iv, dict) else (iv if isinstance(iv, str) else "")
                if isinstance(url, str) and url.startswith("data:"):
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"data:[inline image elided — {len(url)} chars]"}})
                    changed = True
                    continue
            parts.append(part)
        if changed:
            copy = dict(msg)
            copy["content"] = parts
            out.append(copy)
        else:
            out.append(msg)
    return out


def _trim_request_log(path: Path) -> None:
    """Trim the request log back to the caps, writing atomically.

    Byte-aware and cheap: reads AT MOST _REQUEST_LOG_MAX_BYTES from the file's
    TAIL (never the whole file into memory on the turn thread — the old
    readlines() paid for every oversized line ever written), drops the partial
    first line of that window, keeps the newest _REQUEST_LOG_MAX_LINES complete
    lines, and os.replace()s them into place (atomic — a crash mid-write can
    never leave a half-written or truncated log). Skips the rewrite entirely
    when the file is already within both caps. Best-effort: any failure is
    swallowed by the caller."""
    size = path.stat().st_size
    over_bytes = size > _REQUEST_LOG_MAX_BYTES
    with path.open("rb") as fh:
        if over_bytes:
            fh.seek(size - _REQUEST_LOG_MAX_BYTES)
        blob = fh.read(_REQUEST_LOG_MAX_BYTES)
    if over_bytes:
        # The tail window almost certainly starts mid-line; drop the fragment.
        nl = blob.find(b"\n")
        blob = blob[nl + 1:] if nl >= 0 else b""
    lines = blob.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # trailing newline, not an empty record
    if not over_bytes and len(lines) <= _REQUEST_LOG_MAX_LINES:
        return  # within both caps — nothing to rewrite
    keep = lines[-_REQUEST_LOG_MAX_LINES:]
    if not keep:
        # The tail window held no complete line — a single record bigger than
        # the byte cap. Rewriting would EMPTY the log; keep the file as-is (the
        # next sweep, with newer normal records in the window, trims it back).
        return
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with tmp.open("wb") as fh:
            if keep:
                fh.write(b"\n".join(keep) + b"\n")
        os.replace(tmp, path)  # atomic swap into place
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _append_request_log(kind: str, system: list[str], messages: list[dict],
                        tools: list[str], model: str) -> None:
    """Append one faithful request record to SANDBOX_ROOT/logs/requests.jsonl.

    This is debug instrumentation — ALWAYS on but capped at the last
    _REQUEST_LOG_MAX_LINES lines. The serialized record is run through the
    credential redactor before write: this file is bundled into the session
    export ZIP, so a secret flowing through context must never land here in
    cleartext. Best-effort, exactly like the audit log: a logging failure must
    NEVER raise into the turn (no-fallback applies to the model output, not to
    this side channel)."""
    global _request_log_appends
    try:
        path = SANDBOX_ROOT / "logs" / "requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "system": list(system),
                "messages": _strip_inline_image_data(messages),
                "tools": list(tools),
                "model": model,
            },
            ensure_ascii=False,
        )
        # Redact known credential shapes before the record touches disk
        # (force=True: this is a hard safety boundary, never emit raw secrets).
        line = redact_sensitive_text(line, force=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # Amortized rotation: the per-turn append is cheap, but the re-read +
        # rewrite is not — only sweep every _REQUEST_LOG_TRIM_EVERY appends, and
        # even then only rewrite when actually over the cap (atomically).
        _request_log_appends += 1
        if _request_log_appends >= _REQUEST_LOG_TRIM_EVERY:
            _request_log_appends = 0
            _trim_request_log(path)
    except Exception:  # noqa: BLE001 - logging must never break a turn
        pass
