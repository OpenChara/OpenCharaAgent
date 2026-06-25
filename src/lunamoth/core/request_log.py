"""The faithful per-turn request log — sandbox/logs/requests.jsonl.

Debug instrumentation extracted from agent.py: the exact system + messages + tools
+ model sent on each turn, capped at the last _REQUEST_LOG_MAX_LINES lines and
credential-redacted before write (the file is bundled into the session export ZIP,
so a secret in context must never land here in cleartext). Best-effort — a logging
failure NEVER raises into the turn (no-fallback applies to model output, not to this
side channel).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..config import SANDBOX_ROOT
from .redact import redact_sensitive_text

_REQUEST_LOG_MAX_LINES = 200
# How often (in appends) to run the trim sweep. The plain append is cheap; the
# re-read+rewrite is not, so we amortize it instead of paying it every turn. The
# file can briefly grow to _REQUEST_LOG_MAX_LINES + this before being trimmed
# back to the cap — still bounded, never the multi-MB-per-turn churn it was.
_REQUEST_LOG_TRIM_EVERY = 50
# In-memory append counter (one process = one chara, so a module global is fine).
_request_log_appends = 0


def _trim_request_log(path: Path) -> None:
    """Trim the request log back to the cap, writing atomically.

    Reads the file, and if it is over the cap rewrites the last
    _REQUEST_LOG_MAX_LINES lines to a sibling temp file then os.replace()s it
    into place (atomic — a crash mid-write can never leave a half-written or
    truncated log). Best-effort: any failure is swallowed by the caller."""
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    if len(lines) <= _REQUEST_LOG_MAX_LINES:
        return
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-_REQUEST_LOG_MAX_LINES:])
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
                "messages": messages,
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
