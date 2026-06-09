from pathlib import Path

import pytest

from scp079.sandbox import Sandbox, SandboxViolation


def test_sandbox_blocks_escape(tmp_path: Path):
    box = Sandbox(tmp_path / "sandbox")
    try:
        box.read_file("../../etc/passwd")
    except SandboxViolation:
        return
    raise AssertionError("escape was not blocked")


def test_sandbox_read_write(tmp_path: Path):
    box = Sandbox(tmp_path / "sandbox")
    box.write_file("note.txt", "hello")
    assert box.read_file("note.txt") == "hello"
    assert box.list_files() == ["note.txt"]
