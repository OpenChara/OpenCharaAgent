"""The module-level async job registry behind the deck's async image generation
(mirrors the matte.* kick→poll pattern). No network; pure threading + TTL."""
from __future__ import annotations

import time

from chara.visuals import jobs


def _await(job_id: str, tries: int = 200) -> dict:
    for _ in range(tries):
        s = jobs.status(job_id)
        if s["status"] != "running":
            return s
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_job_runs_to_ready_and_returns_result():
    jobs._reset()
    jid = jobs.submit(lambda: {"x": 1})
    s = _await(jid)
    assert s["status"] == "ready" and s["result"] == {"x": 1}


def test_job_failure_surfaces_real_error():
    jobs._reset()

    def boom():
        raise RuntimeError("kaboom-xyz")

    s = _await(jobs.submit(boom))
    assert s["status"] == "failed" and "kaboom-xyz" in s["error"]


def test_unknown_job_id_is_unknown():
    jobs._reset()
    assert jobs.status("vj-nope")["status"] == "unknown"


def test_finished_result_evicted_after_ttl(monkeypatch):
    jobs._reset()
    jid = jobs.submit(lambda: 7)
    assert _await(jid)["status"] == "ready"
    # a ready result is re-claimable until the TTL lapses
    assert jobs.status(jid)["status"] == "ready"
    monkeypatch.setattr(jobs, "_TTL_SECONDS", -1.0)
    assert jobs.status(jid)["status"] == "unknown"
