from pathlib import Path


from chara.tools.sandbox import Sandbox, SandboxViolation


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


def test_write_lands_in_workspace_one_file_space(tmp_path: Path):
    # write_file/read_file/list_files all live in workspace/ — the same dir the
    # terminal runs in. There is no separate files/ tree.
    box = Sandbox(tmp_path / "sandbox")
    box.write_file("art/aurora.txt", "lights")
    assert (box.workspace_dir / "art" / "aurora.txt").read_text() == "lights"
    assert box.read_file("art/aurora.txt") == "lights"
    assert box.list_files() == ["art/aurora.txt"]
    assert not (box.root / "files").exists()


def test_migration_folds_legacy_files_into_workspace(tmp_path: Path):
    root = tmp_path / "sandbox"
    legacy = root / "files" / "nested"
    legacy.mkdir(parents=True)
    (root / "files" / "old.txt").write_text("kept", encoding="utf-8")
    (legacy / "deep.txt").write_text("deep", encoding="utf-8")

    box = Sandbox(root)

    assert not (root / "files").exists()  # empty legacy dir removed
    assert box.read_file("old.txt") == "kept"
    assert box.read_file("nested/deep.txt") == "deep"


def test_migration_never_clobbers_existing_workspace_file(tmp_path: Path):
    root = tmp_path / "sandbox"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "note.txt").write_text("workspace wins", encoding="utf-8")
    (root / "files").mkdir(parents=True)
    (root / "files" / "note.txt").write_text("legacy copy", encoding="utf-8")

    box = Sandbox(root)

    # The pre-existing workspace file is untouched...
    assert box.read_file("note.txt") == "workspace wins"
    # ...and the clashing legacy file is kept under a suffixed name.
    names = box.list_files()
    assert "note.txt" in names
    migrated = [n for n in names if n.startswith("note.migrated-")]
    assert migrated, names
    assert box.read_file(migrated[0]) == "legacy copy"
