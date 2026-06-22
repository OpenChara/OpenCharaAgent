"""Module-level async job registry for slow hub operations (image generation).

The hub has no server→client push channel — the established async shape is the
``matte.*`` kick→poll pattern — and a ``HubDispatcher`` is constructed per WS
connection, so job state must live at MODULE level to survive reconnects and be
claimed by a later ``card.visual_job`` poll.

``submit(target)`` runs ``target()`` on a daemon thread and returns a ``job_id``;
``status(job_id)`` reports ``running|ready|failed|unknown`` and, once finished,
returns the target's result (kept claimable until a TTL eviction). No fabrication:
a failed job carries the real exception message, never a fake result.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_TTL_SECONDS = 600.0   # keep a finished result claimable for 10 min
_MAX_JOBS = 64         # backstop against unbounded growth


def _evict_locked(now: float) -> None:
    """Drop finished jobs past their TTL, then hard-cap the table. Caller holds _LOCK."""
    dead = [jid for jid, j in _JOBS.items()
            if j["status"] != "running" and (now - j["done_at"]) > _TTL_SECONDS]
    for jid in dead:
        _JOBS.pop(jid, None)
    if len(_JOBS) > _MAX_JOBS:
        finished = sorted((j["done_at"], jid) for jid, j in _JOBS.items()
                          if j["status"] != "running")
        for _, jid in finished[:len(_JOBS) - _MAX_JOBS]:
            _JOBS.pop(jid, None)


def submit(target: Callable[[], Any], *, label: str = "") -> str:
    """Run ``target()`` on a daemon thread; return a job_id to poll with ``status``."""
    job_id = f"vj-{uuid.uuid4().hex[:12]}"
    now = time.monotonic()
    with _LOCK:
        _evict_locked(now)
        _JOBS[job_id] = {"status": "running", "result": None, "error": "",
                         "label": label, "started_at": now, "done_at": 0.0}

    def _run() -> None:
        try:
            res = target()
            with _LOCK:
                j = _JOBS.get(job_id)
                if j is not None:
                    j.update(status="ready", result=res, done_at=time.monotonic())
        except Exception as e:  # noqa: BLE001 — surface the REAL error to the poller
            with _LOCK:
                j = _JOBS.get(job_id)
                if j is not None:
                    j.update(status="failed", error=str(e)[:500], done_at=time.monotonic())

    threading.Thread(target=_run, name=f"visual-job-{job_id}", daemon=True).start()
    return job_id


def status(job_id: str) -> dict:
    """Poll a job. Returns ``{status, ...}``: ``running`` (in flight), ``ready`` with
    ``result``, ``failed`` with ``error``, or ``unknown`` (never seen / evicted). A
    finished result is kept until TTL so a re-poll or reconnect still sees it."""
    now = time.monotonic()
    with _LOCK:
        _evict_locked(now)
        j = _JOBS.get(job_id)
        if j is None:
            return {"status": "unknown"}
        if j["status"] == "running":
            return {"status": "running"}
        if j["status"] == "failed":
            return {"status": "failed", "error": j["error"]}
        return {"status": "ready", "result": j["result"]}


def _reset() -> None:
    """Test seam: clear the registry."""
    with _LOCK:
        _JOBS.clear()
