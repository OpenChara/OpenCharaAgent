"""AuditLog must never crash the tool loop on a missing directory.

The session's sandbox/logs/ can be created lazily or wiped (reset/cleanup)
between constructing the log and a tool call, so write() recreates its parent.
"""
from __future__ import annotations

import json
import shutil

from chara.obs.audit import AuditLog


def test_write_creates_parent_dir(tmp_path):
    log = AuditLog(tmp_path / "logs" / "audit.jsonl")
    log.write("tool_call", tool="x", result={"ok": True})
    rows = log.tail()
    assert rows and rows[-1]["event"] == "tool_call" and rows[-1]["tool"] == "x"


def test_write_survives_dir_removed_after_construction(tmp_path):
    # construct (parent created), then the whole logs/ dir vanishes — write must
    # recreate it rather than raise FileNotFoundError into the tool loop.
    path = tmp_path / "logs" / "audit.jsonl"
    log = AuditLog(path)
    shutil.rmtree(path.parent)
    assert not path.parent.exists()
    log.write("tool_call", tool="y")  # must not raise
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["tool"] == "y"
